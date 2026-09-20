"""Submission hardening regressions found by the second Level 2 audit."""
from __future__ import annotations

import asyncio
import dataclasses
import time
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import dashboard_api, db, main
from app.seed import load_level
from app.signature import digest
from app.state import Barrier, BarrierPosition, ExhaustFan, ParkingState, SessionPhase, Spot, SpotStatus, Zone


def _signed(event_class: str, **fields):
    payload = {
        "EventClass": event_class,
        "EventId": uuid.uuid4().hex,
        "SequenceId": 1,
        "ServerDateTime": "2026-09-19 12:00:00",
        **fields,
    }
    payload["Signature"] = digest(payload)
    return payload


@pytest.mark.parametrize("raw", ["nan", "inf", "-4", "bad", None])
def test_invalid_planned_duration_cannot_create_invalid_charge(raw):
    assert main._planned_of({"PlannedParkingDurationInMinutes": raw}) == 0.0


def test_live_sync_preserves_fault_status_usage_and_zone_risk():
    state = ParkingState()
    state.load_spots([
        {"name": "BROKEN", "purpose": "Park", "broken": True, "detectedCars": 0,
         "usageCounter": 41},
        {"name": "REPAIRING", "purpose": "Park", "isUnderMaintenance": True,
         "detectedCars": 0, "UsageCounter": 17},
    ])
    state.load_barriers([{"name": "G", "state": "Closed", "usageCounter": 23}])
    state.load_zones([{"name": "Z", "gasCarbonMonoxideLevel": 72, "risk": "High"}])

    assert state.spots["BROKEN"].status == SpotStatus.BROKEN
    assert state.spots["REPAIRING"].status == SpotStatus.MAINTENANCE
    assert state.spots["BROKEN"].cycle_count == 41
    assert state.spots["REPAIRING"].cycle_count == 17
    assert state.barriers["G"].cycle_count == 23
    assert state.zones["Z"].danger_level == "High"
    assert state.occupancy_counts()["AVAILABLE"] == 0


def test_level2_offline_seed_includes_lights_and_usage():
    seeded = load_level("lvl2")
    assert seeded is not None
    assert len(seeded["lights"]) == 30
    assert any(spot["usageCounter"] == 0 for spot in seeded["spots"])


def test_public_bay_picker_hides_queued_repairs(monkeypatch):
    state = ParkingState()
    state.spots["S1"] = Spot("S1", zone_parent="Z")
    state.pending_repairs["S1"] = "ParkingSpot"
    monkeypatch.setattr(dashboard_api, "state", state)

    bays = asyncio.run(dashboard_api.gate_bays())
    assert bays == [{"name": "S1", "zone": "Z", "car_type": "Any", "available": False}]


def test_pending_repairs_are_visible_as_unavailable_components():
    state = ParkingState()
    state.spots["S1"] = Spot("S1")
    state.barriers["G1"] = Barrier("G1")
    state.fans["F1"] = ExhaustFan("F1")
    state.pending_repairs.update({"S1": "ParkingSpot", "G1": "BarrierGate", "F1": "ExhaustFan"})

    snapshot = state.snapshot()
    assert snapshot["spots"][0]["repair_pending"] is True
    assert snapshot["barriers"][0]["repair_pending"] is True
    assert snapshot["fans"][0]["repair_pending"] is True
    assert {row["name"] for row in state.broken_components()} == {"S1", "G1", "F1"}


def test_restart_resumes_exit_invoice_that_was_not_attempted(monkeypatch):
    state = ParkingState()
    monkeypatch.setattr(main, "state", state)
    monkeypatch.setattr(main, "settings", dataclasses.replace(
        main.settings, autopilot=True, exit_charge_delay_s=0, game_speed=8))
    charge = AsyncMock()
    monkeypatch.setattr(main.client, "car_charge", charge)
    session = state.start_session("RESTART EXIT", "ENTRY1", planned_minutes=3)
    session.exit_gate = "EXIT1"
    session.exit_confirmed = True
    session.phase = SessionPhase.AT_EXIT
    db.save_active_session(session)

    async def scenario():
        await main._ensure_barriers_open()
        await asyncio.gather(*list(main._entry_watchers))

    try:
        asyncio.run(scenario())
        charge.assert_awaited_once()
        assert session.charge_attempted
    finally:
        with db._lock, db._conn:
            db._conn.execute("DELETE FROM active_sessions WHERE session_id=?", (session.session_id,))


def test_archive_records_actual_simulated_stay_and_unpaid_status(monkeypatch):
    state = ParkingState()
    monkeypatch.setattr(main, "state", state)
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, game_speed=8))
    session = state.start_session("ACTUAL TIME", "ENTRY1", planned_minutes=9)
    state.spots["S1"] = Spot("S1", zone_parent="Z")
    state.mark_parked(session.plate, "S1")
    session.parked_at = time.monotonic() - 30
    state.mark_left_spot(session.plate)

    main._archive(session.plate)
    try:
        row = db.query("SELECT minutes, planned_minutes, payment_ok FROM sessions WHERE session_id=?",
                       (session.session_id,))[0]
        assert 3.9 <= row["minutes"] <= 4.1
        assert row["planned_minutes"] == 9
        assert row["payment_ok"] is None
    finally:
        with db._lock, db._conn:
            db._conn.execute("DELETE FROM sessions WHERE session_id=?", (session.session_id,))


