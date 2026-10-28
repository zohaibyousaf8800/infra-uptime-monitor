"""
monitor.py — checking engine, incident logic and SQLite storage for infra-uptime-monitor.

The monitor keeps a registry of targets loaded from config.json, performs an
HTTP GET for each target on its own interval from a background thread, stores
every check in SQLite, and opens an incident after 3 consecutive failed
checks (closing it on the next successful check).

Only the Python standard library is used here (sqlite3, urllib, threading).
"""

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

FAILURE_THRESHOLD = 3          # consecutive failures that open an incident
HTTP_TIMEOUT_SECONDS = 5       # per-request timeout for a single check
USER_AGENT = "infra-uptime-monitor/1.0"


def utcnow():
    """Current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


def iso(dt):
    """Format a datetime as an ISO-8601 string in UTC."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,
    url              TEXT NOT NULL,
    expected_status  INTEGER NOT NULL DEFAULT 200,
    interval_seconds INTEGER NOT NULL DEFAULT 60
);
CREATE TABLE IF NOT EXISTS checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id   INTEGER NOT NULL REFERENCES targets(id),
    checked_at  TEXT NOT NULL,
    status_code INTEGER,
    response_ms INTEGER,
    is_up       INTEGER NOT NULL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks_target_time ON checks(target_id, checked_at);
CREATE TABLE IF NOT EXISTS incidents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id       INTEGER NOT NULL REFERENCES targets(id),
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    duration_seconds REAL
);
CREATE INDEX IF NOT EXISTS idx_incidents_target_open ON incidents(target_id, ended_at);
"""


def connect(db_path):
    """Open a SQLite connection with row access by column name."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path):
    """Create the database file and schema if they do not exist yet."""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def sync_targets(db_path, targets):
    """Insert or update the targets from config; returns list of target dicts with ids.

    Targets are keyed by name so ids stay stable across restarts. Existing
    check/incident history is preserved when a target's settings change.
    """
    conn = connect(db_path)
    try:
        for t in targets:
            conn.execute(
                """
                INSERT INTO targets (name, url, expected_status, interval_seconds)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    url = excluded.url,
                    expected_status = excluded.expected_status,
                    interval_seconds = excluded.interval_seconds
                """,
                (t["name"], t["url"], t.get("expected_status", 200),
                 t.get("interval_seconds", 60)),
            )
        conn.commit()
        rows = conn.execute("SELECT * FROM targets ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def record_check(db_path, target_id, status_code, response_ms, is_up, error=None, when=None):
    """Store one check result and apply the incident rules.

    Incident rules:
    - FAILURE_THRESHOLD consecutive failed checks open an incident.
    - The next successful check closes the open incident (if any) and sets
      its duration.
    - At most one open incident per target at any time.
    """
    when = when or utcnow()
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO checks (target_id, checked_at, status_code, response_ms, is_up, error)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (target_id, iso(when), status_code, response_ms, 1 if is_up else 0, error),
        )
        if is_up:
            _close_open_incident(conn, target_id, when)
        else:
            if _consecutive_failures(conn, target_id) >= FAILURE_THRESHOLD:
                _open_incident_if_none(conn, target_id, when)
        conn.commit()
    finally:
        conn.close()


def _consecutive_failures(conn, target_id):
    """How many of the most recent checks (newest first) are failures, in a row."""
    rows = conn.execute(
        "SELECT is_up FROM checks WHERE target_id = ? ORDER BY id DESC LIMIT ?",
        (target_id, FAILURE_THRESHOLD),
    ).fetchall()
    count = 0
    for row in rows:
        if row["is_up"]:
            break
        count += 1
    return count


def _open_incident(conn, target_id):
    row = conn.execute(
        "SELECT * FROM incidents WHERE target_id = ? AND ended_at IS NULL"
        " ORDER BY id DESC LIMIT 1",
        (target_id,),
    ).fetchone()
    return row


def _open_incident_if_none(conn, target_id, when):
    if _open_incident(conn, target_id) is None:
        conn.execute(
            "INSERT INTO incidents (target_id, started_at) VALUES (?, ?)",
            (target_id, iso(when)),
        )


def _close_open_incident(conn, target_id, when):
    row = _open_incident(conn, target_id)
    if row is not None:
        started = datetime.fromisoformat(row["started_at"])
        duration = (when - started).total_seconds()
        conn.execute(
            "UPDATE incidents SET ended_at = ?, duration_seconds = ? WHERE id = ?",
            (iso(when), duration, row["id"]),
        )


# --------------------------------------------------------------------------
# Queries used by the API / status page
# --------------------------------------------------------------------------

def uptime_percent(db_path, target_id, hours=24, now=None):
    """Percentage of successful checks in the last `hours` hours.

    Returns 100.0 when there are no checks in the window (nothing has been
    observed to fail yet), rounded to 2 decimals otherwise.
    """
    now = now or utcnow()
    since = iso(now - timedelta(hours=hours))
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(is_up), 0) AS ups"
            " FROM checks WHERE target_id = ? AND checked_at >= ?",
            (target_id, since),
        ).fetchone()
    finally:
        conn.close()
    total = row["total"]
    if total == 0:
        return 100.0
    return round(100.0 * row["ups"] / total, 2)


