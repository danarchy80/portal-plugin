"""Addendum task: POST /pulse/outcome — validation + per-profile JSON store.

The phone-side PulseRepository.outcome posts {card_id, action, rating?} here.
Unknown actions must 4xx with a clear error and NEVER reach the store.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import portal.pulse as pulse


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(pulse, "resolve_home", lambda profile=None: tmp_path)
    return tmp_path


@pytest.fixture
def client(fake_home):
    app = FastAPI()
    app.include_router(pulse.router)
    return TestClient(app)


def _store_path(home):
    return home / "plugins" / "portal" / "pulse" / "outcomes.json"


def _entries(home):
    path = _store_path(home)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == pulse.OUTCOME_SCHEMA_VERSION
    return data["entries"]


# ── happy path ───────────────────────────────────────────────────────────────

def test_valid_action_is_stored(client, fake_home):
    resp = client.post(
        "/pulse/outcome", json={"card_id": "cron-failures", "action": "do_it"}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    entries = _entries(fake_home)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["card_id"] == "cron-failures"
    assert entry["action"] == "do_it"
    assert entry["rating"] is None
    assert entry["ts"].endswith("Z") or "+00:00" in entry["ts"]


def test_all_four_actions_accepted(client, fake_home):
    for i, action in enumerate(("do_it", "done", "put_off", "dismiss")):
        resp = client.post(
            "/pulse/outcome", json={"card_id": f"c{i}", "action": action}
        )
        assert resp.status_code == 200, action
    assert [e["action"] for e in _entries(fake_home)] == [
        "do_it", "done", "put_off", "dismiss",
    ]


def test_optional_rating_stored(client, fake_home):
    resp = client.post(
        "/pulse/outcome",
        json={"card_id": "brief", "action": "done", "rating": 4},
    )
    assert resp.status_code == 200
    assert _entries(fake_home)[0]["rating"] == 4


def test_action_is_case_insensitive_and_canonicalized(client, fake_home):
    # The phone's Kotlin enum serializes uppercase names (DO_IT); store lowercase.
    resp = client.post(
        "/pulse/outcome", json={"card_id": "x", "action": "  DO_IT "}
    )
    assert resp.status_code == 200
    assert _entries(fake_home)[0]["action"] == "do_it"


# ── rejection: never store ───────────────────────────────────────────────────

def test_unknown_action_rejected_never_stored(client, fake_home):
    resp = client.post(
        "/pulse/outcome", json={"card_id": "x", "action": "nuke_from_orbit"}
    )
    assert resp.status_code == 400
    assert "nuke_from_orbit" in resp.text  # echoes what was rejected
    assert resp.text.lower().count("do_it") >= 1  # lists valid actions
    assert _store_path(fake_home).exists() is False


def test_blank_or_missing_card_id_rejected(client, fake_home):
    resp = client.post("/pulse/outcome", json={"card_id": "   ", "action": "done"})
    assert resp.status_code == 400
    resp = client.post("/pulse/outcome", json={"action": "done"})
    assert resp.status_code >= 400  # 422 from the model, still a rejection
    assert _store_path(fake_home).exists() is False


@pytest.mark.parametrize("rating", [0, 6, -1, 100])
def test_rating_out_of_range_rejected_never_stored(client, fake_home, rating):
    resp = client.post(
        "/pulse/outcome",
        json={"card_id": "x", "action": "done", "rating": rating},
    )
    assert resp.status_code == 400
    assert _store_path(fake_home).exists() is False


def test_oversized_card_id_rejected(client, fake_home):
    resp = client.post(
        "/pulse/outcome", json={"card_id": "x" * 500, "action": "done"}
    )
    assert resp.status_code == 400
    assert _store_path(fake_home).exists() is False


# ── store mechanics ──────────────────────────────────────────────────────────

def test_entries_capped(client, fake_home, monkeypatch):
    monkeypatch.setattr(pulse, "OUTCOMES_CAP", 3)
    for i in range(5):
        assert client.post(
            "/pulse/outcome", json={"card_id": f"c{i}", "action": "dismiss"}
        ).status_code == 200
    entries = _entries(fake_home)
    assert len(entries) == 3
    assert [e["card_id"] for e in entries] == ["c2", "c3", "c4"]


def test_corrupt_store_is_replaced_not_fatal(client, fake_home):
    path = _store_path(fake_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    resp = client.post(
        "/pulse/outcome", json={"card_id": "x", "action": "done"}
    )
    assert resp.status_code == 200
    assert [e["card_id"] for e in _entries(fake_home)] == ["x"]


def test_store_file_is_0600(client, fake_home):
    client.post("/pulse/outcome", json={"card_id": "x", "action": "done"})
    assert (_store_path(fake_home).stat().st_mode & 0o077) == 0


# ── wiring ───────────────────────────────────────────────────────────────────

def test_register_routes_copies_outcome_flat():
    from fastapi import APIRouter

    target = APIRouter()
    pulse.register_routes(target)
    posts = {
        (r.path, m)
        for r in target.routes
        for m in (r.methods or set())
    }
    assert ("/pulse/outcome", "POST") in posts
    assert ("/pulse", "GET") in posts