def test_invalid_payment_is_archived_as_suspect(monkeypatch):
    state = ParkingState()
    monkeypatch.setattr(main, "state", state)
    session = state.start_session("SUSPECT HISTORY", "ENTRY1", planned_minutes=2)
    session.expected_amount = 2.0
    session.charge_attempted = True
    session.charged = True

    asyncio.run(main._handle_payment_made({"CarPlateNumber": session.plate, "Amount": 0.25}))
    main._archive(session.plate)
    try:
        row = db.query("SELECT payment_ok FROM sessions WHERE session_id=?", (session.session_id,))[0]
        assert row["payment_ok"] == 0
    finally:
        with db._lock, db._conn:
            db._conn.execute("DELETE FROM sessions WHERE session_id=?", (session.session_id,))


def test_component_fixed_records_repair_cost(monkeypatch):
    state = ParkingState()
    state.barriers["COST_GATE"] = Barrier("COST_GATE")
    monkeypatch.setattr(main, "state", state)
    asyncio.run(main._handle_component_fixed(
        {"Type": "BarrierGate", "Name": "COST_GATE", "RepairCost": "12.50"}))
    try:
        row = db.query("SELECT amount FROM component_events WHERE name='COST_GATE' ORDER BY id DESC LIMIT 1")[0]
        assert row["amount"] == 12.5
    finally:
        with db._lock, db._conn:
            db._conn.execute("DELETE FROM component_events WHERE name='COST_GATE'")


def test_unknown_entry_departure_does_not_leak_plate_memory(monkeypatch):
    monkeypatch.setattr(main, "_live_bays_synced", True)
    main._left_entry.discard("UNKNOWN LEAVER")
    asyncio.run(main._handle_car_spot_action({
        "CarPlateNumber": "UNKNOWN LEAVER", "SpotName": "ENTRY1",
        "SpotType": "EntrySpot", "Direction": "CarOut",
    }))
    assert "UNKNOWN LEAVER" not in main._left_entry


def test_gate_hold_never_operates_broken_or_repairing_gate(monkeypatch):
    state = ParkingState()
    barrier = Barrier("G", state=BarrierPosition.OPEN, under_maintenance=True)
    barrier.held_vehicles.add("WAIT")
    state.barriers["G"] = barrier
    monkeypatch.setattr(main, "state", state)
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, autopilot=True))
    close = AsyncMock()
    monkeypatch.setattr(main.client, "barrier_close", close)

    asyncio.run(main._handle_gate_action({"Name": "G", "Action": "Open"}))
    close.assert_not_awaited()


def test_manual_hold_on_closed_gate_does_not_add_a_wear_cycle(monkeypatch):
    state = ParkingState()
    state.barriers["G"] = Barrier("G", state=BarrierPosition.CLOSED)
    monkeypatch.setattr(main, "state", state)
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, autopilot=True))
    close = AsyncMock()
    monkeypatch.setattr(main.client, "barrier_close", close)

    result = asyncio.run(main.manual_barrier_close(
        "G", {"username": "admin", "role": "admin"}))
    assert result["operator_override"] is True
    assert result["sent"] is False
    assert state.barriers["G"].cycle_count == 0
    close.assert_not_awaited()


def test_preventive_gate_repair_waits_until_gate_is_idle(monkeypatch):
    state = ParkingState()
    gate = Barrier("BUSY_GATE", operator_override=True)
    state.barriers[gate.name] = gate
    monkeypatch.setattr(main, "state", state)
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, autopilot=True))
    monkeypatch.setattr(main.client, "barrier_repair", AsyncMock())

    async def run_now(label, action, priority=50, on_drop=None):
        await action()

    monkeypatch.setattr(main.maintenance_queue, "submit", run_now)
    # 4.33: a staff-held gate is not even queued; the rotation returns later.
    asyncio.run(main._queue_repair("BarrierGate", gate.name))
    main.client.barrier_repair.assert_not_awaited()
    assert gate.name not in state.pending_repairs


def test_preventive_fan_repair_waits_while_co_is_unsafe(monkeypatch):
    state = ParkingState()
    state.zones["Z"] = Zone("Z", gas_co_level=80, danger_level="High")
    state.fans["F"] = ExhaustFan("F", zone_parent="Z", is_on=True,
                                   turned_on_at=time.monotonic())
    monkeypatch.setattr(main, "state", state)
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, autopilot=True))
    monkeypatch.setattr(main.client, "fan_off", AsyncMock())
    monkeypatch.setattr(main.client, "fan_repair", AsyncMock())

    async def run_now(label, action, priority=50, on_drop=None):
        await action()

    monkeypatch.setattr(main.maintenance_queue, "submit", run_now)
    with pytest.raises(RuntimeError, match="fan is required for unsafe CO"):
        asyncio.run(main._queue_repair("ExhaustFan", "F"))
    main.client.fan_off.assert_not_awaited()
    main.client.fan_repair.assert_not_awaited()


