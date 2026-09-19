import asyncio
import dataclasses
import json
import time
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, main, tariffs
from app.client import SimulatorClient
from app.queue_worker import schedule_lights
from app.signature import digest, verify
from app.state import Barrier, BarrierPosition, ExhaustFan, Light, SessionPhase, Spot, VehicleSession, state


@pytest.fixture
def park(monkeypatch):
    for mapping in (state.sessions, state.spots, state.active_dispatches, state.barriers, state.fans, state.lights, state.deferred_repairs):
        mapping.clear()
    state.neglected_vehicles.clear()
    main._left_entry.clear()
    with db._lock, db._conn:
        for table in ("active_sessions", "ghost_car_events", "neglected_vehicles"):
            db._conn.execute(f"DELETE FROM {table}")
    monkeypatch.setattr(main, "_live_bays_synced", True)
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, autopilot=True, game_speed=8,
                                                            entry_max_attempts=3, exit_charge_delay_s=0))
    for command in ("car_goto", "car_charge", "barrier_open", "barrier_close", "fan_on", "fan_off", "light_on", "light_off", "spot_repair"):
        monkeypatch.setattr(main.client, command, AsyncMock())
    state.spots["S1"] = Spot("S1", zone_parent="ZONE1")
    yield


def event(plate, sensor, kind, direction):
    return {"CarPlateNumber": plate, "SpotName": sensor, "SpotType": kind, "Direction": direction}


def test_signature_exact_recipe_and_missing_rejected():
    payload = {"B": "two", "A": "one", "Signature": "ignored"}
    import hashlib
    assert digest(payload) == hashlib.md5(b"one|two").hexdigest()
    assert not verify(payload).ok
    payload["Signature"] = digest(payload)
    assert verify(payload).ok
    payload.pop("Signature")
    assert not verify(payload).ok
    c = TestClient(main.app)
    assert c.post("/webhooks/simulator", json=payload).status_code == 401
    assert db.query("SELECT * FROM unsigned_webhook_logs ORDER BY id DESC LIMIT 1")


def test_dispatch_nonblocking_cascade_and_retry_stop(park):
    async def scenario():
        result = await main.dispatch_entry("CAR", "ENTRY1")
        assert result["dispatched"]
        assert main.client.car_goto.await_count == 0  # No network await in the webhook.
        await asyncio.sleep(0)
        assert main.client.car_goto.await_count == 1
        await main._handle_car_spot_action(event("CAR", "ENTRY2", "EntrySpot", "CarIn"))
        assert main.client.car_goto.await_count == 1
        assert len(state.active_dispatches) == 1
        await main._handle_car_spot_action(event("CAR", "ENTRY1", "EntrySpot", "CarOut"))
        await asyncio.sleep(0.42)
        assert main.client.car_goto.await_count == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("spot", ["S15", "S30", "S150", "S151"])
def test_route_crossing_exit_does_not_bill_or_archive(park, spot):
    state.spots[spot] = Spot(spot)
    session = state.start_session("CROSS", "ENTRY1")
    state.assign_spot("CROSS", spot)
    async def scenario():
        await main._handle_car_spot_action(event("CROSS", "EXIT", "ExitSpot", "CarIn"))
        await main._handle_car_spot_action(event("CROSS", "EXIT", "ExitSpot", "CarOut"))
        await asyncio.sleep(0)
    asyncio.run(scenario())
    assert state.get_session("CROSS") is session
    assert not db.query("SELECT * FROM ghost_car_events")
    main.client.car_charge.assert_not_awaited()


def test_physical_dwell_guard(park):
    s = state.start_session("QUICK", "ENTRY1")
    state.mark_parked("QUICK", "S1")
    assert not main._valid_exit(s)
    s.created_at -= 10
    assert main._valid_exit(s)


def test_charge_exactly_once_concurrently_and_after_restore(park):
    s = state.start_session("PAY", "ENTRY1", planned_minutes=3)
    s.created_at -= 20
    state.mark_parked("PAY", "S1")
    state.mark_left_spot("PAY")
    async def scenario():
        await asyncio.gather(main._charge_at_exit("PAY", "EXIT"), main._charge_at_exit("PAY", "EXIT"))
        assert main.client.car_charge.await_count == 1
        await main._apply_charge_correction({"ComponentName": "PAY"}, 3, 4)
        assert main.client.car_charge.await_count == 1
        state.sessions.clear()
        main.restore_sessions()
        assert state.sessions["PAY"].charge_attempted
        await main._charge_at_exit("PAY", "EXIT")
        assert main.client.car_charge.await_count == 1
    asyncio.run(scenario())


def test_failed_charge_never_retries(park):
    s = state.start_session("FAIL", "ENTRY1", planned_minutes=2)
    main.client.car_charge.side_effect = httpx.ReadTimeout("uncertain response")
    async def scenario():
        await main._charge_at_exit("FAIL", "EXIT")
        await main._charge_at_exit("FAIL", "EXIT")
    asyncio.run(scenario())
    assert s.charge_attempted and main.client.car_charge.await_count == 1


