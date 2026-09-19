"""A run must not inherit the last one's faults (log 4.x).

Two halves: `start_new_run` empties what described the previous run while
keeping the accounts, tariffs and wear history the next run needs, and the
counters behind the "Needs attention" panel only see events from the current
run even when older rows are still in the file.
"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import db
from app.main import app


def _login(user="admin", pw="admin123"):
    client = TestClient(app, follow_redirects=False)
    assert client.post("/api/auth/login", json={"username": user, "password": pw}).status_code == 200
    return client


def _iso(offset_s: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat()


def _gap(detected_at: str) -> None:
    with db._lock, db._conn:
        db._conn.execute("INSERT INTO sequence_gaps (expected, received, missing, detected_at) "
                         "VALUES (1, 2, 1, ?)", (detected_at,))


def _unprocessed_event(event_id: str, received_at: str) -> None:
    with db._lock, db._conn:
        db._conn.execute("INSERT INTO events (event_id, received_at, payload, processed) "
                         "VALUES (?, ?, '{}', 0)", (event_id, received_at))


def test_start_new_run_clears_the_run_but_keeps_accounts_and_wear():
    db.sync_component_wear("gate7", "BarrierGate", 12, 0)
    _gap(_iso(-5))
    users_before = db.query("SELECT COUNT(*) AS n FROM dashboard_users")[0]["n"]

    removed = db.start_new_run()

    assert removed["sequence_gaps"] >= 1
    assert db.query("SELECT COUNT(*) AS n FROM sequence_gaps")[0]["n"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM dashboard_users")[0]["n"] == users_before
    assert db.query("SELECT cycle_count AS n FROM component_wear WHERE name = 'gate7'")[0]["n"] == 12


def test_counters_ignore_rows_from_before_this_run():
    db.start_new_run()
    run = db.run_started_at()
    _gap(_iso(-3600))                                   # last night
    _unprocessed_event("stale-event", _iso(-3600))
    assert db.counters()["sequence_gaps"] == 0
    assert db.counters()["unprocessed_events"] == 0

    _gap(_iso(1))                                       # this run
    _unprocessed_event("live-event", _iso(1))
    assert db.counters()["sequence_gaps"] == 1
    assert db.counters()["unprocessed_events"] == 1

    stats = _login().get("/api/stats").json()
    assert stats["sequence_gaps"] == 1 and stats["unprocessed_events"] == 1
    assert db.run_started_at() == run                   # reading it never moves the window
