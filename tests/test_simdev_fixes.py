"""simdev fixes: fixed fan hysteresis, preventive bay repair, ghost cars and
zone maintenance (engineering log 4.23-4.26)."""
import asyncio
import dataclasses

import pytest

from app import db, main

REAL_PERSIST = main._persist   # the ghost flow needs the real active_sessions row (claim_charge)
from app.state import (Barrier, BarrierPosition, ExhaustFan, Light, SessionPhase, Spot, SpotStatus,
                       VehicleSession, state)


@pytest.fixture
def sim(monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(
        main.settings, autopilot=True, game_speed=8, co_fan_on_threshold=50.0, co_fan_off_threshold=15.0))
    for table in (state.spots, state.barriers, state.sessions, state.active_dispatches,
                  state.pending_repairs, state.fans, state.zones, state.deferred_repairs,
                  state.lights):
        table.clear()
    main._left_entry.clear()
    main._waiting_at.clear()
    main._holding_gate.clear()
    main._last_motion.clear()
    state.fake_payments.clear()
    sent = []

    def recorder(kind):
        async def call(*args):
            sent.append((kind, *args))
        return call

    for kind in ("fan_on", "fan_off", "barrier_open", "barrier_close", "barrier_repair",
                 "spot_repair", "car_goto", "car_charge", "light_on", "light_off"):
        monkeypatch.setattr(main.client, kind, recorder(kind))
    monkeypatch.setattr(main, "_persist", lambda plate: None)
    monkeypatch.setattr(main, "_live_bays_synced", True)
    with db._lock, db._conn:   # the test database is shared across files
        db._conn.execute("DELETE FROM meta WHERE key LIKE 'pending_proactive_repair:PM%'")
    yield sent
    with db._lock, db._conn:
        db._conn.execute("DELETE FROM meta WHERE key LIKE 'pending_proactive_repair:PM%'")
    for task in list(main._entry_watchers):
        task.cancel()


# --------------------------------------------------------------------------- #
# 3. Fans: on above 50, keep ventilating down to 15
# --------------------------------------------------------------------------- #
def _co(level):
    return {"EventClass": "carbon_monoxide_event", "ZoneName": "ZONE1",
            "CarbonMonoxideLevel": str(level), "DangerLevel": "Safe"}


def test_fan_switches_on_above_50_and_runs_until_below_15(sim):
    state.fans["fan1"] = ExhaustFan("fan1", zone_parent="ZONE1")
    state.spots["S1"] = Spot("S1", zone_parent="ZONE1", status=SpotStatus.OCCUPIED)  # a full zone must not lower the edge

    for level, expect_on in ((45, False), (50, False), (51, True), (40, True), (16, True), (15, True), (14, False)):
        asyncio.run(main._handle_carbon_monoxide_event(_co(level)))
        assert state.fans["fan1"].is_on is expect_on, f"CO {level}"
    assert [c[0] for c in sim] == ["fan_on", "fan_off"]


# --------------------------------------------------------------------------- #
# 4. Bays: preventive repair after SPOT_PREVENTIVE_PARKS parks since repair
# --------------------------------------------------------------------------- #
def _park(plate, bay, direction="CarIn"):
    return {"EventClass": "car_spot_action", "CarPlateNumber": plate, "SpotName": bay,
            "SpotType": "Park", "Direction": direction, "PlannedParkingDurationInMinutes": "3"}


def test_each_park_counts_and_an_empty_worn_bay_is_repaired(sim, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, spot_preventive_parks=3))
    state.spots["PM1"] = Spot("PM1", zone_parent="ZONE1")
    for i in range(3):
        plate = f"PRK {i:03d}"
        asyncio.run(main._handle_car_spot_action(_park(plate, "PM1")))
        asyncio.run(main._handle_car_spot_action(_park(plate, "PM1", "CarOut")))
    assert state.spots["PM1"].cycle_count == 3
    asyncio.run(main.check_wear())
    assert "PM1" in state.pending_repairs, "preventive repair queued for the empty worn bay"


def test_a_worn_bay_with_a_car_in_it_is_repaired_only_after_the_car_leaves(sim, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, spot_preventive_parks=3))
    state.spots["PM2"] = Spot("PM2", zone_parent="ZONE1", cycle_count=2)
    asyncio.run(main._handle_car_spot_action(_park("PRK 010", "PM2")))       # third park: now worn, and occupied
    asyncio.run(main.check_wear())
    assert "PM2" not in state.pending_repairs and "PM2" in state.deferred_repairs
    asyncio.run(main._handle_car_spot_action(_park("PRK 010", "PM2", "CarOut")))
    assert "PM2" in state.pending_repairs, "repair queued the moment the bay emptied"
    assert "PM2" not in state.available_spots(), "a bay awaiting repair is never dispatched to"