def test_operator_hold_survives_closed_webhook_and_blocks_dispatch(park):
    state.barriers["gateA"] = Barrier("gateA", state=BarrierPosition.OPEN)
    async def scenario():
        await main.manual_barrier_close("gateA", {"username": "operator", "role": "facility_operator"})
        await main._handle_gate_action({"Name": "gateA", "Action": "Closed"})
        assert not (await main.dispatch_entry("HELD", "ENTRY1"))["dispatched"]
        main.client.barrier_open.assert_not_awaited()
        assert state.snapshot()["barriers"][0]["hold_reason"] == "Held closed by operator"
    asyncio.run(scenario())


@pytest.mark.parametrize("kind,multiplier", [("Normal", 1), (" sedan ", 1), ("SUV", 1.25), ("Van", 1.25)])
def test_vehicle_billing(park, kind, multiplier):
    before = tariffs.effective()
    try:
        tariffs.update({"billing_basis": "planned", "parking_rate_per_minute": 1, "minimum_charge": 0,
                        "class_multiplier_sedan": 1, "class_multiplier_suv": 1.25}, "test")
        s = VehicleSession("BILL", "ENTRY1", car_type=kind, planned_minutes=4)
        assert main.compute_charge(s) == (4 * multiplier, 0, 4)
    finally:
        tariffs.update(before, "test cleanup")


def test_rounding_ev_and_mid_session_tariff_change(park):
    before = tariffs.effective()
    try:
        tariffs.update({"billing_basis": "measured", "parking_rate_per_minute": 1, "minimum_charge": 0,
                        "class_multiplier_ev": 1.1, "electric_multiplier": 2, "electric_split_charging": True}, "test")
        s = VehicleSession("EV", "ENTRY1", car_type="EV", parked_at=100, left_spot_at=118.75)
        for mode, minutes in (("round", 3), ("ceil", 3), ("exact", 2.5)):
            tariffs.update({"billing_rounding": mode}, "test")
            parking, charging, billable = main.compute_charge(s)
            assert billable == minutes and parking == charging == round(minutes * 1.1, 2)
        tariffs.update({"parking_rate_per_minute": 2}, "auditor")
        assert main.compute_charge(s) == (5.5, 5.5, 2.5)
        tariffs.update({"billing_basis": "planned", "electric_split_charging": False}, "test")
        s.planned_minutes = 4
        assert main.compute_charge(s) == (17.6, 0, 4)
    finally:
        tariffs.update(before, "test cleanup")


def test_accessible_prefers_accessible_bay(park):
    state.spots["S99"] = Spot("S99", parking_for_car_type="Accessible", is_accessible=True)
    assert asyncio.run(main.dispatch_entry("ACCESS", "ENTRY1", "Accessible", dry_run=True))["target"] == "S99"


def test_ghost_car_closes_barrier_and_waits_for_real_payment(park):
    state.barriers["exitGate"] = Barrier("exitGate", state=BarrierPosition.OPEN)
    async def scenario():
        await main._handle_ghost_car("GHOST", "EXIT", {})
        await main._handle_ghost_car("GHOST", "EXIT", {})
        rows = db.query("SELECT * FROM ghost_car_events")
        assert len(rows) == 1
        main.client.barrier_close.assert_awaited()
        await main.ghost_car_override(rows[0]["id"], {"username": "operator", "role": "facility_operator"})
        assert not state.sessions["GHOST"].paid
        main.client.car_goto.assert_not_awaited()
        await main._handle_payment_made({"CarPlateNumber": "GHOST", "Amount": state.sessions["GHOST"].expected_amount})
        main.client.car_goto.assert_awaited_once_with("GHOST", "leavepark")
    asyncio.run(scenario())


def test_fake_payment_held_even_with_valid_signature(park):
    s = state.start_session("FAKE", "ENTRY1", planned_minutes=2)
    s.exit_gate = "EXIT"
    state.barriers["out"] = Barrier("out", state=BarrierPosition.OPEN)
    async def scenario():
        await main._charge_at_exit("FAKE", "EXIT")
        payload = {"EventClass": "payment_made", "EventId": uuid.uuid4().hex, "CarPlateNumber": "FAKE", "Amount": "0.01"}
        payload["Signature"] = digest(payload)
        assert verify(payload).ok
        await main._handle_payment_made(payload)
        assert not s.paid and "FAKE" in state.barriers["out"].held_vehicles
        main.client.car_goto.assert_not_awaited()
    asyncio.run(scenario())


def test_environment_dynamic_ids_and_hysteresis(park):
    state.lights["unusual-light-42"] = Light("unusual-light-42", is_on=True)
    state.fans["F"] = ExhaustFan("F", zone_parent="Z")
    async def scenario():
        await schedule_lights("2026-09-19 07:00:00", state, main.client, main.act)
        main.client.light_off.assert_awaited_once_with("unusual-light-42")
        await schedule_lights("2026-09-19 18:59:59", state, main.client, main.act)
        assert main.client.light_off.await_count == 1
        await schedule_lights("2026-09-19 19:00:00", state, main.client, main.act)
        main.client.light_on.assert_awaited_once_with("unusual-light-42")
        for value in (51, 45, 30, 29):
            await main._handle_carbon_monoxide_event({"ZoneName": "Z", "CarbonMonoxideLevel": value})
        main.client.fan_on.assert_awaited_once_with("F")
        main.client.fan_off.assert_awaited_once_with("F")
    asyncio.run(scenario())