def latest_check(db_path, target_id):
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM checks WHERE target_id = ? ORDER BY id DESC LIMIT 1",
            (target_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def target_status(db_path, target, now=None):
    """A target dict enriched with live status fields for the API."""
    last = latest_check(db_path, target["id"])
    if last is None:
        status, last_checked, last_ms = "unknown", None, None
    else:
        status = "up" if last["is_up"] else "down"
        last_checked, last_ms = last["checked_at"], last["response_ms"]
    return {
        "id": target["id"],
        "name": target["name"],
        "url": target["url"],
        "expected_status": target["expected_status"],
        "interval_seconds": target["interval_seconds"],
        "status": status,
        "last_checked_at": last_checked,
        "last_response_ms": last_ms,
        "uptime_24h": uptime_percent(db_path, target["id"], hours=24, now=now),
    }


def list_targets(db_path):
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM targets ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def check_history(db_path, target_id, limit=50):
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM checks WHERE target_id = ? ORDER BY id DESC LIMIT ?",
            (target_id, limit),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["is_up"] = bool(d["is_up"])
            result.append(d)
        return result
    finally:
        conn.close()


def list_incidents(db_path):
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT i.*, t.name AS target_name
            FROM incidents i JOIN targets t ON t.id = i.target_id
            ORDER BY (i.ended_at IS NULL) DESC, i.started_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def summary(db_path, now=None):
    """Aggregate figures for /api/summary."""
    now = now or utcnow()
    targets = [target_status(db_path, t, now=now) for t in list_targets(db_path)]
    since = iso(now - timedelta(hours=24))
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(is_up), 0) AS ups FROM checks"
            " WHERE checked_at >= ?",
            (since,),
        ).fetchone()
        open_incidents = conn.execute(
            "SELECT COUNT(*) AS c FROM incidents WHERE ended_at IS NULL"
        ).fetchone()["c"]
    finally:
        conn.close()
    total, ups = row["total"], row["ups"]
    overall_uptime = 100.0 if total == 0 else round(100.0 * ups / total, 2)
    return {
        "targets_total": len(targets),
        "targets_up": sum(1 for t in targets if t["status"] == "up"),
        "targets_down": sum(1 for t in targets if t["status"] == "down"),
        "targets_unknown": sum(1 for t in targets if t["status"] == "unknown"),
        "total_checks_24h": total,
        "overall_uptime_24h": overall_uptime,
        "open_incidents": open_incidents,
    }


# --------------------------------------------------------------------------
# The actual HTTP check
# --------------------------------------------------------------------------

def perform_check(target):
    """Perform one HTTP GET against a target.

    Returns (status_code, response_ms, is_up, error). Never raises: network
    problems are reported as a failed check with the error text attached.
    """
    req = urllib.request.Request(target["url"], method="GET",
                                 headers={"User-Agent": USER_AGENT})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            resp.read(512)  # a small read proves the body actually flows
            status_code = resp.status
        elapsed_ms = int((time.monotonic() - started) * 1000)
        expected = target.get("expected_status", 200)
        is_up = status_code == expected
        error = None if is_up else f"expected HTTP {expected}, got {status_code}"
        return status_code, elapsed_ms, is_up, error
    except urllib.error.HTTPError as exc:
        # The server answered, just with an error status (4xx/5xx).
        elapsed_ms = int((time.monotonic() - started) * 1000)
        expected = target.get("expected_status", 200)
        is_up = exc.code == expected
        error = None if is_up else f"expected HTTP {expected}, got {exc.code}"
        return exc.code, elapsed_ms, is_up, error
    except Exception as exc:  # DNS, timeout, refused connection, TLS, ...
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return None, elapsed_ms, False, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# Config + background loop
# --------------------------------------------------------------------------

def load_config(config_path):
    with open(config_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    targets = data["targets"]
    for t in targets:
        t.setdefault("expected_status", 200)
        t.setdefault("interval_seconds", 60)
    return targets


class Monitor:
    """Background checker: schedules perform_check per target on its interval."""

    def __init__(self, db_path, targets, check_fn=perform_check):
        self.db_path = str(db_path)
        self.targets = targets            # target dicts including db ids
        self.check_fn = check_fn
        self._stop = threading.Event()
        self._thread = None
        self._next_due = {t["id"]: 0.0 for t in targets}  # due immediately

    def run_once(self, target, when=None):
        """Run a single check for one target and record the result."""
        status_code, response_ms, is_up, error = self.check_fn(target)
        record_check(self.db_path, target["id"], status_code, response_ms,
                     is_up, error, when=when)
        return is_up

    def _loop(self):
        while not self._stop.is_set():
            now_mono = time.monotonic()
            for target in self.targets:
                if self._stop.is_set():
                    break
                if now_mono >= self._next_due[target["id"]]:
                    try:
                        self.run_once(target)
                    except Exception:
                        # A single bad target must never kill the loop.
                        pass
                    self._next_due[target["id"]] = (
                        time.monotonic() + target["interval_seconds"]
                    )
            self._stop.wait(0.25)

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="uptime-checker",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


def bootstrap(db_path, config_path, check_fn=perform_check):
    """Initialise the DB, sync config targets, and return a ready Monitor."""
    db_path = Path(db_path)
    init_db(db_path)
    configured = load_config(config_path)
    targets = sync_targets(db_path, configured)
    return Monitor(db_path, targets, check_fn=check_fn)
