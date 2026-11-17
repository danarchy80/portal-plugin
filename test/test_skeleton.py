import portal.portal as p


def test_router_has_health():
    paths = {r.path for r in p.router.routes}
    assert "/health" in paths


def test_register_runs():
    assert p.register(object()) == {}
