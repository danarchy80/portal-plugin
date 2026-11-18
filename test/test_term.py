"""Task 1 (plan 2026-11-18-remote-terminal.md): host-side PTY session
manager + routes.

Integration shape mirrors the host and test_phone.py: the plugin package is
imported under the name ``portal`` (see conftest.py) and exercised through a
FastAPI app built exactly like ``plugin_host_child.op_asgi`` mounts it
(``FastAPI()`` + ``include_router(portal.router)``). Sessions are REAL PTYs
spawned on this Linux host — no mocks on the tty path.
"""

import base64
import json
import time
import uuid

import pytest

from portal import term
from portal.portal import router


@pytest.fixture(autouse=True)
def profile_home(tmp_path, monkeypatch):
    """Isolated per-profile HERMES_HOME + the real clock by default."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    term.set_clock(time.monotonic)
    yield tmp_path
    term.shutdown_all()
    term.set_clock(time.monotonic)


@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router)  # the portal router, as op_asgi mounts it
    return TestClient(app)


@pytest.fixture()
def opted_in(profile_home):
    term.set_opt_in(True)
    return profile_home


# ------------------------------------------------------------------ helpers

def _start(client, **body):
    body.setdefault("cols", 80)
    body.setdefault("rows", 24)
    resp = client.post("/term/start", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


def _send(client, sid, text):
    resp = client.post(
        f"/term/{sid}/input",
        json={"data": base64.b64encode(text.encode()).decode()},
    )
    assert resp.status_code == 204, resp.text


def _poll_until(client, sid, predicate, *, deadline=20.0):
    """Long-poll the output route (cursor-advancing) until predicate(joined,
    last_body) is true. Returns (joined_text, last_body)."""
    joined = ""
    since = 0
    t_end = time.time() + deadline
    while time.time() < t_end:
        resp = client.get(f"/term/{sid}/output", params={"since": since})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        joined += base64.b64decode(body["data"]).decode("utf-8", "replace")
        since = body["cursor"]
        if predicate(joined, body):
            return joined, body
    raise AssertionError(f"output predicate not met; tail: {joined[-600:]!r}")


# ------------------------------------------------------------------- opt-in

def test_status_reports_disabled_by_default(client):
    resp = client.get("/term/status")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "sessions": []}


def test_start_refused_without_opt_in(client):
    resp = client.post("/term/start", json={"cols": 80, "rows": 24})
    assert resp.status_code == 403
    assert "opt-in" in resp.json()["error"]


def test_opt_in_cli_toggles_the_flag(client, profile_home):
    assert term._cli(["opt-in"]) == 0
    assert client.get("/term/status").json()["enabled"] is True
    assert term._cli(["opt-out"]) == 0
    assert client.get("/term/status").json()["enabled"] is False
    assert term._cli(["status"]) == 0
    assert term._cli(["bogus"]) == 2


# ------------------------------------------------------------- PTY round trip

def test_round_trip_over_real_pty(client, opted_in):
    sid = _start(client)
    assert client.post(f"/term/{sid}/input",
                       json={"data": base64.b64encode(b"true\n").decode()}).status_code == 204
    marker = f"portal-term-{uuid.uuid4().hex[:10]}"
    _send(client, sid, f"echo {marker}\n")
    _poll_until(client, sid, lambda joined, b: marker in joined)

    _send(client, sid, "exit\n")
    _, body = _poll_until(client, sid, lambda joined, b: b["eof"], deadline=15)
    assert body["exit_code"] == 0
    # Fully drained after eof -> gone (410), not a phantom empty 200.
    assert client.get(f"/term/{sid}/output",
                      params={"since": body["cursor"]}).status_code == 410


def test_start_validates_body(client, opted_in):
    assert client.post("/term/start", json={}).status_code in (400, 422)
    assert client.post("/term/start", json={"cols": 0, "rows": 24}).status_code in (400, 422)
    resp = client.post("/term/start", json={"cols": 80, "rows": 24, "cwd": "/nonexistent-portal-term"})
    assert resp.status_code == 400


def test_start_accepts_cwd(client, opted_in, tmp_path):
    sid = _start(client, cwd=str(tmp_path))
    _send(client, sid, "pwd\n")
    joined, _ = _poll_until(client, sid, lambda j, b: str(tmp_path) in j)
    assert str(tmp_path) in joined


# ------------------------------------------------------------ resize / kill

def test_resize_returns_204(client, opted_in):
    sid = _start(client)
    resp = client.post(f"/term/{sid}/resize", json={"cols": 120, "rows": 40})
    assert resp.status_code == 204


def test_delete_kills_session(client, opted_in):
    sid = _start(client)
    assert client.delete(f"/term/{sid}").status_code == 204
    _, body = _poll_until(client, sid, lambda j, b: b["eof"], deadline=10)
    assert client.get(f"/term/{sid}/output",
                      params={"since": body["cursor"]}).status_code == 410


def test_unknown_session_id_is_404_everywhere(client, opted_in):
    b64 = base64.b64encode(b"x").decode()
    assert client.post("/term/nope/input", json={"data": b64}).status_code == 404
    assert client.get("/term/nope/output").status_code == 404
    assert client.post("/term/nope/resize", json={"cols": 80, "rows": 24}).status_code == 404
    assert client.delete("/term/nope").status_code == 404


def test_bad_base64_input_is_400(client, opted_in):
    sid = _start(client)
    assert client.post(f"/term/{sid}/input", json={"data": "not base64!!"}).status_code == 400


# ------------------------------------------------------- caps and lifecycle

def test_third_concurrent_session_refused(client, opted_in):
    _start(client)
    _start(client)
    resp = client.post("/term/start", json={"cols": 80, "rows": 24})
    assert resp.status_code == 429


def test_status_lists_live_sessions(client, opted_in):
    sid = _start(client)
    body = client.get("/term/status").json()
    assert body["enabled"] is True
    assert [s["session_id"] for s in body["sessions"] if not s["eof"]] == [sid]


def test_idle_reaper_kills_sessions_without_activity(client, opted_in):
    fake = [1000.0]
    term.set_clock(lambda: fake[0])
    sid = _start(client)

    fake[0] += term.IDLE_TIMEOUT_SECONDS - 60
    term.reap_once()  # not idle long enough yet
    body = client.get(f"/term/{sid}/output").json()  # an attached poll...
    assert body["eof"] is False  # ...still alive

    fake[0] += term.IDLE_TIMEOUT_SECONDS + 1  # >deadline since THAT poll
    term.reap_once()
    _poll_until(client, sid, lambda j, b: b["eof"], deadline=10)


def test_reaper_leaves_active_sessions(client, opted_in):
    fake = [1000.0]
    term.set_clock(lambda: fake[0])
    sid = _start(client)
    fake[0] += term.IDLE_TIMEOUT_SECONDS - 1
    term.reap_once()
    assert client.get(f"/term/{sid}/output").json()["eof"] is False


# ---------------------------------------------------------------- audit log

def test_audit_records_lifecycle_but_never_command_bytes(client, opted_in, profile_home):
    audit = profile_home / "plugins" / "portal" / "term" / "audit.log"
    sid = _start(client)
    secret = f"echo-secret-{uuid.uuid4().hex[:8]}"
    _send(client, sid, f"echo {secret}\n")
    _poll_until(client, sid, lambda j, b: secret in j)
    _send(client, sid, "exit\n")
    _poll_until(client, sid, lambda j, b: b["eof"], deadline=15)

    rows = []
    t_end = time.time() + 5
    while time.time() < t_end:
        if audit.is_file():
            rows = [json.loads(line) for line in
                    audit.read_text(encoding="utf-8").splitlines() if line.strip()]
            if any(r.get("event") == "stop" and r.get("session_id") == sid for r in rows):
                break
        time.sleep(0.1)
    assert any(r.get("event") == "start" and r.get("session_id") == sid for r in rows), rows
    stop = next(r for r in rows if r.get("event") == "stop" and r.get("session_id") == sid)
    assert stop.get("exit_code") == 0
    # The audit trail is lifecycle-only: keystrokes/command bytes never land.
    assert secret not in audit.read_text(encoding="utf-8")


# ------------------------------------------------------------- ring buffer

def test_ring_buffer_caps_output_and_keeps_the_tail(client, opted_in):
    sid = _start(client)
    # ~330 KiB burst: exceeds the 256 KiB ring, so the tail must survive.
    _send(client, sid, "yes abcdefghij | head -n 30000; echo RING-TAIL-MARKER\n")
    since = 0
    saw_tail = False
    t_end = time.time() + 30
    while time.time() < t_end and not saw_tail:
        resp = client.get(f"/term/{sid}/output", params={"since": since})
        assert resp.status_code == 200
        body = resp.json()
        data = base64.b64decode(body["data"])
        assert len(data) <= term.RING_BYTES
        saw_tail = b"RING-TAIL-MARKER" in data
        since = body["cursor"]
    assert saw_tail, "ring buffer lost the tail of a 330 KiB burst"


# ------------------------------------------------------------------ wiring

def test_term_routes_registered_flat_on_portal_router():
    from fastapi.routing import APIRoute

    routes = {r.path: r.methods for r in router.routes if isinstance(r, APIRoute)}
    assert {p for p in routes if p.startswith("/term/")} == {
        "/term/start",
        "/term/status",
        "/term/{session_id}",
        "/term/{session_id}/input",
        "/term/{session_id}/output",
        "/term/{session_id}/resize",
    }
    assert routes["/term/start"] == {"POST"}
    assert routes["/term/status"] == {"GET"}
    assert routes["/term/{session_id}"] == {"DELETE"}
    assert routes["/term/{session_id}/input"] == {"POST"}
    assert routes["/term/{session_id}/output"] == {"GET"}
    assert routes["/term/{session_id}/resize"] == {"POST"}
