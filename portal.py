from fastapi import APIRouter

from .pulse import register_tools as register_pulse_tools
from .pulse import register_routes as register_pulse_routes
from .voice_tokens import register_routes as register_voice_routes

router = APIRouter()

# Flat route registration everywhere (no include_router): FastAPI >=0.163
# nests included routers as opaque _IncludedRouter branches, which breaks
# structural assertions on portal.router.routes (Task 1's skeleton test).

# Cloud live-voice token minting -> /api/plugins/portal/voice/token
register_voice_routes(router)

# Pulse card feed -> /api/plugins/portal/pulse
register_pulse_routes(router)

# Phone-data sinks -> /api/plugins/portal/phone/{location,calendar,contacts,health}.
# Optional module (Task 3): absent until that task lands its files, and a
# pushed plugin package must import cleanly without it.
try:
    from . import phone as _phone
except ImportError:  # pragma: no cover - depends on task sequencing
    _phone = None
if _phone is not None:
    _phone.register_routes(router)

# Remote terminal (opt-in host PTY sessions) -> /api/plugins/portal/term/*.
# Same optional-module pattern: routes are flat-registered, and a pushed
# plugin package must import cleanly even if this module were absent.
try:
    from . import term as _term
except ImportError:  # pragma: no cover - depends on task sequencing
    _term = None
if _term is not None:
    _term.register_routes(router)


@router.get("/health")
async def health():
    return {"ok": True, "name": "portal", "version": "0.1.0-dev"}


def register(ctx):
    """Portal plugin. Registers the declared tools (plugin.yaml
    provides_tools); routes ride the module-level ``router``.

    Tolerates a ctx without ``register_tool`` (the plugin host always has
    one; skeleton-style dummy ctxs are for import/mount verification only)
    so a partial ctx can never break plugin load.
    """
    if hasattr(ctx, "register_tool"):
        register_pulse_tools(ctx)  # pulse_get
        if _phone is not None:
            _phone.register_phone_tools(ctx)  # portal_location/calendar/contacts/health + portal_locate_phone
    return {}