def test_orphan_and_neglected_sessions_are_durable(park):
    orphan = state.start_session("ORPHAN", "ENTRY1")
    state.mark_parked("ORPHAN", "S1")
    state.mark_left_spot("ORPHAN")
    orphan.left_spot_at -= 1000
    neglected = state.start_session("NEGLECT", "ENTRY1")
    neglected.created_at -= 1000
    neglected.entry_departed = True
    asyncio.run(main.reap_orphans())
    assert not state.sessions
    assert db.query("SELECT * FROM sessions WHERE plate = 'ORPHAN'")[0]["zone"] == "ZONE1"
    assert db.query("SELECT * FROM neglected_vehicles WHERE plate = 'NEGLECT'")


def test_client_auth_recovery_single_sync_and_no_charge_transport_retry():
    calls = []
    recovered = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("login"):
            return httpx.Response(200, json={"token": "fresh"})
        if request.headers.get("Authorization") == "Bearer stale":
            return httpx.Response(401)
        if request.url.path.endswith("charge"):
            return httpx.Response(500)
        return httpx.Response(200, json=[])
    async def scenario():
        c = SimulatorClient()
        await c._http.aclose()
        c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://sim")
        c._token = "stale"
        async def recover():
            recovered.append(True)
            await c.list_parking_spots()
        c.on_reconnect = recover
        await c.barrier_open("G")
        assert len(recovered) == 1
        with pytest.raises(httpx.HTTPStatusError):
            await c.car_charge("P", 1)
        assert sum(p.endswith("charge") for p in calls) == 1
        await c.aclose()
    asyncio.run(scenario())


def test_dry_run_skips_gate_fan_light_and_charge(park, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, autopilot=False))
    state.barriers["G"] = Barrier("G")
    state.fans["F"] = ExhaustFan("F")
    state.lights["L"] = Light("L")
    state.start_session("DRY", "ENTRY1")
    async def scenario():
        await main.manual_barrier_open("G", {"username": "admin", "role": "admin"})
        await main.manual_fan_on("F", {"username": "admin", "role": "admin"})
        await main.manual_light_off("L", {"username": "admin", "role": "admin"})
        await main._charge_at_exit("DRY", "EXIT")
    asyncio.run(scenario())
    for name in ("barrier_open", "fan_on", "light_off", "car_charge"):
        getattr(main.client, name).assert_not_awaited()
    assert not state.sessions["DRY"].charge_attempted


def test_wear_85_percent_defers_occupied_bay_and_resets(park, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, wear_cycle_threshold=100))
    queued = AsyncMock()
    monkeypatch.setattr(main.maintenance_queue, "submit", queued)
    state.spots["S1"].cycle_count = 85
    state.spots["S1"].occupant_plate = "OCCUPANT"
    state.barriers["WORN"] = Barrier("WORN", cycle_count=84)
    async def scenario():
        await main.check_wear()
        assert state.deferred_repairs["S1"] == "ParkingSpot"
        queued.assert_not_awaited()
        state.barriers["WORN"].cycle_count = 85
        await main.check_wear()
        assert queued.await_count == 1
        await main.check_wear()
        assert queued.await_count == 1
        await main._handle_component_fixed({"Type": "BarrierGate", "Name": "WORN"})
        assert state.barriers["WORN"].cycle_count == 0
        assert db.query("SELECT cycle_count FROM component_wear WHERE name = 'WORN'")[0]["cycle_count"] == 0
    asyncio.run(scenario())


def test_paid_car_waits_for_manual_hold_then_releases(park):
    state.barriers["G"] = Barrier("G", operator_override=True)
    s = state.start_session("WAIT", "ENTRY1", planned_minutes=2)
    s.exit_gate = "EXIT"
    async def scenario():
        await main._charge_at_exit("WAIT", "EXIT")
        await main._handle_payment_made({"CarPlateNumber": "WAIT", "Amount": s.expected_amount})
        assert s.paid and not s.released
        main.client.car_goto.assert_not_awaited()
        await main.manual_barrier_open("G", {"username": "operator", "role": "facility_operator"})
        assert s.released
        main.client.car_goto.assert_awaited_once_with("WAIT", "leavepark")
    asyncio.run(scenario())


def test_daily_report_groups_persisted_zone(park):
    s = state.start_session("REPORT", "ENTRY1")
    state.assign_spot("REPORT", "S1")
    main._archive("REPORT")
    result = asyncio.run(main.daily_report({"role": "facility_operator"}))
    assert any(r["zone"] == "ZONE1" and r["sessions"] >= 1 for r in result["throughput_by_zone"])
    assert "revenue" not in result