def test_a_bay_below_the_threshold_is_left_alone(sim, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, spot_preventive_parks=3))
    state.spots["PM3"] = Spot("PM3", zone_parent="ZONE1", cycle_count=2)
    asyncio.run(main.check_wear())
    assert "PM3" not in state.pending_repairs and "PM3" not in state.deferred_repairs


def test_a_level_load_zeroes_saved_wear_so_bays_are_not_all_repaired_at_once(sim):
    db.sync_component_wear("PM4", "ParkingSpot", 12, 0)
    db.reset_live_level()
    assert db.query("SELECT cycle_count FROM component_wear WHERE name = 'PM4'")[0]["cycle_count"] == 0


# --------------------------------------------------------------------------- #
# 2. Ghost cars: billed automatically, released only by staff
# --------------------------------------------------------------------------- #
STAFF = {"username": "operator", "role": "facility_operator"}


@pytest.fixture
def ghost(sim, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, exit_charge_delay_s=0.0, min_dwell_time_s=0.0))
    monkeypatch.setattr(main.ml_agent, "ghost_car_anomaly_imputation",
                        lambda plate, car_type: {"imputed_fee": 4.0, "confidence_score": 0.5, "median_duration": 3.0})
    state.barriers["gate6"] = Barrier("gate6", zone_parent="ZONE3", state=BarrierPosition.OPEN)
    monkeypatch.setattr(main, "_barrier_for_sensor", lambda sensor: "gate6" if sensor == "Exit100" else None)
    monkeypatch.setattr(main, "_persist", REAL_PERSIST)
    with db._lock, db._conn:
        db._conn.execute("DELETE FROM ghost_car_events")
        db._conn.execute("DELETE FROM active_sessions WHERE plate LIKE 'GHO %'")
    yield sim


def _exit(plate, direction="CarIn"):
    return {"EventClass": "car_spot_action", "CarPlateNumber": plate, "SpotName": "Exit100",
            "SpotType": "ExitSpot", "Direction": direction, "CarType": "Normal"}


def _pay(plate, amount):
    return {"EventClass": "payment_made", "CarPlateNumber": plate, "Amount": str(amount),
            "EventId": f"pay-{plate}-{amount}"}


def _arrive_ghost(plate):
    async def scenario():
        await main._handle_car_spot_action(_exit(plate))
        await asyncio.sleep(0.05)
    asyncio.run(scenario())
    return db.query("SELECT * FROM ghost_car_events WHERE plate = ?", (plate,))[0]["id"]


def test_unknown_car_at_the_exit_is_billed_automatically_and_held(ghost):
    _arrive_ghost("GHO 100")
    assert ("car_charge", "GHO 100", 4.0, 0.0) in ghost, "invoice sent with the ML fee, no staff click"
    assert ("barrier_close", "gate6") in ghost
    assert not any(c[0] == "car_goto" for c in ghost)


def test_a_car_that_appeared_in_a_bay_is_a_ghost_too(ghost):
    state.spots["P70"] = Spot("P70", zone_parent="ZONE3")
    park = {"EventClass": "car_spot_action", "CarPlateNumber": "GHO 101", "SpotName": "P70",
            "SpotType": "Park", "Direction": "CarIn", "PlannedParkingDurationInMinutes": "3"}
    asyncio.run(main._handle_car_spot_action(park))          # never seen at an entrance
    asyncio.run(main._handle_car_spot_action({**park, "Direction": "CarOut"}))
    _arrive_ghost("GHO 101")
    assert ("car_charge", "GHO 101", 4.0, 0.0) in ghost
    assert db.query("SELECT COUNT(*) AS n FROM ghost_car_events WHERE plate = 'GHO 101'")[0]["n"] == 1


def test_a_ghost_car_that_pays_is_let_out_automatically(ghost):
    # 4.38: bill with the ML fee; a valid payment releases it (with a notice).
    ghost_id = _arrive_ghost("GHO 102")

    async def pay():
        asyncio.get_running_loop().call_later(0.02, state.update_barrier_state, "gate6", "Open")
        await main._handle_payment_made(_pay("GHO 102", 4.0))
    asyncio.run(pay())
    assert ghost.index(("barrier_open", "gate6")) < ghost.index(("car_goto", "GHO 102", "leavepark"))
    assert db.query("SELECT resolved FROM ghost_car_events WHERE id = ?", (ghost_id,))[0]["resolved"] == 1


