"""Opt-in phone-data sinks: location / calendar / contacts.

Privacy model (plan 2026-11-17-portal-plugin.md, Task 3 + Global Constraints):

* Phone data is **opt-in, off by default**. Each data class has its own flag
  file under ``<hermes home>/plugins/portal/phone/optin.<class>``; sinks
  refuse every payload until the profile opts in, and the agent tools return
  an empty feed until then. The user's own shell opts in with
  ``python -m portal.phone opt-in <class>`` (``opt-out`` / ``status`` too).
* Payloads arrive **envelope-encrypted to the device's own key** (the same
  AES-256-GCM scheme the core FCM path uses). The host stores the opaque
  ``enc`` blob it cannot read, plus a small strict-validated plaintext record
  so agent tools can answer simple questions without holding any key. No
  secret ever leaves the host, and the host never sees plaintext it didn't
  get to validate.
* Per-change consent rides the **core approval/clarify flow**: this module
  registers no consent UI, no approval transport, no notify path, and exposes
  no HTTP route that an agent can call to mutate the store. The sinks are
  client-only ingestion; agent-facing tools are read-only and therefore
  dispatched as ordinary registered tools, where core's approval system
  gates them with the standard approval card if the profile's approvals mode
  requires it. Reaching for anything else would be the parallel consent path
  the plan forbids.

Storage is JSON under ``get_hermes_home()`` (context-local, per-profile),
written atomically with 0600 perms:

* ``<home>/plugins/portal/phone/<class>.json``         latest snapshot
* ``<home>/plugins/portal/phone/<class>.history.json`` capped append log
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient  # noqa: F401  (test helper surface)

TOOLSET = "portal"

OPT_IN_CLASSES = ("location", "calendar", "contacts")

# Hard caps. Anything bigger is refused outright — these sinks are tiny
# encrypted envelopes, not a blob store.
MAX_BODY_BYTES = 1 << 20          # whole request body: 1 MiB
MAX_ENC_B64_BYTES = 64 * 1024     # one record's encrypted blob (base64 text)
HISTORY_CAP = 500                 # append-log entries kept per class
SYNC_ITEM_CAP = 2000              # records per calendar/contacts sync

OPT_IN_FILENAME = "optin.{cls}"
STORE_FILENAME = "{cls}.json"
HISTORY_FILENAME = "{cls}.history.json"
STORE_SCHEMA_VERSION = 1

# Per-class plaintext record shape: field -> max string length (numbers get
# explicit range checks). Unknown keys are rejected so the plaintext lane
# cannot smuggle bulk or encrypted data past the caps.
_PLAIN_FIELDS: dict[str, dict[str, int]] = {
    "location": {"label": 120},
    "calendar": {"id": 64, "title": 500, "location": 200},
    "contacts": {"name": 200, "note": 500},
}
_TIME_FIELDS = {
    "location": (("timestamp", True),),
    "calendar": (("start", True), ("end", False)),
    "contacts": (),
}

_ERR_OPT_IN = (
    "portal phone data is opt-in and this profile has not opted in for "
    f"'{{cls}}'. The profile owner runs `python -m portal.phone opt-in {{cls}}` "
    "on the Hermes host to enable it."
)


# --------------------------------------------------------------------------
# profile-scoped paths
# --------------------------------------------------------------------------

def _hermes_home() -> Path:
    """The calling process's profile home.

    Same context-local resolution the host gives plugins
    (``hermes_constants.get_hermes_home``: override -> ``HERMES_HOME`` ->
    platform default). Imported lazily so the module also loads in a bare
    test venv, where the ``HERMES_HOME`` env fallback provides the same
    per-profile scoping.
    """
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        env = os.environ.get("HERMES_HOME", "").strip()
        return Path(env).expanduser() if env else Path.home() / ".hermes"
    return Path(get_hermes_home())


def store_dir() -> Path:
    return _hermes_home() / "plugins" / "portal" / "phone"


def _flag_path(cls: str) -> Path:
    return store_dir() / OPT_IN_FILENAME.format(cls=cls)


def _store_path(cls: str) -> Path:
    return store_dir() / STORE_FILENAME.format(cls=cls)


def _history_path(cls: str) -> Path:
    return store_dir() / HISTORY_FILENAME.format(cls=cls)


def _phone_dir(home: Path | None = None) -> Path:
    return store_dir() if home is None else home / "plugins" / "portal" / "phone"


def is_opted_in(cls: str, home: Path | None = None) -> bool:
    """Per-class opt-in flag presence. Off (missing) unless explicitly created."""
    if cls not in OPT_IN_CLASSES:
        raise ValueError(f"unknown phone data class {cls!r}")
    return (_phone_dir(home) / OPT_IN_FILENAME.format(cls=cls)).is_file()


def set_opt_in(cls: str, enabled: bool, home: Path | None = None) -> bool:
    """Create/remove the opt-in flag. Owner-side action only (CLI / shell);
    deliberately *not* reachable over HTTP — consent changes must not have a
    network-reachable mutation path."""
    if cls not in OPT_IN_CLASSES:
        raise ValueError(f"unknown phone data class {cls!r}")
    flag = _phone_dir(home) / OPT_IN_FILENAME.format(cls=cls)
    if enabled:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n", encoding="utf-8")
    else:
        flag.unlink(missing_ok=True)
    return flag.is_file()


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_iso_z(value: str) -> bool:
    if not isinstance(value, str) or not value.endswith("Z") or len(value) < 20:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        return True
    except ValueError:
        return False


_ALLOWED_FIELDS: dict[str, set[str]] = {
    "location": {"lat", "lng", "accuracy_m"},
    "calendar": set(),
    "contacts": set(),
}


def _validate_record(cls: str, record: Any) -> dict:
    """Strictly validate one plaintext record; return a normalized copy.

    Unknown keys, oversized strings, missing/bad timestamps, and oversized or
    non-base64 ``enc`` blobs are ``ValueError`` (-> HTTP 400).
    """
    if not isinstance(record, dict):
        raise ValueError("record must be a JSON object")
    allowed = set(_PLAIN_FIELDS[cls]) | _ALLOWED_FIELDS[cls] | {"enc"} | {f for f, _ in _TIME_FIELDS[cls]}
    unknown = sorted(set(record) - allowed)
    if unknown:
        raise ValueError(f"unknown field(s) {unknown}; allowed: {sorted(allowed)}")

    out: dict[str, Any] = {}
    for field, maxlen in _PLAIN_FIELDS[cls].items():
        if field not in record:
            continue
        value = record[field]
        if not isinstance(value, str):
            raise ValueError(f"{field} must be a string")
        value = value.strip()
        if len(value) > maxlen:
            raise ValueError(f"{field} longer than {maxlen} chars")
        out[field] = value

    for field, required in _TIME_FIELDS[cls]:
        if field not in record:
            if required:
                raise ValueError(f"{field} is required")
            continue
        if not _is_iso_z(record[field]):
            raise ValueError(f"{field} must be an ISO-8601 UTC string like 2026-11-17T12:00:00Z")
        out[field] = record[field]

    if "enc" in record:
        blob = record["enc"]
        if not isinstance(blob, str):
            raise ValueError("enc must be a base64 string")
        if len(blob) > MAX_ENC_B64_BYTES:
            raise ValueError(f"enc blob larger than {MAX_ENC_B64_BYTES} bytes")
        try:
            base64.b64decode(blob, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("enc must be strict base64 (device-key ciphertext)")
        out["enc"] = blob

    if cls == "location":
        for key, lo, hi in (("lat", -90.0, 90.0), ("lng", -180.0, 180.0)):
            value = record.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{key} must be a number")
            if not (lo <= float(value) <= hi):
                raise ValueError(f"{key} out of range [{lo}, {hi}]")
            out[key] = float(value)
        accuracy = record.get("accuracy_m")
        if accuracy is not None:
            if not isinstance(accuracy, (int, float)) or isinstance(accuracy, bool) or accuracy < 0:
                raise ValueError("accuracy_m must be a non-negative number")
            out["accuracy_m"] = float(accuracy)
    return out


def _validate_sync(cls: str, payload: Any) -> list[dict]:
    """Validate the POST body for one class; return normalized records."""
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    unknown = sorted(set(payload) - {"class", "records"})
    if unknown:
        raise ValueError(f"unknown field(s) {unknown}; allowed: ['class', 'records']")
    declared = payload.get("class")
    if declared not in (None, cls):
        raise ValueError(f"class field {declared!r} does not match route {cls!r}")
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("records must be a non-empty list")
    if len(records) > SYNC_ITEM_CAP:
        raise ValueError(f"more than {SYNC_ITEM_CAP} records in one sync")
    return [_validate_record(cls, r) for r in records]


# --------------------------------------------------------------------------
# store (atomic JSON, 0600)
# --------------------------------------------------------------------------

def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _append_history(cls: str, records: list[dict]) -> None:
    history = _read_json(_history_path(cls))
    entries = history.get("entries") if isinstance(history, dict) else None
    if not isinstance(entries, list):
        entries = []
    entries.extend(records[-HISTORY_CAP:])
    _write_json(_history_path(cls), {"schema": STORE_SCHEMA_VERSION, "entries": entries[-HISTORY_CAP:]})


def ingest(cls: str, payload: dict) -> dict:
    """Validate and persist one sync for ``cls``. Raises ValueError/TypeError
    on malformed input (route maps those to 400)."""
    records = _validate_sync(cls, payload)
    stamp = _now_iso()
    for record in records:
        record["received_at"] = stamp
    _write_json(_store_path(cls), {
        "schema": STORE_SCHEMA_VERSION,
        "class": cls,
        "updated_at": stamp,
        "records": records,
    })
    _append_history(cls, records)
    return {"stored": len(records), "class": cls, "updated_at": stamp}


def read_store(cls: str) -> dict:
    data = _read_json(_store_path(cls))
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        return {"updated_at": None, "records": []}
    return data


def read_history(cls: str) -> list[dict]:
    history = _read_json(_history_path(cls))
    entries = history.get("entries") if isinstance(history, dict) else None
    return entries if isinstance(entries, list) else []


# --------------------------------------------------------------------------
# HTTP sinks (mounted by portal.router at /api/plugins/portal/)
# --------------------------------------------------------------------------

router = APIRouter()


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": message})


async def _sink(cls: str, request: Request):
    """One shared sink body for the three classes (plan: same pattern)."""
    if not is_opted_in(cls):
        return _err(403, _ERR_OPT_IN.format(cls=cls))
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY_BYTES:
                return _err(413, f"payload larger than {MAX_BODY_BYTES} bytes")
        except ValueError:
            pass
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return _err(413, f"payload larger than {MAX_BODY_BYTES} bytes")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _err(400, "body must be valid UTF-8 JSON")
    try:
        result = ingest(cls, payload)
    except (ValueError, TypeError) as exc:
        return _err(400, str(exc))
    return JSONResponse(status_code=201, content={"ok": True, **result})


def register_routes(router: APIRouter) -> None:
    """Mount the three phone sinks flat onto the portal router.

    Deliberately not ``router.include_router(...)``: FastAPI >=0.163 nests
    included routers as opaque ``_IncludedRouter`` branch objects whose
    entries lack ``.path``, breaking structural assertions on
    ``portal.router.routes`` (Task 1's skeleton test). Flat registration
    keeps the route table inspectable and the mount identical.
    """

    @router.post("/phone/location")
    async def phone_location_sink(request: Request):
        """Receive an encrypted location sync from the Portal app (opt-in gated)."""
        return await _sink("location", request)

    @router.post("/phone/calendar")
    async def phone_calendar_sink(request: Request):
        """Receive an encrypted calendar sync from the Portal app (opt-in gated)."""
        return await _sink("calendar", request)

    @router.post("/phone/contacts")
    async def phone_contacts_sink(request: Request):
        """Receive an encrypted contacts sync from the Portal app (opt-in gated)."""
        return await _sink("contacts", request)


def make_test_client() -> TestClient:
    """A FastAPI app exactly like the host's plugin-host mount (op_asgi:
    ``FastAPI()`` + ``include_router(router)``), for integration tests and
    manual probes."""
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# --------------------------------------------------------------------------
# agent tools (read-only; consent/approval handled by core's tool flow)
# --------------------------------------------------------------------------

def _tool_error(message: str) -> str:
    return json.dumps({"error": message}, ensure_ascii=False)


def _opted_or_feed(cls: str) -> str | None:
    """Return the error envelope when opted out, else None (empty feed is fine)."""
    if not is_opted_in(cls):
        return _tool_error(_ERR_OPT_IN.format(cls=cls))
    return None


def _handle_portal_location(args: dict, **kw) -> str:
    if (err := _opted_or_feed("location")) is not None:
        return err
    store = read_store("location")
    records = store["records"]
    latest = records[-1] if records else None
    out = {"ok": True, "class": "location", "updated_at": store["updated_at"], "latest": latest}
    if args.get("include_history"):
        out["history"] = read_history("location")
    return json.dumps(out, ensure_ascii=False)


def _handle_portal_calendar(args: dict, **kw) -> str:
    if (err := _opted_or_feed("calendar")) is not None:
        return err
    store = read_store("calendar")
    events = store["records"]
    limit = args.get("limit", 20)
    limit = 20 if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 else min(limit, 200)
    if args.get("all_events"):
        selected = events
    else:
        now = datetime.now(timezone.utc)
        def _upcoming(event: dict) -> bool:
            end = event.get("end") or event.get("start")
            if not end:
                return True
            try:
                return datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) >= now
            except ValueError:
                return True
        selected = [e for e in events if _upcoming(e)]
    out = {"ok": True, "class": "calendar", "updated_at": store["updated_at"],
           "count": len(selected), "events": selected[-limit:]}
    return json.dumps(out, ensure_ascii=False)


def _handle_portal_contacts(args: dict, **kw) -> str:
    if (err := _opted_or_feed("contacts")) is not None:
        return err
    store = read_store("contacts")
    contacts = store["records"]
    query = args.get("query")
    if isinstance(query, str) and query.strip():
        needle = query.strip().lower()
        contacts = [c for c in contacts
                    if needle in c.get("name", "").lower() or needle in c.get("note", "").lower()]
    out = {"ok": True, "class": "contacts", "updated_at": store["updated_at"],
           "count": len(contacts), "contacts": contacts}
    return json.dumps(out, ensure_ascii=False)


_PORTAL_LOCATION_SCHEMA = {
    "name": "portal_location",
    "description": (
        "Read the phone's latest synced location (and optionally recent history) "
        "from the Hermes Portal app. Read-only; returns an error unless the "
        "profile has opted in to location sharing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "include_history": {
                "type": "boolean",
                "description": "Include the capped location history log (default false).",
            },
        },
        "required": [],
    },
}

_PORTAL_CALENDAR_SCHEMA = {
    "name": "portal_calendar",
    "description": (
        "Read calendar events synced from the phone by the Hermes Portal app. "
        "Defaults to upcoming events. Read-only; requires the profile's "
        "calendar opt-in."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Max events to return (default 20)."},
            "all_events": {"type": "boolean", "description": "Return the full synced snapshot, not just upcoming events."},
        },
        "required": [],
    },
}

_PORTAL_CONTACTS_SCHEMA = {
    "name": "portal_contacts",
    "description": (
        "Read contacts synced from the phone by the Hermes Portal app, "
        "optionally filtered by a name/note substring. Read-only; requires "
        "the profile's contacts opt-in. Returns [] before any sync."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Case-insensitive substring filter on name/note."},
        },
        "required": [],
    },
}

TOOL_SPECS: list[tuple[dict, Callable]] = [
    (_PORTAL_LOCATION_SCHEMA, _handle_portal_location),
    (_PORTAL_CALENDAR_SCHEMA, _handle_portal_calendar),
    (_PORTAL_CONTACTS_SCHEMA, _handle_portal_contacts),
]


def register_phone_tools(ctx) -> None:
    """Register the three read-only phone-data tools (plan Task 3 step 4).

    No check_fn gating: the manifest already advertises the tools, and an
    opted-out profile should see the clear opt-in error from the handler,
    not a silently missing tool.
    """
    for schema, handler in TOOL_SPECS:
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema,
                          handler=handler, emoji="📱")


# --------------------------------------------------------------------------
# owner-side CLI: opt-in / opt-out / status (never over HTTP)
# --------------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    usage = "usage: python -m portal.phone {opt-in|opt-out|status} [location|calendar|contacts]"
    if not argv:
        print(usage)
        return 2
    action, cls = argv[0], (argv[1] if len(argv) > 1 else None)
    if action == "status":
        for name in OPT_IN_CLASSES:
            print(f"{name}: {'ON' if is_opted_in(name) else 'off'}")
        return 0
    if action not in ("opt-in", "opt-out"):
        print(usage)
        return 2
    if cls not in OPT_IN_CLASSES:
        print(usage)
        return 2
    enabled = set_opt_in(cls, action == "opt-in")
    print(f"{cls}: {'opted IN — the Portal app may now sync this data class' if enabled else 'opted out — sinks refuse it'}")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
