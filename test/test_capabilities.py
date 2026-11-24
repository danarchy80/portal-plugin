"""C1 behavioral tests for the calendar capability probe — FIXTURE LEVEL.

These tests exercise the portal plugin router through a local FastAPI app
mounted the way the host mounts it (``include_router`` under
``/api/plugins/portal``), with the two ``hermes_cli`` seams the handler
calls at request time stubbed (``web_server._require_token``,
``profiles.get_active_profile_name``). They are deliberately NOT stock-host
evidence: the real authenticated stock app, real plugin mounting through
``dashboard/manifest.json`` + ``api.py``, and real profile scoping are
covered by ``test_capabilities_stock.py``.

Async tests require ``pytest-asyncio`` (strict mode, explicit markers).
"""

import asyncio
import os
import sys
import types
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException

import portal.phone as portal_phone
import portal.portal as portal_module

pytestmark = pytest.mark.asyncio

TOKEN = "cap-fixture-token"
BOUND_PROFILE = "calendar-test"


def snapshot_phone_files(home):
    """Recursive relative-path -> bytes map of ``home/plugins/portal/phone``.

    Returns ``{}`` when the directory is absent; never creates it.
    """
    root = Path(home) / "plugins" / "portal" / "phone"
    if not root.is_dir():
        return {}
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _make_profile(root: Path, name: str) -> Path:
    home = root / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    return home


def _set_calendar_flag(home: Path, enabled: bool) -> None:
    flag = home / "plugins" / "portal" / "phone" / "optin.calendar"
    if enabled:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("2026-11-24T00:00:00Z\n", encoding="utf-8")
    else:
        flag.unlink(missing_ok=True)


def _install_stock_stubs(monkeypatch, root: Path):
    """Stub only the two stock seams capabilities.py imports at request time.

    ``_require_token`` mirrors stock loopback semantics (401 unless the
    session header matches); ``get_active_profile_name`` mirrors stock
    resolution (name under ``<root>/profiles/``, ``default`` at the root,
    ``custom`` anywhere else).
    """
    pkg = types.ModuleType("hermes_cli")
    ws = types.ModuleType("hermes_cli.web_server")

    def _require_token(request):
        if request.headers.get("X-Hermes-Session-Token", "") != TOKEN:
            raise HTTPException(status_code=401, detail="Unauthorized")

    ws._require_token = _require_token
    prof = types.ModuleType("hermes_cli.profiles")

    def get_active_profile_name():
        home = Path(os.environ["HERMES_HOME"]).resolve()
        if home == root.resolve():
            return "default"
        try:
            parts = home.relative_to((root / "profiles").resolve()).parts
        except ValueError:
            return "custom"
        return parts[0] if len(parts) == 1 else "custom"

    prof.get_active_profile_name = get_active_profile_name
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server", ws)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", prof)


@pytest.fixture
def hermes_root(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    (root / "profiles").mkdir(parents=True)
    _make_profile(root, BOUND_PROFILE)
    _make_profile(root, "plain")
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / BOUND_PROFILE))
    _install_stock_stubs(monkeypatch, root)
    return root


@pytest.fixture
def app(hermes_root):
    application = FastAPI()
    # Same mount shape as the stock host (web_server_dashboard._mount_one_plugin_api).
    application.include_router(portal_module.router, prefix="/api/plugins/portal")
    return application


@pytest_asyncio.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://portal.test",
        headers={"X-Hermes-Session-Token": TOKEN},
    ) as http:
        yield http


@pytest.fixture
def profile_home(hermes_root):
    return hermes_root / "profiles" / BOUND_PROFILE


EXPECTED_FALSE = {
    "schema_version": 1,
    "plugin": {"name": "portal", "version": "0.2.0-dev", "api_loaded": True},
    "scope": {"profile": BOUND_PROFILE, "authoritative": True},
    "calendar": {"supported": True, "read_sync": True, "host_opted_in": False},
}


async def test_capabilities_reads_flag_without_calendar_data(client, profile_home):
    before = snapshot_phone_files(profile_home)
    out = await client.get(f"/api/plugins/portal/capabilities?profile={BOUND_PROFILE}")
    assert out.status_code == 200
    assert out.json() == {
        "schema_version": 1,
        "plugin": {"name": "portal", "version": "0.2.0-dev", "api_loaded": True},
        "scope": {"profile": "calendar-test", "authoritative": True},
        "calendar": {"supported": True, "read_sync": True, "host_opted_in": False},
    }
    assert snapshot_phone_files(profile_home) == before