def test_releasing_an_unpaid_ghost_needs_explicit_confirmation(ghost):
    ghost_id = _arrive_ghost("GHO 103")
    with pytest.raises(main.HTTPException) as refused:
        asyncio.run(main.ghost_car_release(ghost_id, main.GhostReleaseIn(), STAFF))
    assert refused.value.status_code == 409
    assert not any(c[0] == "car_goto" for c in ghost)

    async def release():
        asyncio.get_running_loop().call_later(0.02, state.update_barrier_state, "gate6", "Open")
        return await main.ghost_car_release(ghost_id, main.GhostReleaseIn(confirm_unpaid=True), STAFF)
    assert asyncio.run(release())["released"]
    assert ("car_goto", "GHO 103", "leavepark") in ghost


def test_only_gate_staff_may_release_a_ghost():
    from app import policy
    assert policy.allowed({"role": "facility_operator"}, "/api/ghost-cars/7/release", "POST")
    assert policy.allowed({"role": "admin"}, "/api/ghost-cars/7/release", "POST")
    assert not policy.allowed({"role": "auditor"}, "/api/ghost-cars/7/release", "POST")
    assert not policy.allowed({"role": "maintenance_technician"}, "/api/ghost-cars/7/release", "POST")



# --------------------------------------------------------------------------- #
# Night lights follow car movement per zone (4.29)
# --------------------------------------------------------------------------- #
NIGHT, DAY = "2026-09-20 02:44:00", "2026-09-20 12:00:00"


def _lights():
    for zone in ("ZONE1", "ZONE2"):
        for i in range(2):
            state.lights[f"{zone}-L{i}"] = Light(f"{zone}-L{i}", zone_parent=zone, is_on=True)
    state.spots["S1"] = Spot("S1", zone_parent="ZONE1")
    state.spots["bay36"] = Spot("bay36", zone_parent="ZONE2")


def _on():
    return sorted(n for n, l in state.lights.items() if l.is_on)


def test_at_night_only_the_zone_with_a_moving_car_is_lit(sim):
    _lights()
    state.reserve_spot("S1", "MOV 001")
    state.sessions["MOV 001"] = VehicleSession(plate="MOV 001", entry_gate="ENTRY1",
                                               phase=SessionPhase.ASSIGNED, assigned_spot="S1", reached_zone=True)
    asyncio.run(main._refresh_lights(NIGHT))
    assert _on() == ["ZONE1-L0", "ZONE1-L1"], "car driving to a ZONE1 bay; ZONE2 is dark"


def test_at_night_all_lights_go_off_once_every_car_is_parked(sim, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, light_hold_s=0.0))
    _lights()
    session = VehicleSession(plate="MOV 002", entry_gate="ENTRY1", phase=SessionPhase.PARKED, assigned_spot="S1")
    session.parked_at, session.zone = 1.0, "ZONE1"
    state.sessions["MOV 002"] = session
    state.mark_spot_occupied("S1", "MOV 002")
    asyncio.run(main._refresh_lights(NIGHT))
    assert _on() == []


def test_a_car_leaving_its_bay_lights_its_zone_until_it_has_gone(sim, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, light_hold_s=0.0))
    _lights()
    session = VehicleSession(plate="MOV 003", entry_gate="ENTRY1", phase=SessionPhase.PARKED, assigned_spot="bay36")
    session.parked_at, session.left_spot_at, session.zone = 1.0, 2.0, "ZONE2"
    state.sessions["MOV 003"] = session
    asyncio.run(main._refresh_lights(NIGHT))
    assert _on() == ["ZONE2-L0", "ZONE2-L1"]
    state.complete_session("MOV 003")
    asyncio.run(main._refresh_lights(NIGHT))
    assert _on() == []


def test_lights_stay_on_briefly_after_the_last_movement(sim, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, light_hold_s=80.0))  # 10 s at speed 8
    _lights()
    state.reserve_spot("S1", "MOV 004")
    state.sessions["MOV 004"] = VehicleSession(plate="MOV 004", entry_gate="ENTRY1",
                                               phase=SessionPhase.ASSIGNED, assigned_spot="S1", reached_zone=True)
    asyncio.run(main._refresh_lights(NIGHT))
    state.sessions.clear()
    asyncio.run(main._refresh_lights(NIGHT))
    assert _on() == ["ZONE1-L0", "ZONE1-L1"], "held on: avoids switching between cars"


