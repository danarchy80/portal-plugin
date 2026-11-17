from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health():
    return {"ok": True, "name": "portal", "version": "0.1.0-dev"}


def register(ctx):
    """Portal plugin. v1 registers no tools/hooks yet — skeleton only."""
    return {}
