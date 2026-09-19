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

-- Level 2 --------------------------------------------------------------- --

CREATE TABLE IF NOT EXISTS unsigned_webhook_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT,
    reason      TEXT,
    received_at TEXT NOT NULL,
    payload     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_username TEXT,
    actor_role     TEXT,
    action         TEXT NOT NULL,
    detail         TEXT,
    occurred_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_logs_at ON audit_logs(occurred_at);

CREATE TABLE IF NOT EXISTS login_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL,
    ip          TEXT,
    success     INTEGER NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_user ON login_attempts(username);

CREATE TABLE IF NOT EXISTS component_wear (
    name             TEXT PRIMARY KEY,
    type             TEXT NOT NULL,
    cycle_count      INTEGER NOT NULL DEFAULT 0,
    runtime_seconds  REAL NOT NULL DEFAULT 0,
    last_repaired_at TEXT
);

CREATE TABLE IF NOT EXISTS ghost_car_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    plate           TEXT NOT NULL,
    gate            TEXT,
    occurred_at     TEXT NOT NULL,
    fallback_charge REAL,
    resolved        INTEGER NOT NULL DEFAULT 0,
    resolved_by     TEXT,
    resolved_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_ghost_car_resolved ON ghost_car_events(resolved);
"""

with _lock:
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    _conn.executescript(_SCHEMA)
    columns = {r[1] for r in _conn.execute("PRAGMA table_info(sessions)")}
    for name in ("zone", "session_id"):
        if name not in columns:
            _conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} TEXT")
    _conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_session_identity ON sessions(session_id)")
    _conn.executescript("""
        CREATE TABLE IF NOT EXISTS active_sessions (
            plate TEXT PRIMARY KEY, session_id TEXT NOT NULL, payload TEXT NOT NULL,
            charge_attempted INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS neglected_vehicles (
            session_id TEXT PRIMARY KEY, plate TEXT, gate TEXT, reason TEXT, occurred_at TEXT);
    """)
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


def mark_failed(event_id: Optional[str], error: str) -> None:
    """Keep a failed accepted event retryable while retaining its error."""
    if not event_id:
        return
    with _lock:
        _conn.execute(
            "UPDATE events SET processed = 0, process_error = ? WHERE event_id = ?",
            (error, event_id),
        )
        _conn.commit()


def event_status(event_id: str) -> Optional[dict[str, Any]]:
    rows = query("SELECT processed, process_error FROM events WHERE event_id = ?", (event_id,))
    return rows[0] if rows else None


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
            """INSERT OR IGNORE INTO sessions
               (plate, car_type, spot, entry_gate, exit_gate, arrived_at, parked_at,
                left_spot_at, minutes, planned_minutes, parking_cost, charging_cost,
                paid_amount, payment_ok, completed_at, zone, session_id)
               VALUES (:plate, :car_type, :spot, :entry_gate, :exit_gate, :arrived_at,
                       :parked_at, :left_spot_at, :minutes, :planned_minutes, :parking_cost,
                       :charging_cost, :paid_amount, :payment_ok, :completed_at, :zone, :session_id)""",
            {"completed_at": _utcnow(), "zone": None, "session_id": None, **row},
        )
        if row.get("session_id"):
            _conn.execute("DELETE FROM active_sessions WHERE session_id = ?", (row["session_id"],))
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


def record_unsigned_webhook(event_id: Optional[str], reason: str, payload: dict[str, Any]) -> None:
    """Enforce-mode rejection: log the payload instead of silently dropping it."""
    with _lock:
        _conn.execute(
            """INSERT INTO unsigned_webhook_logs (event_id, reason, received_at, payload)
               VALUES (?, ?, ?, ?)""",
            (event_id, reason, _utcnow(), json.dumps(payload, separators=(",", ":"))),
        )
        _conn.commit()


def record_audit_log(actor_username: Optional[str], actor_role: Optional[str],
                      action: str, detail: Optional[str] = None) -> None:
    """Append-only trail of every staff-triggered mutation."""
    with _lock:
        _conn.execute(
            """INSERT INTO audit_logs (actor_username, actor_role, action, detail, occurred_at)
               VALUES (?, ?, ?, ?, ?)""",
            (actor_username, actor_role, action, detail, _utcnow()),
        )
        _conn.commit()


def record_login_attempt(username: str, ip: Optional[str], success: bool) -> None:
    with _lock:
        _conn.execute(
            """INSERT INTO login_attempts (username, ip, success, occurred_at)
               VALUES (?, ?, ?, ?)""",
            (username, ip, int(success), _utcnow()),
        )
        _conn.commit()


def prior_login_attempts(username: str, limit: int = 3) -> list[dict[str, Any]]:
    """The last ``limit`` attempts for ``username`` BEFORE the one just recorded."""
    with _lock:
        rows = _conn.execute(
            """SELECT username, ip, success, occurred_at FROM login_attempts
               WHERE username = ? ORDER BY id DESC LIMIT ?""",
            (username, max(1, min(limit, 100))),
        ).fetchall()
    return [dict(r) for r in rows][:limit]


def upsert_component_wear(name: str, type_: str, cycle_delta: int = 0,
                           runtime_delta: float = 0.0) -> dict[str, Any]:
    """Add to a component's wear counters, creating the row if needed."""
    with _lock:
        _conn.execute(
            """INSERT INTO component_wear (name, type, cycle_count, runtime_seconds)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 cycle_count     = cycle_count + excluded.cycle_count,
                 runtime_seconds = runtime_seconds + excluded.runtime_seconds,
                 type            = excluded.type""",
            (name, type_, cycle_delta, runtime_delta),
        )
        _conn.commit()
        row = _conn.execute("SELECT * FROM component_wear WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else {}


def sync_component_wear(name: str, type_: str, cycle_count: int, runtime_seconds: float) -> None:
    """Overwrite a component's wear row with the live absolute counters from
    ``app.state`` (the in-memory counters are the source of truth; this just
    makes them durable across restarts). Safer than accumulating deltas here,
    since the caller may resync the same reading more than once."""
    with _lock:
        _conn.execute(
            """INSERT INTO component_wear (name, type, cycle_count, runtime_seconds)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 cycle_count     = excluded.cycle_count,
                 runtime_seconds = excluded.runtime_seconds,
                 type            = excluded.type""",
            (name, type_, cycle_count, runtime_seconds),
        )
        _conn.commit()


def mark_component_repaired(name: str) -> None:
    with _lock:
        _conn.execute(
            """UPDATE component_wear SET cycle_count = 0, runtime_seconds = 0,
               last_repaired_at = ? WHERE name = ?""",
            (_utcnow(), name),
        )
        _conn.commit()


def component_wear_rows() -> list[dict[str, Any]]:
    return query("SELECT * FROM component_wear ORDER BY name")


def record_ghost_car(plate: str, gate: Optional[str], fallback_charge: float) -> int:
    with _lock:
        existing = _conn.execute("SELECT id FROM ghost_car_events WHERE plate = ? AND resolved = 0", (plate,)).fetchone()
        if existing:
            return existing["id"]
        cur = _conn.execute(
            """INSERT INTO ghost_car_events (plate, gate, occurred_at, fallback_charge)
               VALUES (?, ?, ?, ?)""",
            (plate, gate, _utcnow(), fallback_charge),
        )
        _conn.commit()
        return cur.lastrowid


def resolve_ghost_car(ghost_id: int, resolved_by: str) -> Optional[dict[str, Any]]:
    with _lock:
        row = _conn.execute("SELECT * FROM ghost_car_events WHERE id = ?", (ghost_id,)).fetchone()
        if row is None or row["resolved"]:
            return None
        _conn.execute(
            "UPDATE ghost_car_events SET resolved = 1, resolved_by = ?, resolved_at = ? WHERE id = ?",
            (resolved_by, _utcnow(), ghost_id),
        )
        _conn.commit()
        row = _conn.execute("SELECT * FROM ghost_car_events WHERE id = ?", (ghost_id,)).fetchone()
    return dict(row) if row else None


def median_parking_cost() -> Optional[float]:
    rows = query(
        "SELECT parking_cost FROM sessions WHERE parking_cost IS NOT NULL ORDER BY parking_cost"
    )
    values = [r["parking_cost"] for r in rows]
    n = len(values)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return float(values[mid])
    return float((values[mid - 1] + values[mid]) / 2.0)


def save_active_session(session) -> None:
    from dataclasses import asdict
    with _lock, _conn:
        _conn.execute("""INSERT INTO active_sessions (plate, session_id, payload, charge_attempted) VALUES (?, ?, ?, ?)
            ON CONFLICT(plate) DO UPDATE SET session_id=excluded.session_id, payload=excluded.payload,
            charge_attempted=MAX(active_sessions.charge_attempted, excluded.charge_attempted)""",
            (session.plate, session.session_id, json.dumps(asdict(session)), int(session.charge_attempted)))


def delete_active_session(session_id: str) -> None:
    with _lock, _conn:
        _conn.execute("DELETE FROM active_sessions WHERE session_id = ?", (session_id,))


def claim_charge(session) -> bool:
    """Commit the once-only claim before any network I/O, including after a crash."""
    with _lock, _conn:
        cur = _conn.execute("UPDATE active_sessions SET charge_attempted = 1 WHERE session_id = ? AND charge_attempted = 0",
                            (session.session_id,))
        return cur.rowcount == 1


def record_neglect(session, reason: str) -> None:
    with _lock, _conn:
        _conn.execute("INSERT OR IGNORE INTO neglected_vehicles (session_id, plate, gate, reason, occurred_at) VALUES (?, ?, ?, ?, ?)",
                      (session.session_id, session.plate, session.entry_gate, reason, _utcnow()))


def counters() -> dict[str, Any]:
    return query(
        """SELECT
             (SELECT COUNT(*) FROM events)     AS events,
             (SELECT COUNT(*) FROM sessions)   AS completed_sessions,
             (SELECT COUNT(*) FROM penalties)  AS penalties,
             (SELECT COALESCE(SUM(fine_amount), 0) FROM penalties) AS total_fines,
             (SELECT COUNT(*) FROM payments WHERE valid = 0)       AS suspect_payments,
             (SELECT COUNT(*) FROM sequence_gaps)                  AS sequence_gaps,
             (SELECT COUNT(*) FROM events WHERE processed = 0)     AS unprocessed_events"""
    )[0]
