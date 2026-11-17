import portal.portal as p


def _mounted_paths():
    """Concrete route paths on the plugin router. Routes are registered
    flat (no include_router composition; see portal.py), so this matches
    what the host serves at /api/plugins/portal/ (op_asgi mounts this same
    router on a bare FastAPI)."""
    from fastapi.routing import APIRoute

    return {r.path for r in p.router.routes if isinstance(r, APIRoute)}


def test_router_has_health():
    assert "/health" in _mounted_paths()


def test_register_runs():
    assert p.register(object()) == {}
