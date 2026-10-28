"""Tests for infra-uptime-monitor: uptime math, incident logic, and the API.

No real network access happens in these tests: the checker is driven with a
stubbed check function, and the API tests use Flask's test client against a
temporary database.
"""

import json
from datetime import timedelta

import pytest

import app as app_module
import monitor as mon

SAMPLE_TARGETS = [
    {"name": "Alpha", "url": "https://alpha.example.com",
     "expected_status": 200, "interval_seconds": 30},
    {"name": "Beta", "url": "https://beta.example.com",
     "expected_status": 200, "interval_seconds": 30},
]


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    mon.init_db(path)
    return path


@pytest.fixture()
def targets(db_path):
    return mon.sync_targets(db_path, SAMPLE_TARGETS)


def scripted_check(results):
    """Build a fake check function that replays a list of (code, ms, is_up, err)."""
    queue = list(results)

    def _check(target):
        if not queue:
            raise AssertionError("scripted check ran out of results")
        return queue.pop(0)

    return _check


DOWN = (None, 15, False, "ConnectionError: stubbed failure")
UP = (200, 42, True, None)


def run_checks(db_path, targets, target, results):
    monitor = mon.Monitor(db_path, targets, check_fn=scripted_check(results))
    base = mon.utcnow()
    for i in range(len(results)):
        monitor.run_once(target, when=base + timedelta(seconds=i * 10))
    return monitor


# ---------------------------------------------------------------- uptime %

def test_uptime_empty_window_is_100(db_path, targets):
    assert mon.uptime_percent(db_path, targets[0]["id"]) == 100.0


def test_uptime_percentage_math(db_path, targets):
    t = targets[0]
    now = mon.utcnow()
    for i, is_up in enumerate([True, True, True, False]):  # 3 of 4 up -> 75%
        mon.record_check(db_path, t["id"], 200 if is_up else None, 10,
                         is_up, None if is_up else "boom",
                         when=now - timedelta(minutes=30 - i))
    assert mon.uptime_percent(db_path, t["id"], now=now + timedelta(minutes=1)) == 75.0


def test_uptime_ignores_checks_outside_window(db_path, targets):
    t = targets[0]
    now = mon.utcnow()
    # An old failure (48 h ago) must not count toward the 24 h figure.
    mon.record_check(db_path, t["id"], None, 10, False, "old",
                     when=now - timedelta(hours=48))
    mon.record_check(db_path, t["id"], 200, 10, True, None,
                     when=now - timedelta(minutes=5))
    assert mon.uptime_percent(db_path, t["id"], now=now) == 100.0


def test_uptime_rounding(db_path, targets):
    t = targets[0]
    now = mon.utcnow()
    for i, is_up in enumerate([True, False, False]):  # 1 of 3 -> 33.33%
        mon.record_check(db_path, t["id"], 200 if is_up else None, 5,
                         is_up, None, when=now - timedelta(minutes=10 - i))
    assert mon.uptime_percent(db_path, t["id"], now=now) == 33.33


# -------------------------------------------------------------- incidents

def test_incident_opens_after_three_consecutive_failures(db_path, targets):
    t = targets[0]
    run_checks(db_path, targets, t, [DOWN, DOWN])
    assert mon.list_incidents(db_path) == []

    run_checks(db_path, targets, t, [DOWN])
    incidents = mon.list_incidents(db_path)
    assert len(incidents) == 1
    assert incidents[0]["target_id"] == t["id"]
    assert incidents[0]["ended_at"] is None


def test_incident_not_opened_by_non_consecutive_failures(db_path, targets):
    t = targets[0]
    run_checks(db_path, targets, t, [DOWN, DOWN, UP, DOWN, DOWN])
    assert mon.list_incidents(db_path) == []


def test_no_duplicate_incident_while_one_is_open(db_path, targets):
    t = targets[0]
    run_checks(db_path, targets, t, [DOWN] * 7)
    incidents = mon.list_incidents(db_path)
    assert len(incidents) == 1
    assert incidents[0]["ended_at"] is None


