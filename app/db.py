"""Durable SQLite event log and reporting store.

``app/state.py`` stays the hot path: it is in-memory, lock-protected and fast
enough to answer a webhook inside a millisecond. This module sits beside it and
gives us the things memory cannot:

* an append-only record of every event, so nothing is lost to a crash,
* searchable history for the operator dashboard, which Level 1 requires
  ("Log car arrivals/parking time/departure/charges in database"),
* a payment and penalty audit trail,
* the signature-calibration tally (see ``app/signature.py``).

Writes are small and synchronous. SQLite in WAL mode handles this comfortably
at the event rate the simulator produces.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.config import settings

_DB_PATH = Path(settings.database_path)
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# FastAPI serves requests from a threadpool, so the connection is shared across
# threads and every write is serialised behind this lock.
_conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_lock = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    sequence_id     INTEGER,
    event_class     TEXT,
    server_datetime TEXT,
    real_datetime   TEXT,
    received_at     TEXT NOT NULL,
    signature       TEXT,
    signature_ok    INTEGER,
    payload         TEXT NOT NULL,
    processed       INTEGER NOT NULL DEFAULT 0,
    process_error   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_seq   ON events(sequence_id);
CREATE INDEX IF NOT EXISTS idx_events_class ON events(event_class);

CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plate         TEXT NOT NULL,
    car_type      TEXT,
    spot          TEXT,
    entry_gate    TEXT,
    exit_gate     TEXT,
    arrived_at    TEXT,
    parked_at     TEXT,
    left_spot_at  TEXT,
    minutes       REAL,
    planned_minutes REAL,
    parking_cost  REAL,
    charging_cost REAL,
    paid_amount   REAL,
    payment_ok    INTEGER,
    completed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_plate ON sessions(plate);
CREATE INDEX IF NOT EXISTS idx_sessions_done  ON sessions(completed_at);

CREATE TABLE IF NOT EXISTS payments (
    event_id        TEXT PRIMARY KEY,
    plate           TEXT,
    amount          REAL,
    expected        REAL,
    valid           INTEGER,
    reason          TEXT,
    server_datetime TEXT
);
CREATE INDEX IF NOT EXISTS idx_payments_plate ON payments(plate);

CREATE TABLE IF NOT EXISTS penalties (
    event_id        TEXT PRIMARY KEY,
    reason          TEXT,
    fine_amount     REAL,
    type            TEXT,
    component_name  TEXT,
    server_datetime TEXT
);

CREATE TABLE IF NOT EXISTS component_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT,
    type       TEXT,
    event      TEXT,
    amount     REAL,
    occurred_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_compev_name ON component_events(name);

CREATE TABLE IF NOT EXISTS sequence_gaps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    expected    INTEGER,
    received    INTEGER,
    missing     INTEGER,
    detected_at TEXT
);

CREATE TABLE IF NOT EXISTS signature_trials (
    recipe   TEXT PRIMARY KEY,
    matches  INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

with _lock:
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    _conn.executescript(_SCHEMA)
    _conn.commit()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_event(payload: dict[str, Any], signature_ok: Optional[bool]) -> bool:
    """Persist an inbound webhook.

    ``EventId`` is the primary key, so a redelivery is rejected by the database
    itself rather than by application logic. Returns ``False`` when the event
    was already stored.
    """
    event_id = payload.get("EventId")
    if not event_id:
        return True  # Unsigned/idless test traffic: process but do not dedupe.

    sequence_id = payload.get("SequenceId")
    try:
        sequence_id = int(sequence_id) if sequence_id is not None else None
    except (TypeError, ValueError):
        sequence_id = None

    with _lock:
        cur = _conn.execute(
            """INSERT OR IGNORE INTO events
               (event_id, sequence_id, event_class, server_datetime, real_datetime,
                received_at, signature, signature_ok, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                sequence_id,
                payload.get("EventClass"),
                payload.get("ServerDateTime"),
                payload.get("RealDateTime"),
                _utcnow(),
                payload.get("Signature"),
                None if signature_ok is None else int(signature_ok),
                json.dumps(payload, separators=(",", ":")),
            ),
        )
        _conn.commit()
        return cur.rowcount > 0


def mark_processed(event_id: Optional[str], error: Optional[str] = None) -> None:
    if not event_id:
        return
    with _lock:
        _conn.execute(
            "UPDATE events SET processed = 1, process_error = ? WHERE event_id = ?",
            (error, event_id),
        )
        _conn.commit()


