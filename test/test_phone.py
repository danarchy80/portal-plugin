"""Task 3: opt-in phone-data sinks + tools.

Integration shape mirrors the host: the plugin package is imported under the
name ``portal`` (see conftest.py) and exercised through a FastAPI app built
exactly like ``plugin_host_child.op_asgi`` (``FastAPI()`` +
``include_router(router)``), i.e. routes at ``/phone/<class>``.
"""

import base64
import json

import pytest

from portal import phone
from portal.portal import register, router


@pytest.fixture(autouse=True)
def profile_home(tmp_path, monkeypatch):
    """Every test gets an isolated per-profile HERMES_HOME (same fallback
    get_hermes_home() uses inside the plugin host)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(router)  # the portal router, as op_asgi mounts it
    return TestClient(app)


def _record(**extra):
    record = {
        "lat": 33.77,
        "lng": -84.4,
        "timestamp": "2026-11-17T12:00:00Z",
        "enc": base64.b64encode(b"device-key ciphertext, host cannot read").decode(),
    }
    record.update(extra)
    return record


# ---------------------------------------------------------------- opt-in

def test_opt_in_is_off_by_default(profile_home):
    assert phone.is_opted_in("location") is False


def test_posting_location_with_opt_in_off_is_refused(client):
    resp = client.post("/phone/location", json={"records": [_record()]})
    assert resp.status_code == 403
    assert resp.json()["ok"] is False
    assert "opt-in" in resp.json()["error"]


def test_opt_in_flag_is_per_class(profile_home):
    phone.set_opt_in("location", True)
    assert phone.is_opted_in("location") is True
    assert phone.is_opted_in("calendar") is False
    phone.set_opt_in("location", False)
    assert phone.is_opted_in("location") is False


def test_calendar_post_refused_until_calendar_opts_in(client, profile_home):
    event = {"id": "e1", "title": "Standup", "start": "2026-11-18T15:00:00Z"}
    assert client.post("/phone/calendar", json={"records": [event]}).status_code == 403
    phone.set_opt_in("calendar", True)
    resp = client.post("/phone/calendar", json={"records": [event]})
    assert resp.status_code == 201


# ---------------------------------------------------------------- sinks

def test_posting_with_opt_in_on_persists(client, profile_home):
    phone.set_opt_in("location", True)
    resp = client.post("/phone/location", json={"class": "location", "records": [_record()]})
    assert resp.status_code == 201
    assert resp.json()["stored"] == 1
    store_file = profile_home / "plugins" / "portal" / "phone" / "location.json"
    assert store_file.is_file()
    assert store_file.stat().st_mode & 0o077 == 0  # owner-only (0600)
    on_disk = json.loads(store_file.read_text())
    assert on_disk["records"][0]["lat"] == 33.77
    assert on_disk["records"][0]["enc"]  # opaque blob stored verbatim


def test_unencrypted_plaintext_shape_still_strictly_validated(client, profile_home):
    """No encrypted blob and no valid timestamp -> refused, never stored."""
    phone.set_opt_in("location", True)
    bad = {"lat": 33.77, "lng": -84.4}  # missing timestamp
    resp = client.post("/phone/location", json={"records": [bad]})
    assert resp.status_code == 400
    assert not (profile_home / "plugins" / "portal" / "phone" / "location.json").exists()


def test_unknown_fields_and_bad_base64_enc_rejected(client, profile_home):
    phone.set_opt_in("location", True)
    resp = client.post("/phone/location", json={"records": [{**_record(), "raw": "x" * 999}]})
    assert resp.status_code == 400
    resp = client.post("/phone/location", json={"records": [_record(enc="not base64!!")]})
    assert resp.status_code == 400
    assert "base64" in resp.json()["error"]


def test_oversized_payload_rejected(client, profile_home):
    phone.set_opt_in("location", True)
    huge = _record(enc=base64.b64encode(b"\x00" * (phone.MAX_BODY_BYTES + 10)).decode())
    body = json.dumps({"records": [huge]}).encode()
    assert len(body) > phone.MAX_BODY_BYTES
    resp = client.post("/phone/location", content=body,
                       headers={"content-type": "application/json"})
    assert resp.status_code == 413


def test_location_out_of_range_rejected(client, profile_home):
    phone.set_opt_in("location", True)
    resp = client.post("/phone/location", json={"records": [_record(lat=91.0)]})
    assert resp.status_code == 400


# ---------------------------------------------------------------- tools

def test_portal_location_reads_back_what_was_posted(client, profile_home):
    phone.set_opt_in("location", True)
    resp = client.post("/phone/location", json={"records": [_record(label="Home")]})
    assert resp.status_code == 201
    out = json.loads(phone._handle_portal_location({}))
    assert out["ok"] is True
    assert out["latest"]["lat"] == 33.77
    assert out["latest"]["label"] == "Home"


def test_portal_location_error_when_opted_out(profile_home):
    out = json.loads(phone._handle_portal_location({}))
    assert "error" in out and "opt-in" in out["error"]


def test_portal_contacts_returns_empty_before_any_data(profile_home):
    phone.set_opt_in("contacts", True)
    out = json.loads(phone._handle_portal_contacts({}))
    assert out["ok"] is True
    assert out["contacts"] == []
    assert out["count"] == 0


def test_portal_calendar_upcoming_filter_and_query(profile_home):
    phone.set_opt_in("calendar", True)
    phone.set_opt_in("contacts", True)
    phone.ingest("calendar", {"records": [
        {"id": "old", "title": "Yesterday", "start": "2026-01-01T10:00:00Z", "end": "2026-01-01T11:00:00Z"},
        {"id": "new", "title": "Tomorrow", "start": "2099-01-01T10:00:00Z", "end": "2099-01-01T11:00:00Z"},
    ]})
    upcoming = json.loads(phone._handle_portal_calendar({}))
    assert [e["id"] for e in upcoming["events"]] == ["new"]
    everything = json.loads(phone._handle_portal_calendar({"all_events": True}))
    assert everything["count"] == 2

    phone.ingest("contacts", {"records": [
        {"name": "Alice Chen", "note": "dentist"},
        {"name": "Bob"},
    ]})
    assert json.loads(phone._handle_portal_contacts({"query": "alice"}))["count"] == 1
    assert json.loads(phone._handle_portal_contacts({"query": "nobody"}))["contacts"] == []


# ---------------------------------------------------------------- wiring

class _Ctx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = (toolset, schema, handler)


def test_register_registers_the_phone_tools():
    ctx = _Ctx()
    assert register(ctx) == {}
    assert {"portal_location", "portal_calendar", "portal_contacts",
            "portal_health", "portal_locate_phone"} <= set(ctx.tools)
    for name, (toolset, schema, handler) in ctx.tools.items():
        assert toolset == "portal", name
        assert schema["name"] and callable(handler)


def _mounted_routes():
    """(path -> methods) for the plugin router's concrete routes. Routes
    are registered flat (no include_router composition; see portal.py)."""
    from fastapi.routing import APIRoute

    return {r.path: r.methods for r in router.routes if isinstance(r, APIRoute)}


def test_router_exposes_the_phone_sinks():
    routes = _mounted_routes()
    phone_paths = {p for p in routes if p.startswith("/phone/")}
    assert phone_paths == {"/phone/location", "/phone/calendar",
                           "/phone/contacts", "/phone/health"}
    for path in phone_paths:
        assert routes[path] == {"POST"}


def test_health_route_still_mounted():
    assert "/health" in _mounted_routes()


# ---------------------------------------------------------------- health

def _health(**extra):
    record = {
        "type": "steps",
        "value": 8123,
        "unit": "count",
        "timestamp": "2026-11-21T12:00:00Z",
        "source": "health_connect",
    }
    record.update(extra)
    return record


def test_health_post_refused_until_health_opts_in(client, profile_home):
    resp = client.post("/phone/health", json={"records": [_health()]})
    assert resp.status_code == 403
    assert "opt-in" in resp.json()["error"]


def test_health_round_trip(client, profile_home):
    phone.set_opt_in("health", True)
    records = [
        _health(),
        _health(type="sleep_duration", value=462, unit="min",
                timestamp="2026-11-21T02:00:00Z", end_timestamp="2026-11-21T09:42:00Z"),
    ]
    resp = client.post("/phone/health", json={"class": "health", "records": records})
    assert resp.status_code == 201
    assert resp.json()["stored"] == 2
    store_file = profile_home / "plugins" / "portal" / "phone" / "health.json"
    assert store_file.is_file()
    assert store_file.stat().st_mode & 0o077 == 0  # owner-only (0600)
    on_disk = json.loads(store_file.read_text())
    assert on_disk["records"][0]["value"] == 8123
    assert on_disk["records"][1]["end_timestamp"] == "2026-11-21T09:42:00Z"


def test_health_bad_type_rejected(client, profile_home):
    phone.set_opt_in("health", True)
    resp = client.post("/phone/health", json={"records": [_health(type="glucose")]})
    assert resp.status_code == 400
    assert "type" in resp.json()["error"]


def test_health_non_numeric_value_rejected(client, profile_home):
    phone.set_opt_in("health", True)
    resp = client.post("/phone/health", json={"records": [_health(value="many")]})
    assert resp.status_code == 400
    assert "value" in resp.json()["error"]
    resp = client.post("/phone/health", json={"records": [_health(value=True)]})
    assert resp.status_code == 400


def test_health_value_out_of_range_rejected(client, profile_home):
    phone.set_opt_in("health", True)
    resp = client.post("/phone/health", json={"records": [_health(value=10_000_001)]})
    assert resp.status_code == 400
    resp = client.post("/phone/health", json={"records": [_health(value=-1)]})
    assert resp.status_code == 400


def test_health_fractional_second_timestamp_rejected(client, profile_home):
    phone.set_opt_in("health", True)
    resp = client.post("/phone/health",
                       json={"records": [_health(timestamp="2026-11-21T12:00:00.500Z")]})
    assert resp.status_code == 400
    assert "timestamp" in resp.json()["error"]


def test_health_end_before_start_rejected(client, profile_home):
    phone.set_opt_in("health", True)
    resp = client.post("/phone/health",
                       json={"records": [_health(type="sleep_duration", unit="min",
                                                 timestamp="2026-11-21T09:00:00Z",
                                                 end_timestamp="2026-11-21T02:00:00Z")]})
    assert resp.status_code == 400
    assert "end_timestamp" in resp.json()["error"]


def test_health_wrong_source_and_missing_unit_rejected(client, profile_home):
    phone.set_opt_in("health", True)
    resp = client.post("/phone/health", json={"records": [_health(source="google_fit")]})
    assert resp.status_code == 400
    resp = client.post("/phone/health", json={"records": [_health(unit="")]})
    assert resp.status_code == 400


def test_portal_health_error_when_opted_out(profile_home):
    out = json.loads(phone._handle_portal_health({}))
    assert "error" in out and "opt-in" in out["error"]


def test_portal_health_reads_back_and_filters_by_type(client, profile_home):
    phone.set_opt_in("health", True)
    phone.ingest("health", {"records": [
        _health(),
        _health(type="heart_rate", value=61, unit="bpm", timestamp="2026-11-21T12:05:00Z"),
        _health(type="steps", value=9500, timestamp="2026-11-21T13:00:00Z"),
    ]})
    everything = json.loads(phone._handle_portal_health({}))
    assert everything["ok"] is True and everything["count"] == 3
    assert everything["updated_at"]
    steps = json.loads(phone._handle_portal_health({"type": "steps"}))
    assert steps["type"] == "steps"
    assert [r["value"] for r in steps["records"]] == [8123.0, 9500.0]
    bad = json.loads(phone._handle_portal_health({"type": "glucose"}))
    assert "error" in bad