def test_incident_closes_on_recovery_with_duration(db_path, targets):
    t = targets[0]
    base = mon.utcnow()
    monitor = mon.Monitor(db_path, targets,
                          check_fn=scripted_check([DOWN, DOWN, DOWN, UP]))
    for i in range(4):
        monitor.run_once(t, when=base + timedelta(seconds=i * 10))
    incidents = mon.list_incidents(db_path)
    assert len(incidents) == 1
    inc = incidents[0]
    assert inc["ended_at"] is not None
    # Opened at the 3rd failure (t+20s), closed by the success at t+30s.
    # Timestamps are stored at whole-second precision, so allow ~1 s slack.
    assert inc["duration_seconds"] == pytest.approx(10.0, abs=1.0)


def test_second_outage_opens_a_new_incident(db_path, targets):
    t = targets[0]
    run_checks(db_path, targets, t, [DOWN, DOWN, DOWN, UP, DOWN, DOWN, DOWN])
    incidents = mon.list_incidents(db_path)
    assert len(incidents) == 2
    assert incidents[0]["ended_at"] is None       # newest first: open one first
    assert incidents[1]["ended_at"] is not None


# -------------------------------------------------------------------- API

@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "api.db"
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"targets": SAMPLE_TARGETS}))
    flask_app = app_module.create_app(db_path=db, config_path=cfg)
    flask_app.config.update(TESTING=True)
    return flask_app.test_client(), db


def test_api_targets_shape_and_status(client):
    c, db = client
    resp = c.get("/api/targets")
    assert resp.status_code == 200
    data = resp.get_json()
    assert [t["name"] for t in data] == ["Alpha", "Beta"]
    for t in data:
        assert t["status"] == "unknown"
        assert t["uptime_24h"] == 100.0
        assert t["last_checked_at"] is None

    alpha_id = data[0]["id"]
    mon.record_check(db, alpha_id, 200, 33, True, None)
    data = c.get("/api/targets").get_json()
    alpha = next(t for t in data if t["id"] == alpha_id)
    assert alpha["status"] == "up"
    assert alpha["last_response_ms"] == 33
    assert alpha["last_checked_at"] is not None


def test_api_history_newest_first_and_limit(client):
    c, db = client
    alpha_id = c.get("/api/targets").get_json()[0]["id"]
    for i in range(5):
        mon.record_check(db, alpha_id, 200, 100 + i, True, None)
    history = c.get(f"/api/targets/{alpha_id}/history").get_json()
    assert len(history) == 5
    assert [h["response_ms"] for h in history] == [104, 103, 102, 101, 100]
    limited = c.get(f"/api/targets/{alpha_id}/history?limit=2").get_json()
    assert len(limited) == 2
    assert c.get("/api/targets/9999/history").status_code == 404


def test_api_incidents_open_first(client):
    c, db = client
    targets = mon.list_targets(db)
    monitor = mon.Monitor(db, targets,
                          check_fn=scripted_check([DOWN, DOWN, DOWN, UP,
                                                   DOWN, DOWN, DOWN]))
    alpha = targets[0]
    for _ in range(7):
        monitor.run_once(alpha)
    incidents = c.get("/api/incidents").get_json()
    assert len(incidents) == 2
    assert incidents[0]["open"] is True
    assert incidents[1]["open"] is False
    assert incidents[0]["target_name"] == "Alpha"


def test_api_summary_math(client):
    c, db = client
    targets = mon.list_targets(db)
    alpha, beta = targets
    mon.record_check(db, alpha["id"], 200, 10, True, None)
    mon.record_check(db, alpha["id"], 200, 10, True, None)
    mon.record_check(db, beta["id"], None, 10, False, "down")
    s = c.get("/api/summary").get_json()
    assert s["targets_total"] == 2
    assert s["targets_up"] == 1
    assert s["targets_down"] == 1
    assert s["targets_unknown"] == 0
    assert s["total_checks_24h"] == 3
    assert s["overall_uptime_24h"] == 66.67
    assert s["open_incidents"] == 0


def test_status_page_renders_targets(client):
    c, _db = client
    resp = c.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Alpha" in html and "Beta" in html
    assert "No open incidents." in html
