"""Opt-in phone-data sinks: location / calendar / contacts / health.

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

TOOLSET = "portal"

OPT_IN_CLASSES = ("location", "calendar", "contacts", "health")

# Health record vocabulary (Portal app posts Health Connect summaries).
HEALTH_TYPES = ("steps", "heart_rate", "sleep_duration", "weight")
HEALTH_SOURCES = ("health_connect",)
MAX_HEALTH_VALUE = 10_000_000

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
    "health": {"type": 32, "unit": 16, "source": 32},
}
_TIME_FIELDS = {
    "location": (("timestamp", True),),
    "calendar": (("start", True), ("end", False)),
    "contacts": (),
    "health": (("timestamp", True), ("end_timestamp", False)),
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
    "health": {"value"},
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
    if cls == "health":
        if out.get("type") not in HEALTH_TYPES:
            raise ValueError(f"type must be one of {list(HEALTH_TYPES)}")
        if not out.get("unit"):
            raise ValueError("unit is required (e.g. count, bpm, min, kg)")
        if out.get("source") not in HEALTH_SOURCES:
            raise ValueError(f"source must be one of {list(HEALTH_SOURCES)}")
        value = record.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("value must be a number")
        if not (0 <= float(value) <= MAX_HEALTH_VALUE):
            raise ValueError(f"value out of range [0, {MAX_HEALTH_VALUE}]")
        out["value"] = float(value)
        # Both timestamps are strict fixed-width %Y-%m-%dT%H:%M:%SZ here, so
        # lexicographic order equals chronological order.
        if "end_timestamp" in out and out["end_timestamp"] < out["timestamp"]:
            raise ValueError("end_timestamp must be >= timestamp")
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

    @router.post("/phone/health")
    async def phone_health_sink(request: Request):
        """Receive a health-metrics sync from the Portal app (opt-in gated)."""
        return await _sink("health", request)


def make_test_client():
    """A FastAPI app exactly like the host's plugin-host mount (op_asgi:
    ``FastAPI()`` + ``include_router(router)``), for integration tests and
    manual probes. The TestClient import is deferred so this test-only
    dependency never breaks a production plugin load."""
    from fastapi.testclient import TestClient  # local: test-only dep

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


def _handle_portal_health(args: dict, **kw) -> str:
    if (err := _opted_or_feed("health")) is not None:
        return err
    store = read_store("health")
    records = store["records"]
    rtype = args.get("type")
    if isinstance(rtype, str) and rtype.strip():
        rtype = rtype.strip()
        if rtype not in HEALTH_TYPES:
            return _tool_error(f"type must be one of {list(HEALTH_TYPES)}")
        records = [r for r in records if r.get("type") == rtype]
    limit = args.get("limit", 50)
    limit = 50 if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 else min(limit, 500)
    out = {"ok": True, "class": "health", "updated_at": store["updated_at"],
           "count": len(records), "records": records[-limit:]}
    if rtype:
        out["type"] = rtype
    return json.dumps(out, ensure_ascii=False)


# --------------------------------------------------------------------------
# agent-triggered locate (portal_locate_phone)
# --------------------------------------------------------------------------

LOCATE_KIND = "portal_locate"
LOCATE_TIMEOUT_S = 45.0
LOCATE_POLL_INTERVAL_S = 1.0


def _current_profile() -> str:
    """Profile id for this process's Hermes home (registration files key
    devices by this name). Falls back to "default" outside a profile tree or
    in a bare test venv without hermes_constants."""
    try:
        from hermes_constants import get_hermes_home, profile_name_for_home
        name = profile_name_for_home(get_hermes_home())
        if name:
            return name
    except Exception:
        pass
    return "default"


def _send_locate_ping(profile: str, payload: dict) -> dict:
    """Push ``payload`` to every FCM device registered for ``profile``.

    Thin seam over ``hermes_cli.fcm_notifier`` (in-process import: the plugin
    runs inside the gateway process). Encryption rides the module's own
    ``_send_one`` -> ``_encrypt_payload`` path (AES-256-GCM, base64
    nonce||ct||tag in ``data["encrypted"]``) — the exact wire format the app
    already decrypts — and the service-account auth path stays core's.
    Returns ``{"ok": bool, ...}``; never raises; logs nothing secret.
    """
    try:
        from hermes_cli import fcm_notifier
    except ImportError:
        return {"ok": False,
                "error": "hermes_cli.fcm_notifier is not importable in this process"}
    try:
        regs = fcm_notifier._load_registrations()
        devices = [(token, reg) for token, reg in regs.items()
                   if reg.get("profile") == profile and reg.get("encryption_key")]
        if not devices:
            return {"ok": False,
                    "error": f"no FCM-registered device for profile {profile!r}"}
        sent = sum(1 for token, reg in devices
                   if fcm_notifier._send_one(token, reg["encryption_key"], payload))
    except Exception as exc:
        return {"ok": False,
                "error": f"FCM locate ping failed: {type(exc).__name__}: {exc}"}
    if not sent:
        return {"ok": False, "error": "FCM send failed (service account not "
                                      "configured, or delivery error — see gateway log)"}
    return {"ok": True, "devices": sent}


def _find_fresh_location(requested_at: str) -> dict | None:
    """Newest location record whose timestamp is >= requested_at (strict
    fixed-width %Y-%m-%dT%H:%M:%SZ, so lexicographic == chronological)."""
    for record in reversed(read_store("location")["records"]):
        ts = record.get("timestamp")
        if isinstance(ts, str) and ts >= requested_at:
            return record
    return None


def _handle_portal_locate_phone(args: dict, **kw) -> str:
    """Ping the phone for a fresh GPS fix and wait for it to land in the
    location store. Never raises: every failure becomes structured JSON."""
    try:
        if (err := _opted_or_feed("location")) is not None:
            return err
        profile = args.get("profile")
        profile = profile.strip() if isinstance(profile, str) and profile.strip() else _current_profile()
        requested_at = _now_iso()
        payload = {"kind": LOCATE_KIND, "requested_at": requested_at}
        ping = _send_locate_ping(profile, payload)
        if not isinstance(ping, dict) or not ping.get("ok"):
            message = ping.get("error") if isinstance(ping, dict) else None
            return json.dumps({"ok": False, "error": message or "locate ping failed",
                               "requested_at": requested_at}, ensure_ascii=False)
        deadline = time.monotonic() + LOCATE_TIMEOUT_S
        while True:
            fresh = _find_fresh_location(requested_at)
            if fresh is not None:
                return json.dumps({"ok": True, "stale": False,
                                   "requested_at": requested_at, "record": fresh},
                                  ensure_ascii=False)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(LOCATE_POLL_INTERVAL_S, remaining))
        records = read_store("location")["records"]
        out = {"ok": True, "stale": True, "requested_at": requested_at,
               "record": records[-1] if records else None,
               "note": f"no fresh fix arrived within {LOCATE_TIMEOUT_S:.0f}s"
                       + ("" if records else "; no location has ever been synced")}
        return json.dumps(out, ensure_ascii=False)
    except Exception as exc:
        return _tool_error(f"portal_locate_phone failed: {type(exc).__name__}: {exc}")


_PORTAL_LOCATION_SCHEMA = {
    "name": "portal_location",
    "description": (
        "Read the phone's latest synced location (and optionally recent history) "
        "from the Portal app. Read-only; returns an error unless the "
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
        "Read calendar events synced from the phone by the Portal app. "
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
        "Read contacts synced from the phone by the Portal app, "
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

_PORTAL_HEALTH_SCHEMA = {
    "name": "portal_health",
    "description": (
        "Read health metrics synced from the phone by the Portal app "
        "(steps, heart_rate, sleep_duration, weight). Returns the latest "
        "snapshot, optionally filtered by type. Read-only; requires the "
        "profile's health opt-in. Returns [] before any sync."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": list(HEALTH_TYPES),
                "description": "Only return records of this metric type.",
            },
            "limit": {"type": "integer", "description": "Max records to return (default 50)."},
        },
        "required": [],
    },
}

_PORTAL_LOCATE_PHONE_SCHEMA = {
    "name": "portal_locate_phone",
    "description": (
        "Ask the phone for a fresh GPS fix now: sends an encrypted locate "
        "push over FCM, then waits up to 45 s for a location record newer "
        "than the request to arrive. Returns the fresh record (stale=false), "
        "or the last-known record with stale=true on timeout. Requires the "
        "profile's location opt-in and a registered FCM device."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "profile": {
                "type": "string",
                "description": "FCM registration profile to ping (defaults to this host's profile).",
            },
        },
        "required": [],
    },
}

TOOL_SPECS: list[tuple[dict, Callable]] = [
    (_PORTAL_LOCATION_SCHEMA, _handle_portal_location),
    (_PORTAL_CALENDAR_SCHEMA, _handle_portal_calendar),
    (_PORTAL_CONTACTS_SCHEMA, _handle_portal_contacts),
    (_PORTAL_HEALTH_SCHEMA, _handle_portal_health),
    (_PORTAL_LOCATE_PHONE_SCHEMA, _handle_portal_locate_phone),
]


def register_phone_tools(ctx) -> None:
    """Register the read-only phone-data tools + portal_locate_phone (plan
    Task 3 step 4; health/locate extend the same site).

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
    usage = "usage: python -m portal.phone {opt-in|opt-out|status} [location|calendar|contacts|health]"
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
