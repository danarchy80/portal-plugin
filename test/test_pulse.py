"""Task 2: PulseCard model, deterministic build_cards, /pulse route, pulse_get tool."""

import asyncio
import json
import sqlite3
import time
from dataclasses import fields

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import portal.pulse as pulse


def _make_home(tmp_path, *, rows):
    """Fake profile home: state.db with sessions + cron/jobs.json."""
    (tmp_path / "cron").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(tmp_path / "state.db")
    conn.execute(
        "CREATE TABLE sessions ("
        "id TEXT PRIMARY KEY, started_at REAL, ended_at REAL, "
        "title TEXT, source TEXT, model_config TEXT)"
    )
    conn.executemany(
        "INSERT INTO sessions (id, started_at, ended_at, title, source, model_config) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    now = time.time()
    jobs = {
        "jobs": [
            {"id": "ok", "name": "daily-brief", "enabled": True, "last_status": "success"},
            {"id": "broken", "name": "feed-watch", "enabled": True, "last_status": "failed",
             "last_error": "boom"},
            {"id": "off", "name": "parked", "enabled": False, "last_status": None},
        ],
        "updated_at": now,
    }
    (tmp_path / "cron" / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
    return tmp_path


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    today = time.time()
    yesterday = today - 86400 * 2
    home = _make_home(tmp_path, rows=[
        ("s1", today, today, "today's session", "cli", None),
        ("s2", yesterday, yesterday, "old session", "cli", None),
    ])
    monkeypatch.setattr(pulse, "resolve_home", lambda profile=None: home)
    return home


FIELDS = {"id", "title", "body", "kind", "created_at", "actionable"}


def test_pulse_card_fields():
    assert {f.name for f in fields(pulse.PulseCard)} == FIELDS


def test_build_cards_shape(fake_home):
    cards = asyncio.run(pulse.build_cards(None))
    assert cards, "expected at least the brief card"
    for c in cards:
        assert isinstance(c, pulse.PulseCard)
        assert c.id and c.title
        assert c.body
        assert c.created_at
        assert isinstance(c.actionable, bool)


def test_build_cards_deterministic_counts(fake_home):
    cards = {c.id: c for c in asyncio.run(pulse.build_cards(None))}
    brief = cards["brief"]
    # Exactly one session started today; 2 enabled cron jobs.
    assert "1 session" in brief.body
    assert "2 cron jobs enabled" in brief.body
    # The failed job raises its own attention card naming it.
    attention = cards["cron-failures"]
    assert attention.kind == "cron_attention"
    assert "feed-watch" in attention.body


def test_pulse_route_serializes_cards(fake_home):
    app = FastAPI()
    app.include_router(pulse.router)
    resp = TestClient(app).get("/pulse")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["cards"]
    for card in payload["cards"]:
        assert set(card) == FIELDS


def test_pulse_get_tool_registered_and_returns_json(fake_home):
    registered = {}

    class FakeCtx:
        def register_tool(self, name, **kwargs):
            registered[name] = kwargs

    result = pulse.register_tools(FakeCtx())
    assert result == ["pulse_get"]
    assert registered["pulse_get"]["is_async"] is True
    handler = registered["pulse_get"]["handler"]
    payload = json.loads(asyncio.run(handler({})))
    assert payload["ok"] is True
    assert payload["cards"][0]["id"]