async def test_capabilities_optin_true(client, profile_home):
    _set_calendar_flag(profile_home, True)
    out = await client.get(f"/api/plugins/portal/capabilities?profile={BOUND_PROFILE}")
    assert out.status_code == 200
    assert out.json()["calendar"]["host_opted_in"] is True


async def test_capabilities_no_store_header(client, profile_home):
    out = await client.get(f"/api/plugins/portal/capabilities?profile={BOUND_PROFILE}")
    assert out.status_code == 200
    assert out.headers.get("cache-control") == "no-store"


async def test_capabilities_body_under_16kib(client, profile_home):
    out = await client.get(f"/api/plugins/portal/capabilities?profile={BOUND_PROFILE}")
    assert out.status_code == 200
    assert len(out.content) <= 16 * 1024


async def test_capabilities_denied_auth(app, hermes_root):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://portal.test") as bare:
        out = await bare.get(f"/api/plugins/portal/capabilities?profile={BOUND_PROFILE}")
        assert out.status_code == 401


async def test_capabilities_custom_profile_rejected_409(client, hermes_root, monkeypatch):
    stray = hermes_root.parent / "stray-home"
    stray.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(stray))
    out = await client.get(f"/api/plugins/portal/capabilities?profile={BOUND_PROFILE}")
    assert out.status_code == 409
    assert out.json() == {"schema_version": 1, "error": "scope_unverified"}


async def test_capabilities_profile_error_rejected_409(client, monkeypatch):
    def boom():
        raise RuntimeError("resolution failed")

    monkeypatch.setattr(sys.modules["hermes_cli.profiles"], "get_active_profile_name", boom)
    out = await client.get(f"/api/plugins/portal/capabilities?profile={BOUND_PROFILE}")
    assert out.status_code == 409
    assert out.json() == {"schema_version": 1, "error": "scope_unverified"}


async def test_capabilities_guard_import_failure_fails_closed(app, hermes_root, monkeypatch):
    """Import failure of the stock guard is unsupported (404), never allow-all."""
    monkeypatch.delitem(sys.modules, "hermes_cli.web_server", raising=False)
    monkeypatch.delitem(sys.modules, "hermes_cli.profiles", raising=False)
    monkeypatch.setitem(sys.modules, "hermes_cli", None)  # import of hermes_cli.* halts
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://portal.test") as bare:
        out = await bare.get(f"/api/plugins/portal/capabilities?profile={BOUND_PROFILE}")
        assert out.status_code == 404


async def test_capabilities_two_profiles_opposite_flags(client, hermes_root, monkeypatch):
    bound = hermes_root / "profiles" / BOUND_PROFILE
    other = hermes_root / "profiles" / "plain"
    _set_calendar_flag(bound, True)
    _set_calendar_flag(other, False)

    out = await client.get(f"/api/plugins/portal/capabilities?profile={BOUND_PROFILE}")
    assert out.status_code == 200
    assert out.json()["scope"]["profile"] == BOUND_PROFILE
    assert out.json()["calendar"]["host_opted_in"] is True

    monkeypatch.setenv("HERMES_HOME", str(other))
    out = await client.get(f"/api/plugins/portal/capabilities?profile={BOUND_PROFILE}")
    assert out.status_code == 200
    # Never a query echo: the bound context wins over the requested name.
    assert out.json()["scope"]["profile"] == "plain"
    assert out.json()["calendar"]["host_opted_in"] is False


async def test_capabilities_zero_data_store_calls(client, profile_home, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("capability probe must not touch phone data paths")

    for name in ("ingest", "read_store", "read_history", "set_opt_in", "_write_json", "_append_history"):
        monkeypatch.setattr(portal_phone, name, boom)
    out = await client.get(f"/api/plugins/portal/capabilities?profile={BOUND_PROFILE}")
    assert out.status_code == 200
    assert out.json() == EXPECTED_FALSE


async def test_capabilities_route_flat_registered():
    from fastapi.routing import APIRoute

    paths = {r.path for r in portal_module.router.routes if isinstance(r, APIRoute)}
    assert "/capabilities" in paths
    # Existing routes preserved.
    assert {"/health", "/pulse", "/voice/token"} <= paths


async def test_missing_route_still_404(client):
    out = await client.get("/api/plugins/portal/no-such-route")
    assert out.status_code == 404


async def test_health_route_preserved(client):
    out = await client.get("/api/plugins/portal/health")
    assert out.status_code == 200
    assert out.json()["ok"] is True
