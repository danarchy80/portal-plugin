"""Calendar capability probe (shared v1 wire contract).

``GET /api/plugins/portal/capabilities?profile=<selected>`` answers, from the
bound host context only, which profile this companion actually serves and
whether that profile has opted in to phone calendar data. It reads the
existing owner-side opt-in flag (``phone.is_opted_in("calendar",
home=bound_home)``) and nothing else: no events, no stores, no counts, no
paths, no secrets, and no consent mutation (consent changes deliberately
have no network-reachable path — see ``phone.set_opt_in``).

Fail-closed by construction:

- the stock request guard (``hermes_cli.web_server._require_token``, which
  also honours hosted request-local admission) is imported and called per
  request; if it cannot be imported the route answers 404 (unsupported),
  never allow-all;
- the actual profile is derived per request from the bound host context
  (``hermes_cli.profiles.get_active_profile_name()`` and the same
  context-local home ``phone._hermes_home()`` resolves), never echoed from
  the query and never captured at import time; a ``custom`` home or any
  resolution error answers 409 ``scope_unverified`` rather than defaulting.

``api_loaded: true`` proves this routed plugin API executed; it makes no
claim about universal tool reload.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

SCHEMA_VERSION = 1
IMPLEMENTATION_VERSION = "0.2.0-dev"
_NO_STORE = {"Cache-Control": "no-store"}
_SCOPE_UNVERIFIED = {"schema_version": SCHEMA_VERSION, "error": "scope_unverified"}


def _json(status: int, body: dict) -> JSONResponse:
    return JSONResponse(status_code=status, content=body, headers=dict(_NO_STORE))


async def capabilities(request: Request):
    """Read-only capability probe. See module docstring for the contract."""
    try:
        from hermes_cli.web_server import _require_token
    except Exception:
        # No stock guard available: this deployment cannot authenticate the
        # probe, so it is unsupported here. Fail closed — never allow-all.
        return _json(404, {"detail": "Not Found"})
    _require_token(request)  # raises HTTPException(401) unless admitted

    try:
        from hermes_cli.profiles import get_active_profile_name

        profile = get_active_profile_name()
    except Exception:
        return _json(409, _SCOPE_UNVERIFIED)
    if not profile or profile == "custom":
        return _json(409, _SCOPE_UNVERIFIED)

    try:
        from . import phone

        bound_home = phone._hermes_home()
        host_opted_in = bool(phone.is_opted_in("calendar", home=bound_home))
    except Exception:
        return _json(409, _SCOPE_UNVERIFIED)

    return _json(
        200,
        {
            "schema_version": SCHEMA_VERSION,
            "plugin": {
                "name": "portal",
                "version": IMPLEMENTATION_VERSION,
                "api_loaded": True,
            },
            "scope": {"profile": profile, "authoritative": True},
            "calendar": {
                "supported": True,
                "read_sync": True,
                "host_opted_in": host_opted_in,
            },
        },
    )


def register_routes(router: APIRouter) -> None:
    """Flat registration onto the plugin router (see portal.py)."""
    router.add_api_route("/capabilities", capabilities, methods=["GET"])