def test_by_day_every_light_is_off(sim):
    _lights()
    state.reserve_spot("S1", "MOV 005")
    state.sessions["MOV 005"] = VehicleSession(plate="MOV 005", entry_gate="ENTRY1",
                                               phase=SessionPhase.ASSIGNED, assigned_spot="S1")
    asyncio.run(main._refresh_lights(DAY))
    assert _on() == []


def test_held_lights_switch_off_when_the_hold_ends_without_another_event(sim, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, light_hold_s=0.8))  # 0.1 s at speed 8
    _lights()
    state.reserve_spot("S1", "MOV 006")
    state.sessions["MOV 006"] = VehicleSession(plate="MOV 006", entry_gate="ENTRY1",
                                               phase=SessionPhase.ASSIGNED, assigned_spot="S1", reached_zone=True)

    async def scenario():
        await main._refresh_lights(NIGHT)
        state.sessions.clear()                     # the car parked; no further webhook arrives
        await main._refresh_lights(NIGHT)
        assert _on() == ["ZONE1-L0", "ZONE1-L1"]
        await asyncio.sleep(0.3)
    asyncio.run(scenario())
    assert _on() == [], "switched off by the hold timer, not by the next event"


def test_by_default_a_zone_goes_dark_as_soon_as_its_last_car_parks(sim):
    assert main.settings.light_hold_s == 0.0
    _lights()
    state.reserve_spot("S1", "MOV 007")
    session = VehicleSession(plate="MOV 007", entry_gate="ENTRY1", phase=SessionPhase.ASSIGNED, assigned_spot="S1", reached_zone=True)
    state.sessions["MOV 007"] = session
    asyncio.run(main._refresh_lights(NIGHT))
    assert _on() == ["ZONE1-L0", "ZONE1-L1"]
    state.mark_parked("MOV 007", "S1")
    asyncio.run(main._refresh_lights(NIGHT))
    assert _on() == []


# --------------------------------------------------------------------------- #
# 4.31: no mass repairs. The ML sweep's model predicted ~99% failure for every
# component (107 breakdowns vs 1 preventive repair), so it queued them all.
# --------------------------------------------------------------------------- #
def test_the_ml_sweep_queues_no_repairs_by_default(sim, monkeypatch):
    queued = []

    async def queue(component_type, name):
        queued.append(name)

    monkeypatch.setattr(main.ml_agent, "repair_period_prediction",
                        lambda *a: {"needs_repair": True, "days_to_failure": 30, "failure_probability": 0.99})
    rows = [{"name": "PM9", "type": "ParkingSpot", "cycle_count": 0, "runtime_seconds": 0.0,
             "broken": False, "under_maintenance": False},
            {"name": "fan9", "type": "ExhaustFan", "cycle_count": 1, "runtime_seconds": 5.0,
             "broken": False, "under_maintenance": False}]
    asyncio.run(main.ml_agent.predictive_sweep_once(lambda: rows, queue))
    assert queued == []


def test_a_bay_that_was_never_parked_in_is_never_repaired(sim):
    state.spots["PM8"] = Spot("PM8", zone_parent="ZONE1", cycle_count=0)
    asyncio.run(main.check_wear())
    assert "PM8" not in state.pending_repairs and "PM8" not in state.deferred_repairs


def test_only_one_fan_per_zone_is_repaired_at_a_time(sim):
    for name in ("fanA", "fanB"):
        state.fans[name] = ExhaustFan(name, zone_parent="ZONE1")
    state.fans["fanC"] = ExhaustFan("fanC", zone_parent="ZONE2")
    for name in ("fanA", "fanB", "fanC"):
        asyncio.run(main._queue_repair("ExhaustFan", name))
    fans_in_repair = sorted(n for n in state.pending_repairs if n in state.fans)
    assert fans_in_repair == ["fanA", "fanC"], "ZONE1 keeps fanB ventilating while fanA is repaired"
    asyncio.run(main._handle_component_fixed({"Type": "ExhaustFan", "Name": "fanA"}))
    asyncio.run(main._queue_repair("ExhaustFan", "fanB"))   # its trigger fires again
    assert "fanB" in state.pending_repairs, "fanA is back, so fanB may go now"


