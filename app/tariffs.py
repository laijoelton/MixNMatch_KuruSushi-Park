"""Validated, durable effective tariffs. Each invoice reads a consistent version."""
from __future__ import annotations

import json
import math

from app import db
from app.config import settings

KEYS = ("parking_rate_per_minute", "minimum_charge", "electric_multiplier", "electric_split_charging",
        "class_multiplier_sedan", "class_multiplier_suv", "class_multiplier_ev",
        "billing_rounding", "billing_basis")


def seed() -> None:
    with db._lock, db._conn:
        db._conn.execute("""CREATE TABLE IF NOT EXISTS tariff_settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL)""")
        for key in KEYS:
            db._conn.execute("INSERT OR IGNORE INTO tariff_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
                             (key, json.dumps(getattr(settings, key)), db._utcnow(), "environment"))


def effective() -> dict:
    return {r["key"]: json.loads(r["value"]) for r in db.query("SELECT key, value FROM tariff_settings")}


def update(changes: dict, actor: str) -> tuple[dict, dict]:
    if not changes or set(changes) - set(KEYS):
        raise ValueError("supply known tariff keys only")
    for key, value in changes.items():
        if key == "billing_rounding":
            valid = value in ("round", "ceil", "exact")
        elif key == "billing_basis":
            valid = value in ("planned", "measured")
        elif key == "electric_split_charging":
            valid = isinstance(value, bool)
        else:
            valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
        if not valid:
            raise ValueError(f"invalid tariff value for {key}")
    with db._lock, db._conn:
        before = effective()
        for key, value in changes.items():
            db._conn.execute("UPDATE tariff_settings SET value = ?, updated_at = ?, updated_by = ? WHERE key = ?",
                             (json.dumps(value), db._utcnow(), actor, key))
        after = effective()
    return before, after


seed()
