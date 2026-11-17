from fastapi import APIRouter

from .voice_tokens import register_routes as register_voice_routes

router = APIRouter()

# Cloud live-voice token minting -> /api/plugins/portal/voice/token
register_voice_routes(router)


@router.get("/health")
async def health():
    return {"ok": True, "name": "portal", "version": "0.1.0-dev"}


def register(ctx):
    """Portal plugin. v1 registers no tools/hooks yet — skeleton only."""
    return {}
