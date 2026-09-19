import asyncio
import uuid

from app import db, main


def test_daily_penalties_use_receipt_date_like_operational_totals():
    user = {"role": "auditor"}
    before = asyncio.run(main.daily_report(user))["revenue"]["penalty_total"]
    event_id = uuid.uuid4().hex
    payload = {"EventId": event_id, "EventClass": "penalty", "ServerDateTime": "2020-01-01 12:00:00"}
    db.record_event(payload, True)
    db.record_penalty(event_id, "clock regression", 17.0, "BarrierGate", "G", payload["ServerDateTime"])
    try:
        report = asyncio.run(main.daily_report(user))
        assert report["revenue"]["penalty_total"] == before + 17
        assert report["date_basis"] == "UTC receipt date"
    finally:
        with db._lock, db._conn:
            db._conn.execute("DELETE FROM penalties WHERE event_id=?", (event_id,))
            db._conn.execute("DELETE FROM events WHERE event_id=?", (event_id,))