def record_sequence_gap(expected: int, received: int, missing: int) -> None:
    with _lock:
        _conn.execute(
            """INSERT INTO sequence_gaps (expected, received, missing, detected_at)
               VALUES (?, ?, ?, ?)""",
            (expected, received, missing, _utcnow()),
        )
        _conn.commit()


def record_payment(
    event_id: str,
    plate: str,
    amount: float,
    expected: Optional[float],
    valid: bool,
    reason: Optional[str],
    server_datetime: Optional[str],
) -> None:
    with _lock:
        _conn.execute(
            """INSERT OR IGNORE INTO payments
               (event_id, plate, amount, expected, valid, reason, server_datetime)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event_id, plate, amount, expected, int(valid), reason, server_datetime),
        )
        _conn.commit()


def record_penalty(
    event_id: str,
    reason: Optional[str],
    fine_amount: float,
    type_: Optional[str],
    component_name: Optional[str],
    server_datetime: Optional[str],
) -> None:
    with _lock:
        _conn.execute(
            """INSERT OR IGNORE INTO penalties
               (event_id, reason, fine_amount, type, component_name, server_datetime)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, reason, fine_amount, type_, component_name, server_datetime),
        )
        _conn.commit()


def record_component_event(
    name: str, type_: str, event: str, amount: Optional[float] = None
) -> None:
    with _lock:
        _conn.execute(
            """INSERT INTO component_events (name, type, event, amount, occurred_at)
               VALUES (?, ?, ?, ?, ?)""",
            (name, type_, event, amount, _utcnow()),
        )
        _conn.commit()


def record_session(row: dict[str, Any]) -> None:
    """Append a finished parking session for dashboard history."""
    with _lock:
        _conn.execute(
            """INSERT INTO sessions
               (plate, car_type, spot, entry_gate, exit_gate, arrived_at, parked_at,
                left_spot_at, minutes, planned_minutes, parking_cost, charging_cost,
                paid_amount, payment_ok, completed_at)
               VALUES (:plate, :car_type, :spot, :entry_gate, :exit_gate, :arrived_at,
                       :parked_at, :left_spot_at, :minutes, :planned_minutes, :parking_cost,
                       :charging_cost, :paid_amount, :payment_ok, :completed_at)""",
            {"completed_at": _utcnow(), **row},
        )
        _conn.commit()


def bump_signature_trial(recipe: str, matched: bool) -> None:
    with _lock:
        _conn.execute(
            """INSERT INTO signature_trials (recipe, matches, attempts)
               VALUES (?, ?, 1)
               ON CONFLICT(recipe) DO UPDATE SET
                 matches  = matches + excluded.matches,
                 attempts = attempts + 1""",
            (recipe, int(matched)),
        )
        _conn.commit()


def signature_trials() -> list[dict[str, Any]]:
    with _lock:
        rows = _conn.execute(
            """SELECT recipe, matches, attempts,
                      ROUND(100.0 * matches / NULLIF(attempts, 0), 2) AS pct
               FROM signature_trials
               WHERE matches > 0
               ORDER BY matches DESC, recipe ASC"""
        ).fetchall()
    return [dict(r) for r in rows]


def signature_attempts() -> int:
    with _lock:
        row = _conn.execute("SELECT MAX(attempts) AS n FROM signature_trials").fetchone()
    return (row["n"] if row and row["n"] else 0)


def get_meta(key: str, default: Optional[str] = None) -> Optional[str]:
    with _lock:
        row = _conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: Any) -> None:
    with _lock:
        _conn.execute(
            """INSERT INTO meta (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, str(value)),
        )
        _conn.commit()


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Read-only helper for the dashboard endpoints."""
    with _lock:
        rows = _conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def counters() -> dict[str, Any]:
    return query(
        """SELECT
             (SELECT COUNT(*) FROM events)     AS events,
             (SELECT COUNT(*) FROM sessions)   AS completed_sessions,
             (SELECT COUNT(*) FROM penalties)  AS penalties,
             (SELECT COALESCE(SUM(fine_amount), 0) FROM penalties) AS total_fines,
             (SELECT COUNT(*) FROM payments WHERE valid = 0)       AS suspect_payments,
             (SELECT COUNT(*) FROM sequence_gaps)                  AS sequence_gaps"""
    )[0]