def test_unavailable_entry_is_tracked_until_vehicle_leaves(monkeypatch):
    state = ParkingState()
    state.spots["ENTRY1"] = Spot("ENTRY1", purpose="EntrySpot")
    state.spots["S1"] = Spot("S1")
    state.barriers["G"] = Barrier("G", broken=True)
    monkeypatch.setattr(main, "state", state)
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, autopilot=True))
    monkeypatch.setattr(main, "_barrier_for_sensor", lambda sensor: "G")

    result = asyncio.run(main.dispatch_entry("BLOCKED ARRIVAL", "ENTRY1", planned_minutes=2))
    assert not result["dispatched"]
    assert state.get_session("BLOCKED ARRIVAL") is not None


def test_startup_restores_sequence_and_penalty_totals(monkeypatch):
    state = ParkingState()
    monkeypatch.setattr(main, "state", state)
    event_id = uuid.uuid4().hex
    payload = {"EventId": event_id, "EventClass": "penalty", "SequenceId": 999999,
               "ServerDateTime": "2026-09-19 12:00:00"}
    db.record_event(payload, True)
    db.record_penalty(event_id, "restore", 7.0, "BarrierGate", "G", payload["ServerDateTime"])
    expected = db.counters()
    try:
        main.restore_operational_observability()
        assert state.last_sequence_id == 999999
        assert state.penalty_count == expected["penalties"]
        assert state.total_fines == expected["total_fines"]
    finally:
        with db._lock, db._conn:
            db._conn.execute("DELETE FROM penalties WHERE event_id=?", (event_id,))
            db._conn.execute("DELETE FROM events WHERE event_id=?", (event_id,))


def test_daily_revenue_uses_payment_receipt_date():
    event_id = uuid.uuid4().hex
    before = asyncio.run(main.daily_report({"role": "auditor"}))["revenue"]["paid_total"]
    payload = {"EventId": event_id, "EventClass": "payment_made",
               "ServerDateTime": "2020-01-01 12:00:00"}
    db.record_event(payload, True)
    db.record_payment(event_id, "CLOCK PAY", 13.0, 13.0, True, "Car Payment",
                      payload["ServerDateTime"])
    try:
        report = asyncio.run(main.daily_report({"role": "auditor"}))
        assert report["revenue"]["paid_total"] == before + 13
    finally:
        with db._lock, db._conn:
            db._conn.execute("DELETE FROM payments WHERE event_id=?", (event_id,))
            db._conn.execute("DELETE FROM events WHERE event_id=?", (event_id,))


def test_daily_financial_report_includes_repair_costs():
    name = "REPORT_REPAIR_" + uuid.uuid4().hex
    before = asyncio.run(main.daily_report({"role": "auditor"}))["revenue"]
    db.record_component_event(name, "BarrierGate", "fixed_reactive", amount=6.5)
    try:
        after = asyncio.run(main.daily_report({"role": "auditor"}))["revenue"]
        assert after["repair_cost_total"] == before["repair_cost_total"] + 6.5
        assert after["net_revenue"] == before["net_revenue"] - 6.5
    finally:
        with db._lock, db._conn:
            db._conn.execute("DELETE FROM component_events WHERE name=?", (name,))


def test_failed_webhook_handler_can_be_retried_on_redelivery(monkeypatch):
    calls = 0

    async def flaky(payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary failure")

    monkeypatch.setitem(main._HANDLERS, "retry_test", flaky)
    payload = _signed("retry_test")
    client = TestClient(main.app)
    try:
        assert client.post("/webhooks/simulator", json=payload).status_code == 500
        assert client.post("/webhooks/simulator", json=payload).status_code == 200
        assert calls == 2
        row = db.query("SELECT processed, process_error FROM events WHERE event_id=?", (payload["EventId"],))[0]
        assert row == {"processed": 1, "process_error": None}
    finally:
        with db._lock, db._conn:
            db._conn.execute("DELETE FROM events WHERE event_id=?", (payload["EventId"],))


def test_numeric_string_sequence_is_processed(monkeypatch):
    seen = []

    async def handler(payload):
        seen.append(payload["SequenceId"])

    monkeypatch.setitem(main._HANDLERS, "string_sequence", handler)
    payload = _signed("string_sequence")
    payload["SequenceId"] = "9001"
    payload["Signature"] = digest(payload)
    client = TestClient(main.app)
    try:
        assert client.post("/webhooks/simulator", json=payload).status_code == 200
        assert seen == ["9001"]
    finally:
        with db._lock, db._conn:
            db._conn.execute("DELETE FROM events WHERE event_id=?", (payload["EventId"],))


def test_malformed_json_webhook_returns_400():
    response = TestClient(main.app).post(
        "/webhooks/simulator", content=b"{", headers={"Content-Type": "application/json"})
    assert response.status_code == 400
