"""Level reloads and recycled plates.

The simulator draws plates from a fixed list (settings/plates.txt), so every
level load replays the same plates. A returning plate must start a fresh visit,
and a new level must not inherit the previous level's cars, holds or gates.
The only level-load signal is the simulator's own console line
``Load Game./settings/lvl2.json``, captured to a log file by START.bat.
"""
import asyncio
import dataclasses
import time

import pytest

from app import db, layout, main, simlog
from app.state import Barrier, BarrierPosition, SessionPhase, Spot, SpotStatus, VehicleSession, state


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, autopilot=True, game_speed=8))
    for table in (state.spots, state.barriers, state.sessions, state.active_dispatches, state.pending_repairs):
        table.clear()
    main._left_entry.clear()
    main._waiting_at.clear()
    main._holding_gate.clear()
    sent = []

    async def car_goto(plate, destination):
        sent.append(("goto", plate, destination))

    async def barrier(name):
        sent.append(("gate", name))

    monkeypatch.setattr(main.client, "car_goto", car_goto)
    monkeypatch.setattr(main.client, "barrier_open", barrier)
    monkeypatch.setattr(main.client, "barrier_close", barrier)
    monkeypatch.setattr(main, "_live_bays_synced", True)
    yield sent
    for task in list(main._entry_watchers):
        task.cancel()
    layout.announce_level(None)


def _entry(plate, sensor="ENTRY1"):
    return {"EventClass": "car_spot_action", "CarPlateNumber": plate, "SpotName": sensor,
            "SpotType": "EntrySpot", "Direction": "CarIn", "CarType": "Normal",
            "PlannedParkingDurationInMinutes": "3"}


# --------------------------------------------------------------------------- #
# B. One session per visit
# --------------------------------------------------------------------------- #
def test_returning_plate_starts_a_fresh_visit_and_is_dispatched(live):
    state.spots["S1"] = Spot("S1")
    state.spots["S2"] = Spot("S2", status=SpotStatus.OCCUPIED, occupant_plate="OLD 001")
    old = VehicleSession(plate="OLD 001", entry_gate="ENTRY1", phase=SessionPhase.CHARGED,
                         assigned_spot="S2", exit_gate="Exit100")
    old.parked_at, old.exit_confirmed, old.charged, old.charge_attempted, old.paid = time.monotonic(), True, True, True, True
    state.sessions["OLD 001"] = old
    state.active_dispatches["OLD 001"] = "S2"

    async def scenario():
        await main._handle_car_spot_action(_entry("OLD 001"))
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    fresh = state.get_session("OLD 001")
    assert fresh is not None and fresh.session_id != old.session_id
    assert not (fresh.charged or fresh.charge_attempted or fresh.paid or fresh.exit_confirmed)
    assert fresh.assigned_spot == "S1" and ("goto", "OLD 001", "S1") in live
    assert db.query("SELECT COUNT(*) AS n FROM sessions WHERE session_id = ?", (old.session_id,))[0]["n"] == 1


def test_car_still_driving_to_its_bay_keeps_its_visit_at_the_next_sensor(live):
    state.spots["S1"] = Spot("S1")
    assert state.reserve_spot("S1", "RUN 002")
    session = VehicleSession(plate="RUN 002", entry_gate="ENTRY1", phase=SessionPhase.ASSIGNED, assigned_spot="S1")
    state.sessions["RUN 002"] = session
    state.active_dispatches["RUN 002"] = "S1"

    asyncio.run(main._handle_car_spot_action(_entry("RUN 002", "ENTRY2")))
    assert state.get_session("RUN 002") is session and live == []


# --------------------------------------------------------------------------- #
# A. Level load detection from the simulator console log
# --------------------------------------------------------------------------- #
def test_follower_reads_powershell_utf16_output_and_skips_old_lines(tmp_path):
    path = tmp_path / "simulator.log"
    path.write_bytes(b"\xff\xfe" + "Load Game./settings/lvl1.json\r\n".encode("utf-16-le"))
    follower = simlog.LogFollower(path)
    assert follower.read_new_lines() == []                      # a load from before we started

    with open(path, "ab") as f:
        chunk = "Stats\r\nLoad Game./settings/lvl2.json\r\nPoint A".encode("utf-16-le")
        f.write(chunk[:-3])                                     # a write cut mid-character
    lines = follower.read_new_lines()
    assert [simlog.level_loaded(l) for l in lines] == [None, "lvl2"]
    with open(path, "ab") as f:
        f.write(chunk[-3:] + "\r\n".encode("utf-16-le"))
    assert follower.read_new_lines() == ["Point A"]


def test_follower_starts_over_when_the_simulator_is_relaunched(tmp_path):
    path = tmp_path / "simulator.log"
    path.write_text("x" * 200 + "\n", encoding="utf-8")
    follower = simlog.LogFollower(path)
    follower.read_new_lines()
    path.write_text("Load Game./settings/lvl2.json\n", encoding="utf-8")   # smaller: a new file
    assert [simlog.level_loaded(l) for l in follower.read_new_lines()] == ["lvl2"]


def test_level_load_clears_the_previous_levels_cars_holds_and_closes_gates(live, monkeypatch):
    state.barriers["gate2"] = Barrier("gate2", operator_override=True, held_vehicles={"GHO 001"})
    db.set_meta("gate_override:gate2", "1")
    ghost = VehicleSession(plate="GHO 001", entry_gate="ENTRY1", phase=SessionPhase.CHARGED)
    ghost.charged = True
    state.sessions["GHO 001"] = ghost
    state.active_dispatches["GHO 001"] = "S9"
    db.save_active_session(ghost)
    synced = []

    async def fake_sync():
        synced.append(True)
        state.barriers["gate2"] = Barrier("gate2", state=BarrierPosition.OPEN,
                                          operator_override=db.get_meta("gate_override:gate2") == "1")
        state.barriers["gate7"] = Barrier("gate7", state=BarrierPosition.OPEN)
        state.spots["S1"] = Spot("S1")
        main._live_bays_synced = True
        await main._close_gates_for_level_start()
        return {}

    monkeypatch.setattr(main, "sync_from_simulator", fake_sync)
    monkeypatch.setattr(main, "LEVEL_SYNC_RETRY_S", 0.0)
    main._gates_closed_for_level = True

    asyncio.run(main._on_level_loaded("lvl2"))
    assert synced and not state.sessions and not state.active_dispatches
    assert db.query("SELECT COUNT(*) AS n FROM active_sessions WHERE plate = 'GHO 001'")[0]["n"] == 0
    assert not state.barriers["gate2"].operator_override
    assert {("gate", "gate2"), ("gate", "gate7")} <= set(live)
    assert layout.running_level() == "lvl2"


def test_level_load_clears_holds_even_when_no_gates_are_loaded_yet(live, monkeypatch):
    # The startup sync ran before a level was clicked, so no gates were in
    # memory when the level loaded. gate2's saved hold must still go.
    db.set_meta("gate_override:gate2", "1")
    db.set_meta("gate_holds:gate6", '["GHO 002"]')

    async def fake_sync():
        main._live_bays_synced = True
        return {}

    monkeypatch.setattr(main, "sync_from_simulator", fake_sync)
    monkeypatch.setattr(main, "LEVEL_SYNC_RETRY_S", 0.0)
    asyncio.run(main._on_level_loaded("lvl2"))
    assert db.get_meta("gate_override:gate2") == "0"
    assert db.get_meta("gate_holds:gate6") == "[]"