def test_a_zone_lights_up_only_once_its_car_reaches_that_zones_sensor(sim, monkeypatch):
    monkeypatch.setattr(main, "running_level", lambda: "lvl2")
    for name, zone in (("gate1", "ZONE1"), ("gate3", "ZONE2"), ("gate5", "ZONE3")):
        state.barriers[name] = Barrier(name, zone_parent=zone)
    for i in range(2):
        state.lights[f"ZONE3-L{i}"] = Light(f"ZONE3-L{i}", zone_parent="ZONE3", is_on=False)
    state.spots["P69"] = Spot("P69", zone_parent="ZONE3")
    state.reserve_spot("P69", "MOV 010")
    state.sessions["MOV 010"] = VehicleSession(plate="MOV 010", entry_gate="ENTRY1",
                                               phase=SessionPhase.ASSIGNED, assigned_spot="P69")
    state.active_dispatches["MOV 010"] = "P69"
    asyncio.run(main._refresh_lights(NIGHT))
    assert _on() == [], "dispatched at ENTRY1, still on the road: ZONE3 stays dark"

    def sensor(name, direction):
        return {"EventClass": "car_spot_action", "CarPlateNumber": "MOV 010", "SpotName": name,
                "SpotType": "EntrySpot", "Direction": direction}
    asyncio.run(main._handle_car_spot_action(sensor("ENTRY2", "CarIn")))
    asyncio.run(main._refresh_lights(NIGHT))
    assert _on() == [], "passing ENTRY2 is not ZONE3"
    asyncio.run(main._handle_car_spot_action(sensor("ENTRY3", "CarIn")))
    asyncio.run(main._refresh_lights(NIGHT))
    assert _on() == ["ZONE3-L0", "ZONE3-L1"]


def test_staff_open_on_the_ringed_gate_lets_the_ghost_car_out(ghost):
    ghost_id = _arrive_ghost("GHO 110")
    gate6 = next(b for b in state.snapshot()["barriers"] if b["name"] == "gate6")
    assert gate6["held_plates"] == ["GHO 110"], "the map rings gate6 orange for this car"

    async def open_it():
        asyncio.get_running_loop().call_later(0.02, state.update_barrier_state, "gate6", "Open")
        return await main.manual_barrier_open("gate6", {"username": "admin", "role": "admin"})
    result = asyncio.run(open_it())
    assert result["released"] == ["GHO 110"]
    assert ("car_goto", "GHO 110", "leavepark") in ghost
    assert db.query("SELECT resolved FROM ghost_car_events WHERE id = ?", (ghost_id,))[0]["resolved"] == 1



# --------------------------------------------------------------------------- #
# 4.38: a forged payment (bad signature) for a car we billed at an exit is
# rejected as before, but the car is held and staff are told. The untrusted
# webhook only ever makes us more careful: it never marks paid or releases.
# --------------------------------------------------------------------------- #
def _billed_at_exit(plate):
    session = VehicleSession(plate=plate, entry_gate="ENTRY1", phase=SessionPhase.CHARGED, exit_gate="Exit100")
    session.charge_attempted, session.expected_amount, session.parked_at = True, 2.2, 1.0
    state.sessions[plate] = session
    return session


def test_a_forged_payment_holds_the_car_and_tells_staff(ghost):
    session = _billed_at_exit("FAK 001")
    asyncio.run(main._flag_forged_payment({"EventClass": "payment_made", "CarPlateNumber": "FAK 001", "Amount": "2.20"}))
    assert not session.paid and session.payment_suspect
    gate6 = next(b for b in state.snapshot()["barriers"] if b["name"] == "gate6")
    assert gate6["held_plates"] == ["FAK 001"] and gate6["held_notes"]["FAK 001"] == "fake payment"
    assert not any(c[0] == "car_goto" for c in ghost)


def test_a_forged_payment_for_a_car_we_did_not_bill_is_ignored(ghost):
    asyncio.run(main._flag_forged_payment({"EventClass": "payment_made", "CarPlateNumber": "NOB 001", "Amount": "9"}))
    assert not state.fake_payments and not state.barriers["gate6"].held_vehicles


def test_the_webhook_still_rejects_a_forged_payment_but_flags_it(ghost):
    from fastapi.testclient import TestClient
    _billed_at_exit("FAK 002")
    response = TestClient(main.app).post("/webhooks/simulator", json={
        "EventClass": "payment_made", "CarPlateNumber": "FAK 002", "Amount": "2.20",
        "EventId": "fake-evt-1", "Signature": "0" * 32})
    assert response.status_code == 401
    assert "FAK 002" in state.fake_payments and "FAK 002" in state.barriers["gate6"].held_vehicles
