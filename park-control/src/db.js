import { DatabaseSync } from 'node:sqlite';
import { config } from './config.js';

export const db = new DatabaseSync(config.dbPath);

db.exec('PRAGMA journal_mode = WAL');
db.exec('PRAGMA synchronous = NORMAL');
db.exec('PRAGMA foreign_keys = ON');

db.exec(`
-- Raw append-only event log. Every webhook lands here first, verbatim.
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
CREATE INDEX IF NOT EXISTS idx_events_seq       ON events(sequence_id);
CREATE INDEX IF NOT EXISTS idx_events_class     ON events(event_class);
CREATE INDEX IF NOT EXISTS idx_events_received  ON events(received_at);
CREATE INDEX IF NOT EXISTS idx_events_processed ON events(processed);

-- One row per car currently known to us, plus history of completed sessions.
CREATE TABLE IF NOT EXISTS cars (
  plate           TEXT PRIMARY KEY,
  car_type        TEXT,
  state           TEXT NOT NULL,
  planned_minutes INTEGER,
  assigned_spot   TEXT,
  entry_spot      TEXT,
  exit_spot       TEXT,
  arrived_at      TEXT,
  parked_at       TEXT,
  left_spot_at    TEXT,
  at_exit_at      TEXT,
  charged_at      TEXT,
  released_at     TEXT,
  parking_cost    REAL,
  charging_cost   REAL,
  paid_amount     REAL,
  payment_ok      INTEGER,
  updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cars_state ON cars(state);

-- Completed sessions, kept separately so the dashboard can search history
-- without the live table growing unbounded.
CREATE TABLE IF NOT EXISTS sessions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  plate         TEXT NOT NULL,
  car_type      TEXT,
  spot          TEXT,
  parked_at     TEXT,
  left_spot_at  TEXT,
  minutes       REAL,
  parking_cost  REAL,
  charging_cost REAL,
  paid_amount   REAL,
  payment_ok    INTEGER,
  released_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_plate ON sessions(plate);
CREATE INDEX IF NOT EXISTS idx_sessions_rel   ON sessions(released_at);

-- Mirror of simulator components, seeded by the bootstrap sync then kept
-- current from webhooks alone (the list endpoints cost us, so we do not poll).
CREATE TABLE IF NOT EXISTS components (
  name              TEXT PRIMARY KEY,
  kind              TEXT NOT NULL,
  zone              TEXT,
  state             TEXT,
  car_type          TEXT,
  light_group       TEXT,
  is_on             INTEGER,
  broken            INTEGER NOT NULL DEFAULT 0,
  under_maintenance INTEGER NOT NULL DEFAULT 0,
  occupied_by       TEXT,
  usage_count       INTEGER NOT NULL DEFAULT 0,
  last_broken_at    TEXT,
  last_fixed_at     TEXT,
  updated_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_components_kind ON components(kind);
CREATE INDEX IF NOT EXISTS idx_components_zone ON components(zone);

CREATE TABLE IF NOT EXISTS zones (
  name       TEXT PRIMARY KEY,
  co_level   REAL,
  danger     TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS penalties (
  event_id        TEXT PRIMARY KEY,
  reason          TEXT,
  fine_amount     REAL,
  type            TEXT,
  component_name  TEXT,
  server_datetime TEXT
);

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

-- Gap detection for SequenceId, so we can prove nothing was silently dropped.
CREATE TABLE IF NOT EXISTS sequence_gaps (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  expected    INTEGER,
  received    INTEGER,
  detected_at TEXT
);

-- Tally of which signature recipes matched, used to calibrate the verifier.
CREATE TABLE IF NOT EXISTS signature_trials (
  recipe   TEXT PRIMARY KEY,
  matches  INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
`);

export const now = () => new Date().toISOString();

export function getMeta(key, fallback = null) {
  const row = db.prepare('SELECT value FROM meta WHERE key = ?').get(key);
  return row ? row.value : fallback;
}

export function setMeta(key, value) {
  db.prepare(
    'INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
  ).run(key, String(value));
}

/**
 * Insert an event, relying on the EventId primary key for idempotency.
 * Returns false when we have already seen this EventId.
 */
export function insertEvent(row) {
  const res = db
    .prepare(
      `INSERT OR IGNORE INTO events
       (event_id, sequence_id, event_class, server_datetime, real_datetime,
        received_at, signature, signature_ok, payload)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    )
    .run(
      row.eventId,
      row.sequenceId,
      row.eventClass,
      row.serverDateTime,
      row.realDateTime,
      now(),
      row.signature,
      row.signatureOk === null ? null : row.signatureOk ? 1 : 0,
      row.payload,
    );
  return res.changes > 0;
}

export function markProcessed(eventId, error = null) {
  db.prepare('UPDATE events SET processed = 1, process_error = ? WHERE event_id = ?').run(
    error,
    eventId,
  );
}

/**
 * SequenceId is documented to increase by exactly one per webhook, so any jump
 * means we lost events. We record the gap rather than trying to backfill --
 * the list endpoints are the documented recovery path.
 */
export function trackSequence(sequenceId) {
  if (typeof sequenceId !== 'number' || Number.isNaN(sequenceId)) return;
  const last = Number(getMeta('last_sequence_id', '0'));
  if (last && sequenceId > last + 1) {
    db.prepare(
      'INSERT INTO sequence_gaps(expected, received, detected_at) VALUES (?, ?, ?)',
    ).run(last + 1, sequenceId, now());
  }
  if (sequenceId > last) setMeta('last_sequence_id', sequenceId);
}
