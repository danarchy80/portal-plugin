"""C1 stock-host integration tests — REAL stock Hermes app.

Evidence category: real stock host. Requires ``PORTAL_M1_STOCK_SRC`` pointing
at an extracted checkout of the exact stock commit (444f980868937926562fe5652d26364d45550960)
plus its runtime import deps on the interpreter's path; the module skips
(honestly, visibly) otherwise. Fixture-level stubs in ``test_capabilities.py``
are a separate evidence category and never substitute for these.

What is real here:
- the stock ``hermes_cli.web_server`` app object, imported fresh against an
  isolated throwaway hermes root (never the user's ``~/.hermes``);
- plugin installation + enablement discovered through
  ``dashboard/manifest.json`` and mounted through ``dashboard/api.py`` by the
  stock mount path (``_mount_plugin_api_routes`` at import), not a hand-built
  router;
- authentication through the stock ``_require_token`` seam (session token);
- profile scoping through the stock ``?profile=`` route dependency.

Multi-launch-profile scenarios (second profile, custom home, disabled plugin)
run the stock app in a subprocess per launch, because the launch home is
frozen at stock import time.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

pytestmark = pytest.mark.skipif(
    not (
        os.environ.get("PORTAL_M1_STOCK_SRC", "")
        and Path(os.environ.get("PORTAL_M1_STOCK_SRC", ""))
        .joinpath("hermes_cli", "web_server.py")
        .is_file()
    ),
    reason=(
        "stock-host evidence needs PORTAL_M1_STOCK_SRC=<extracted stock 444f980 checkout> "
        "with its runtime deps importable; skipping is NOT a pass"
    ),
)

asyncio_mark = pytest.mark.asyncio

STOCK_SRC = os.environ.get("PORTAL_M1_STOCK_SRC", "")
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
TOKEN = "stock-cap-token"
BOUND_PROFILE = "calendar-test"

EXPECTED_KEYS = {
    "schema_version": 1,
    "plugin": {"name": "portal", "version": "0.2.0-dev", "api_loaded": True},
    "scope": {"profile": BOUND_PROFILE, "authoritative": True},
    "calendar": {"supported": True, "read_sync": True, "host_opted_in": False},
}

_ENV_KEYS = ("HERMES_HOME", "HOME", "HERMES_DASHBOARD_SESSION_TOKEN")

_SUBPROCESS_SCRIPT = """
import asyncio, json, os, sys
import hermes_cli.web_server as ws
import httpx

async def main():
    transport = httpx.ASGITransport(app=ws.app)
    async with httpx.AsyncClient(transport=transport, base_url="https://portal.test") as client:
        out = await client.get(
            os.environ["PROBE_PATH"],
            headers={"X-Hermes-Session-Token": os.environ["PROBE_TOKEN"]},
        )
    print(json.dumps({
        "status": out.status_code,
        "body": out.text,
        "cache_control": out.headers.get("cache-control"),
    }))

asyncio.run(main())
"""


def snapshot_phone_files(home):
    root = Path(home) / "plugins" / "portal" / "phone"
    if not root.is_dir():
        return {}
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _install_plugin(root: Path) -> Path:
    dest = root / "plugins" / "portal"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PLUGIN_ROOT, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
    return dest


def _make_profile(root: Path, name: str, *, enable_portal: bool) -> Path:
    home = root / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    lines = ["plugins:\n", "  enabled:\n"] if enable_portal else ["plugins:\n", "  enabled: []\n"]
    if enable_portal:
        lines.append("    - portal\n")
    (home / "config.yaml").write_text("".join(lines), encoding="utf-8")
    return home


def _set_calendar_flag(home: Path, enabled: bool) -> None:
    flag = home / "plugins" / "portal" / "phone" / "optin.calendar"
    if enabled:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("2026-11-24T00:00:00Z\n", encoding="utf-8")
    else:
        flag.unlink(missing_ok=True)


@pytest.fixture(scope="session")
def stock_root(tmp_path_factory):
    """Isolated throwaway hermes root: real layout, never the user's home."""
    root = tmp_path_factory.mktemp("portal-m1-stock-root") / "hermes-root"
    (root / "fake-home").mkdir(parents=True)
    _install_plugin(root)
    _make_profile(root, BOUND_PROFILE, enable_portal=True)
    _make_profile(root, "plain", enable_portal=True)
    _make_profile(root, "off", enable_portal=False)
    return root


