"""Single-use cloud live-voice token minting for the Portal app (Task 4).

``POST /api/plugins/portal/voice/token`` with ``{"provider": ...}`` answers
``{"ok": true, "token": ..., "expires_in_s": ...}`` for the providers core
does not already serve (Gemini Live, Qwen Live, Grok Live).

Privacy contract (plan Global Constraints): provider credentials are read
from the host ``.env`` on every mint and are NEVER included in any response
or stored alongside a token. Tokens themselves are opaque random handles —
they carry no credential material and mean nothing outside this process;
the realtime relay that redeems them consumes them exactly once via
:func:`consume_token`.

GPT-Live is deliberately NOT minted here: core already serves that session
at ``POST /api/audio/voice-live/session`` (verified in
``hermes_cli/web_routers/audio.py``), so the route forwards to core and
returns core's answer — never duplicating the seam.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

# Short-TTL by design; the plan caps it at one hour.
TOKEN_TTL_S = 120
MAX_TTL_S = 3600

CORE_SESSION_TIMEOUT_S = 30


def _core_base_url() -> str:
    """Core gateway base for delegation (same host the dashboard serves on)."""
    return (os.environ.get("HERMES_CORE_BASE_URL") or "http://127.0.0.1:9119").rstrip("/")


CORE_VOICE_LIVE_SESSION_PATH = "/api/audio/voice-live/session"


# --- credential gating -------------------------------------------------------

def _env_value(name: str) -> str:
    """One credential value from the host .env, env-var fallback (core's own
    pattern in ``web_routers/audio.py``). The value is never logged."""
    try:
        from hermes_cli.config import load_env  # profile-scoped ~/.hermes/.env

        value = (load_env().get(name) or "").strip()
    except Exception:
        value = ""
    if not value:
        value = (os.environ.get(name) or "").strip()
    return value


def _configured(*names: str) -> bool:
    return any(_env_value(name) for name in names)


# provider tag -> gating. ``env_hint`` names the variable in the "not
# configured" error; no value ever crosses into a response.
PROVIDERS: dict[str, dict[str, Any]] = {
    "gemini-live": {
        "check_fn": lambda: _configured("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "env_hint": "GEMINI_API_KEY (or GOOGLE_API_KEY)",
    },
    "qwen-live": {
        "check_fn": lambda: _configured("DASHSCOPE_API_KEY"),
        "env_hint": "DASHSCOPE_API_KEY",
    },
    "grok-live": {
        "check_fn": lambda: _configured("XAI_API_KEY"),
        "env_hint": "XAI_API_KEY",
    },
}

# Providers core already serves: delegate, never mint (plan Task 4).
DELEGATED_PROVIDERS = frozenset({"gpt-live"})


# --- single-use token store ---------------------------------------------------

_lock = threading.Lock()
_tokens: dict[str, dict[str, Any]] = {}


def _purge_expired(now: float) -> None:
    dead = [tok for tok, ent in _tokens.items() if ent["expires_at"] <= now]
    for tok in dead:
        del _tokens[tok]


def issue_token(provider: str) -> tuple[str, int]:
    """Mint an opaque single-use handle bound to ``provider``. Returns
    ``(token, ttl_seconds)``. No credential material is stored or derived."""
    token = secrets.token_urlsafe(32)
    ttl = min(TOKEN_TTL_S, MAX_TTL_S)
    now = time.monotonic()
    with _lock:
        _purge_expired(now)
        _tokens[token] = {"provider": provider, "expires_at": now + ttl}
    return token, ttl


def consume_token(token: str) -> Optional[dict[str, Any]]:
    """Redeem a token exactly once. Returns ``{"provider": ...}`` on first
    valid redeem; ``None`` if unknown, expired, or already consumed."""
    now = time.monotonic()
    with _lock:
        entry = _tokens.pop(token, None)  # pop = single-use, even for racers
    if entry is None or entry["expires_at"] <= now:
        return None
    return entry


def list_active_tokens() -> list[str]:
    """Live (unexpired, unconsumed) token handles — introspection for tests
    and the future relay. Handles are opaque randoms; revealing them here is
    process-local only."""
    now = time.monotonic()
    with _lock:
        return [tok for tok, ent in _tokens.items() if ent["expires_at"] > now]


# --- GPT-Live delegation --------------------------------------------------------

def _core_json_post(path: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        _core_base_url() + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=CORE_SESSION_TIMEOUT_S) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"detail": raw.decode("utf-8", "replace")[:500]}


async def _delegate_to_core(payload: "VoiceTokenRequest", request: Request) -> dict[str, Any]:
    """Forward the WebRTC offer to core's GPT-Live session route verbatim."""
    if not (payload.sdp or "").strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "provider 'gpt-live' delegates to core /api/audio/voice-live/session "
                "— an SDP offer is required; this route mints no token for it."
            ),
        )
    body: dict[str, Any] = {"sdp": payload.sdp}  # byte-exact: core needs the offer incl. trailing CRLF
    if payload.history is not None:
        body["history"] = payload.history
    # Forward the caller's own dashboard credentials so core applies the same
    # auth; nothing is invented or cached here.
    forwarded = {k: v for k, v in request.headers.items() if k.lower() in ("cookie", "authorization")}
    try:
        status, data = await asyncio.to_thread(
            _core_json_post, CORE_VOICE_LIVE_SESSION_PATH, body, forwarded
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HTTPException(
            status_code=502, detail=f"core voice-live session route unreachable: {exc.__class__.__name__}"
        )
    if status >= 400:
        raise HTTPException(status_code=status, detail=data.get("detail", f"core returned {status}"))
    return data


# --- route ----------------------------------------------------------------------

class VoiceTokenRequest(BaseModel):
    provider: str
    sdp: Optional[str] = None          # gpt-live delegation only
    history: Optional[list[Any]] = None  # gpt-live delegation only


def register_routes(router: APIRouter) -> None:
    """Mount the voice routes flat onto the portal router.

    Deliberately not ``router.include_router(...)``: FastAPI >=0.163 nests
    included routers as opaque ``_IncludedRouter`` branch objects whose
    entries lack ``.path``, breaking structural assertions on
    ``portal.router.routes`` (Task 1's skeleton test). Flat registration
    keeps the route table inspectable and the mount identical.
    """

    @router.post("/voice/token")
    async def voice_token(payload: VoiceTokenRequest, request: Request) -> Any:
        provider = payload.provider.strip().lower()

        if provider in DELEGATED_PROVIDERS:
            return await _delegate_to_core(payload, request)

        spec = PROVIDERS.get(provider)
        if spec is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown voice provider '{payload.provider}'. "
                    f"Mintable: {', '.join(sorted(PROVIDERS))}; delegated: {', '.join(sorted(DELEGATED_PROVIDERS))}."
                ),
            )
        if not spec["check_fn"]():
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Voice provider '{provider}' not configured on the host: "
                    f"set {spec['env_hint']} in the host .env (~/.hermes/.env)."
                ),
            )

        token, ttl = issue_token(provider)
        return {"ok": True, "token": token, "expires_in_s": ttl}
