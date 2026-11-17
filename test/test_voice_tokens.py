"""Task 4: single-use cloud live-voice token minting (Gemini / Qwen / Grok).

Covers the plan's Step 1 contract:
- minting for an unconfigured provider errors (never a bogus token);
- a configured provider returns a token that is NOT the raw credential and
  has ``expires_in_s <= 3600``;
- the GPT-Live case delegates to core ``/api/audio/voice-live/session``
  (asserted against a real HTTP stub server, not a local token).
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import portal.portal as p
import portal.voice_tokens as vt

MINT_ENV = {
    "gemini-live": "GEMINI_API_KEY",
    "qwen-live": "DASHSCOPE_API_KEY",
    "grok-live": "XAI_API_KEY",
}
FAKE_SECRET = "sk-hos…2345"


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(p.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_store():
    """Isolate the process-wide token store so 'minted nothing' assertions are per-case."""
    vt._tokens.clear()
    yield
    vt._tokens.clear()


@pytest.fixture()
def clear_env(monkeypatch):
    """Deterministic 'nothing configured' baseline (host .env layer is absent in the venv)."""
    for var in (*MINT_ENV.values(), "GOOGLE_API_KEY", "XAI_API_KEY", "DASHSCOPE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def core_stub():
    """Real HTTP stub standing in for the core gateway's GPT-Live session route."""
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            calls.append({"method": self.command, "path": self.path, "body": body})
            reply = json.dumps({"ok": True, "session_id": "core-sess-1", "sdp": "v0 answer"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"calls": calls, "base": f"http://127.0.0.1:{server.server_address[1]}"}
    server.shutdown()
    server.server_close()


# --- registry / gating -----------------------------------------------------

def test_gpt_live_is_not_a_mint_provider():
    assert "gpt-live" not in vt.PROVIDERS
    assert "gpt-live" in vt.DELEGATED_PROVIDERS


@pytest.mark.parametrize("provider", sorted(MINT_ENV))
def test_unconfigured_provider_errors_never_mints(client, clear_env, provider):
    resp = client.post("/voice/token", json={"provider": provider})
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"]
    assert vt.list_active_tokens() == []  # no bogus token was stored


@pytest.mark.parametrize("provider", sorted(MINT_ENV))
def test_configured_provider_mints(client, clear_env, monkeypatch, provider):
    monkeypatch.setenv(MINT_ENV[provider], FAKE_SECRET)
    resp = client.post("/voice/token", json={"provider": provider})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert set(data) == {"ok", "token", "expires_in_s"}
    # opaque and never the raw credential, short-TTL per the plan
    assert data["token"] and data["token"] != FAKE_SECRET
    assert FAKE_SECRET not in resp.text
    assert 0 < data["expires_in_s"] <= 3600


def test_unknown_provider_rejected(client, clear_env):
    resp = client.post("/voice/token", json={"provider": "skynet-live"})
    assert resp.status_code == 400
    assert "Unknown voice provider" in resp.json()["detail"]


# --- single-use semantics --------------------------------------------------

def test_token_is_single_use(client, clear_env, monkeypatch):
    monkeypatch.setenv(MINT_ENV["grok-live"], FAKE_SECRET)
    token = client.post("/voice/token", json={"provider": "grok-live"}).json()["token"]
    first = vt.consume_token(token)
    assert first is not None and first["provider"] == "grok-live"
    assert vt.consume_token(token) is None  # consumed — second redeem fails
    assert vt.consume_token("never-issued") is None


def test_expired_token_is_not_consumable(client, clear_env, monkeypatch):
    monkeypatch.setenv(MINT_ENV["gemini-live"], FAKE_SECRET)
    monkeypatch.setattr(vt, "TOKEN_TTL_S", 0)
    token = client.post("/voice/token", json={"provider": "gemini-live"}).json()["token"]
    assert vt.consume_token(token) is None


# --- GPT-Live delegation ----------------------------------------------------

def test_gpt_live_delegates_to_core_session_route(client, clear_env, monkeypatch, core_stub):
    monkeypatch.setenv("HERMES_CORE_BASE_URL", core_stub["base"])
    resp = client.post(
        "/voice/token",
        json={"provider": "gpt-live", "sdp": "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["session_id"] == "core-sess-1"
    assert "token" not in data  # delegated: the plugin minted nothing
    assert vt.list_active_tokens() == []
    assert len(core_stub["calls"]) == 1
    call = core_stub["calls"][0]
    assert call["path"] == "/api/audio/voice-live/session"
    assert call["body"]["sdp"].startswith("v=0")


def test_gpt_live_delegation_requires_sdp(client, clear_env, monkeypatch, core_stub):
    monkeypatch.setenv("HERMES_CORE_BASE_URL", core_stub["base"])
    resp = client.post("/voice/token", json={"provider": "gpt-live"})
    assert resp.status_code == 400
    assert core_stub["calls"] == []  # never reached core without an offer