@pytest.fixture(scope="session")
def stock_ws(stock_root):
    """Import the real stock app once against the isolated launch profile."""
    saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
    path_before = list(sys.path)
    modules_before = set(sys.modules)
    handlers_before = set(__import__("logging").root.handlers)
    os.environ["HERMES_HOME"] = str(stock_root / "profiles" / BOUND_PROFILE)
    os.environ["HOME"] = str(stock_root / "fake-home")
    os.environ["HERMES_DASHBOARD_SESSION_TOKEN"] = TOKEN
    sys.path.insert(0, STOCK_SRC)
    try:
        import hermes_cli.web_server as ws

        yield ws
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        sys.path[:] = path_before
        import logging

        for handler in list(logging.root.handlers):
            if handler not in handlers_before:
                logging.root.removeHandler(handler)
        for name in set(sys.modules) - modules_before:
            sys.modules.pop(name, None)


@pytest.fixture
def profile_home(stock_root):
    return stock_root / "profiles" / BOUND_PROFILE


@pytest_asyncio.fixture
async def client(stock_ws):
    transport = httpx.ASGITransport(app=stock_ws.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://portal.test",
        headers={"X-Hermes-Session-Token": TOKEN},
    ) as http:
        yield http


@pytest_asyncio.fixture
async def bare_client(stock_ws):
    transport = httpx.ASGITransport(app=stock_ws.app)
    async with httpx.AsyncClient(transport=transport, base_url="https://portal.test") as http:
        yield http


def _run_stock_subprocess(root: Path, launch_home: Path, probe_path: str) -> dict:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(root / "fake-home"),
        "HERMES_HOME": str(launch_home),
        "HERMES_DASHBOARD_SESSION_TOKEN": TOKEN,
        "PYTHONPATH": STOCK_SRC,
        "PROBE_PATH": probe_path,
        "PROBE_TOKEN": TOKEN,
    }
    proc = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"stock subprocess failed: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@asyncio_mark
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


@asyncio_mark
async def test_capabilities_served_by_loaded_package(client, stock_ws):
    """Same-package reuse: the mounted route is served by the package that
    dashboard/api.py loaded (hermes_plugins.portal), not a second import."""
    out = await client.get(f"/api/plugins/portal/capabilities?profile={BOUND_PROFILE}")
    assert out.status_code == 200
    loaded = sys.modules["hermes_plugins.portal"]
    paths = {r.path for r in loaded.router.routes if hasattr(r, "path")}
    assert "/capabilities" in paths


@asyncio_mark
async def test_capabilities_optin_true(client, profile_home):
    _set_calendar_flag(profile_home, True)
    try:
        out = await client.get(f"/api/plugins/portal/capabilities?profile={BOUND_PROFILE}")
        assert out.status_code == 200
        assert out.json()["calendar"]["host_opted_in"] is True
    finally:
        _set_calendar_flag(profile_home, False)


@asyncio_mark
async def test_capabilities_no_store_header(client):
    out = await client.get(f"/api/plugins/portal/capabilities?profile={BOUND_PROFILE}")
    assert out.status_code == 200
    assert out.headers.get("cache-control") == "no-store"


@asyncio_mark
async def test_capabilities_denied_auth(bare_client):
    out = await bare_client.get(f"/api/plugins/portal/capabilities?profile={BOUND_PROFILE}")
    assert out.status_code == 401


@asyncio_mark
async def test_capabilities_missing_route_404(client):
    out = await client.get("/api/plugins/portal/no-such-route")
    assert out.status_code == 404


