"""portal_locate_phone: agent-triggered FCM locate + fresh-fix polling.

The fcm_notifier import is faked at the seams: handler-level tests replace the
thin ``_send_locate_ping`` indirection, and ping-level tests inject a fake
``hermes_cli.fcm_notifier`` module into ``sys.modules`` (the plugin normally
imports it in-process inside the gateway).
"""

import json
import sys
import types
from datetime import datetime, timezone

import pytest

from portal import phone


@pytest.fixture(autouse=True)
def profile_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _loc(ts, **extra):
    record = {"lat": 33.77, "lng": -84.4, "timestamp": ts}
    record.update(extra)
    return record


# ------------------------------------------------------------- handler level

def test_locate_error_when_opted_out(profile_home):
    out = json.loads(phone._handle_portal_locate_phone({}))
    assert "error" in out and "opt-in" in out["error"]


def test_locate_sends_ping_with_expected_payload_and_returns_fresh_record(
        profile_home, monkeypatch):
    phone.set_opt_in("location", True)
    captured = {}

    def fake_ping(profile, payload):
        captured["profile"] = profile
        captured["payload"] = payload
        # The phone answers: a fix newer than requested_at lands in the store.
        phone.ingest("location", {"records": [_loc("2099-01-01T00:00:00Z", label="fresh")]})
        return {"ok": True, "devices": 1}

    monkeypatch.setattr(phone, "_send_locate_ping", fake_ping)
    out = json.loads(phone._handle_portal_locate_phone({}))
    assert out["ok"] is True and out["stale"] is False
    assert out["record"]["label"] == "fresh"
    assert captured["payload"]["kind"] == "portal_locate"
    # requested_at: strict second-precision ISO-8601 UTC
    datetime.strptime(captured["payload"]["requested_at"], "%Y-%m-%dT%H:%M:%SZ")
    assert set(captured["payload"]) == {"kind", "requested_at"}
    assert captured["profile"]  # a profile name was resolved and passed down


def test_locate_explicit_profile_arg_is_passed_through(profile_home, monkeypatch):
    phone.set_opt_in("location", True)
    seen = {}
    monkeypatch.setattr(phone, "_send_locate_ping",
                        lambda p, pl: seen.update(profile=p) or {"ok": True, "devices": 1})
    monkeypatch.setattr(phone, "_find_fresh_location", lambda _ts: {"lat": 1.0, "lng": 2.0})
    out = json.loads(phone._handle_portal_locate_phone({"profile": "work"}))
    assert out["ok"] is True and seen["profile"] == "work"


def test_locate_timeout_returns_last_known_with_stale_flag(profile_home, monkeypatch):
    phone.set_opt_in("location", True)
    phone.ingest("location", {"records": [_loc("2020-01-01T00:00:00Z", label="old")]})
    monkeypatch.setattr(phone, "_send_locate_ping", lambda p, pl: {"ok": True, "devices": 1})
    monkeypatch.setattr(phone, "LOCATE_TIMEOUT_S", 0.0)  # no fresh fix arrives
    out = json.loads(phone._handle_portal_locate_phone({}))
    assert out["ok"] is True and out["stale"] is True
    assert out["record"]["label"] == "old"


def test_locate_timeout_with_no_records_at_all(profile_home, monkeypatch):
    phone.set_opt_in("location", True)
    monkeypatch.setattr(phone, "_send_locate_ping", lambda p, pl: {"ok": True, "devices": 1})
    monkeypatch.setattr(phone, "LOCATE_TIMEOUT_S", 0.0)
    out = json.loads(phone._handle_portal_locate_phone({}))
    assert out["stale"] is True and out["record"] is None
    assert "no location has ever been synced" in out["note"]


def test_locate_ping_failure_becomes_structured_error(profile_home, monkeypatch):
    phone.set_opt_in("location", True)
    monkeypatch.setattr(phone, "_send_locate_ping",
                        lambda p, pl: {"ok": False, "error": "no FCM-registered device"})
    out = json.loads(phone._handle_portal_locate_phone({}))
    assert out["ok"] is False
    assert "no FCM-registered device" in out["error"]
    assert out["requested_at"]


def test_locate_never_raises_even_if_ping_explodes(profile_home, monkeypatch):
    phone.set_opt_in("location", True)

    def boom(profile, payload):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(phone, "_send_locate_ping", boom)
    out = json.loads(phone._handle_portal_locate_phone({}))
    assert "error" in out and "kaboom" in out["error"]


# ---------------------------------------------------------------- ping level

@pytest.fixture()
def fake_fcm(monkeypatch):
    """Inject a fake hermes_cli.fcm_notifier into sys.modules (import seam)."""
    calls = []
    mod = types.SimpleNamespace(
        _load_registrations=lambda: {
            "tok-work": {"encryption_key": "a2V5LWZvci1kZXZpY2U=", "profile": "work"},
            "tok-other": {"encryption_key": "a2V5", "profile": "other"},
            "tok-nokey": {"profile": "work"},
        },
        _send_one=lambda token, key, payload: calls.append((token, key, payload)) or True,
    )
    pkg = types.ModuleType("hermes_cli")
    pkg.fcm_notifier = mod
    monkeypatch.setitem(sys.modules, "hermes_cli", pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.fcm_notifier", mod)
    return calls


def test_send_locate_ping_filters_by_profile_and_sends(fake_fcm):
    result = phone._send_locate_ping("work", {"kind": "portal_locate", "requested_at": "x"})
    assert result == {"ok": True, "devices": 1}
    assert [c[0] for c in fake_fcm] == ["tok-work"]
    assert fake_fcm[0][1] == "a2V5LWZvci1kZXZpY2U="  # device's registered key
    assert fake_fcm[0][2]["kind"] == "portal_locate"


def test_send_locate_ping_unknown_profile_is_structured_error(fake_fcm):
    result = phone._send_locate_ping("nobody", {"kind": "portal_locate"})
    assert result["ok"] is False and "nobody" in result["error"]


def test_send_locate_ping_reports_send_failure(fake_fcm, monkeypatch):
    # fcm_notifier._send_one returns False when no service account is configured.
    sys.modules["hermes_cli.fcm_notifier"]._send_one = lambda *a: False
    result = phone._send_locate_ping("work", {"kind": "portal_locate"})
    assert result["ok"] is False and "service account" in result["error"]


def test_send_locate_ping_without_fcm_module_is_clear_error(monkeypatch):
    # The bare test venv has no hermes_cli; the real import must fail cleanly.
    monkeypatch.delitem(sys.modules, "hermes_cli", raising=False)
    monkeypatch.delitem(sys.modules, "hermes_cli.fcm_notifier", raising=False)
    try:
        import hermes_cli  # noqa: F401
        pytest.skip("hermes_cli is importable in this venv; cannot exercise the missing-module path")
    except ImportError:
        pass
    result = phone._send_locate_ping("default", {"kind": "portal_locate"})
    assert result["ok"] is False and "fcm_notifier" in result["error"]