@asyncio_mark
async def test_capabilities_zero_data_store_calls(client, monkeypatch):
    import hermes_plugins.portal.phone as loaded_phone

    def boom(*args, **kwargs):
        raise AssertionError("capability probe must not touch phone data paths")

    for name in ("ingest", "read_store", "read_history", "set_opt_in", "_write_json", "_append_history"):
        monkeypatch.setattr(loaded_phone, name, boom)
    out = await client.get(f"/api/plugins/portal/capabilities?profile={BOUND_PROFILE}")
    assert out.status_code == 200


@asyncio_mark
async def test_existing_health_route_preserved(client):
    out = await client.get("/api/plugins/portal/health")
    assert out.status_code == 200
    assert out.json()["ok"] is True


def test_stock_second_profile_opposite_flag(stock_root):
    """Two launch profiles, opposite flags. Each launch serves its own bound
    profile and reads its own flag; the ``profile=current`` query keeps the
    scope on the launch profile (no echo, no defaulting)."""
    bound = stock_root / "profiles" / BOUND_PROFILE
    _set_calendar_flag(bound, True)
    plain = stock_root / "profiles" / "plain"
    _set_calendar_flag(plain, False)
    try:
        result = _run_stock_subprocess(
            stock_root, plain, "/api/plugins/portal/capabilities?profile=current"
        )
    finally:
        _set_calendar_flag(bound, False)
    assert result["status"] == 200
    body = json.loads(result["body"])
    assert body["scope"]["profile"] == "plain"
    assert body["calendar"]["host_opted_in"] is False
    assert result["cache_control"] == "no-store"


def test_stock_bound_profile_query_binds_second_profile(stock_root):
    """A valid ``?profile=`` selection is bound by the stock route scope
    dependency, and the probe reports THAT bound context (flag of the bound
    profile), proving the answer is derived, not echoed from the launch."""
    bound = stock_root / "profiles" / BOUND_PROFILE
    _set_calendar_flag(bound, True)
    try:
        result = _run_stock_subprocess(
            stock_root,
            stock_root / "profiles" / "plain",
            f"/api/plugins/portal/capabilities?profile={BOUND_PROFILE}",
        )
    finally:
        _set_calendar_flag(bound, False)
    assert result["status"] == 200
    body = json.loads(result["body"])
    assert body["scope"]["profile"] == BOUND_PROFILE
    assert body["calendar"]["host_opted_in"] is True


def test_stock_unknown_profile_query_404(stock_root):
    """An unknown profile selection never reaches the handler: the stock
    route scope dependency answers 404 (no defaulting to the launch home)."""
    result = _run_stock_subprocess(
        stock_root,
        stock_root / "profiles" / BOUND_PROFILE,
        "/api/plugins/portal/capabilities?profile=ghost-profile",
    )
    assert result["status"] == 404


def test_stock_custom_home_scope_unverified(stock_root):
    """A launch home inside the hermes root but outside ``profiles/`` resolves
    to ``custom`` and is rejected 409 rather than defaulting to any name."""
    native = stock_root / "fake-home" / ".hermes"
    stray = native / "odd-launch-home"
    stray.mkdir(parents=True, exist_ok=True)
    (native / "plugins").mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        PLUGIN_ROOT,
        native / "plugins" / "portal",
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
    )
    (stray / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - portal\n", encoding="utf-8"
    )
    result = _run_stock_subprocess(
        stock_root, stray, "/api/plugins/portal/capabilities?profile=current"
    )
    assert result["status"] == 409
    assert json.loads(result["body"]) == {"schema_version": 1, "error": "scope_unverified"}


def test_stock_disabled_plugin_404(stock_root):
    """A profile whose config does not enable portal gets 404 (never served,
    never readiness) — the honest NotLoadedOrUnsupported answer."""
    off = stock_root / "profiles" / "off"
    result = _run_stock_subprocess(
        stock_root, off, "/api/plugins/portal/capabilities?profile=current"
    )
    assert result["status"] == 404
