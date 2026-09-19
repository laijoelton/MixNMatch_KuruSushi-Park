"""Zone balancing and per-car gate control.

Level 2 has one road in (the operator-only main gate, gate7) and a gate per
zone on each side: entry gates 1/3/5 and exit gates 2/4/6. A car goes to the
zone with the lowest (occupied + reserved + broken + under repair) / bays
ratio; that zone's entry gate opens for it and closes once it has parked.
"""
import asyncio
import dataclasses

import pytest

from app import main, zones
from app.state import Barrier, BarrierPosition, SessionPhase, Spot, SpotStatus, VehicleSession, state


# --------------------------------------------------------------------------- #
# Pure zone arithmetic
# --------------------------------------------------------------------------- #
def _bay(name, zone, status=SpotStatus.AVAILABLE, **kw):
    return Spot(name, zone_parent=zone, status=status, **kw)


def test_ratio_counts_every_unusable_bay_once():
    bays = [
        _bay("A1", "ZONE1", SpotStatus.OCCUPIED),
        _bay("A2", "ZONE1", SpotStatus.RESERVED),
        _bay("A3", "ZONE1", SpotStatus.OCCUPIED, broken=True),   # broken with a car in it: one bay, not two
        _bay("A4", "ZONE1", under_maintenance=True),
        _bay("A5", "ZONE1"),                                      # repair queued
        _bay("A6", "ZONE1"),
        _bay("A7", "ZONE1"),
        _bay("A8", "ZONE1"),
        _bay("ENTRY1", "", purpose="EntrySpot"),                  # not a parking bay
    ]
    assert zones.zone_ratios(bays, pending={"A5"}) == {"ZONE1": 5 / 8}


def test_lowest_ratio_wins_and_ties_go_to_the_nearer_zone():
    ratios = {"ZONE1": 0.42, "ZONE2": 0.35, "ZONE3": 0.26}
    assert zones.pick_zone({"ZONE1", "ZONE2", "ZONE3"}, ratios) == "ZONE3"
    assert zones.pick_zone({"ZONE1", "ZONE2"}, ratios) == "ZONE2"
    assert zones.pick_zone({"ZONE10", "ZONE2"}, {"ZONE10": 0.1, "ZONE2": 0.1}) == "ZONE2"
    assert zones.pick_zone(set(), ratios) is None


def test_entry_gate_is_the_zone_gate_nearest_an_entry_sensor():
    barriers = [Barrier("gate3", zone_parent="ZONE2"), Barrier("gate4", zone_parent="ZONE2"),
                Barrier("gate7", zone_parent="ZONE2")]
    positions = {"gate3": (333, 2448), "gate4": (2592, 2495), "gate7": (190, 2400)}
    entries = [(192, 2350)]
    assert zones.entry_gate_for_zone("ZONE2", barriers, positions, entries, exclude={"gate7"}) == "gate3"
    assert zones.entry_gate_for_zone("ZONE9", barriers, positions, entries, exclude=set()) is None
    assert zones.entry_gate_for_zone("", barriers, positions, entries, exclude=set()) is None


# --------------------------------------------------------------------------- #
# Dispatcher behaviour on the real Level 2 layout
# --------------------------------------------------------------------------- #
class _Commands(list):
    gate_states: list


@pytest.fixture
def lvl2(monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(
        main.settings, autopilot=True, main_gate="gate7", game_speed=8, gate_close_delay_s=0.0,
        entry_gate_close_delay_s=0.8))
    monkeypatch.setattr(main, "running_level", lambda: "lvl2")
    state.spots.clear()
    state.barriers.clear()
    state.sessions.clear()
    state.active_dispatches.clear()
    state.pending_repairs.clear()
    main._left_entry.clear()
    main._waiting_at.clear()
    main._holding_gate.clear()
    for name, zone in (("gate1", "ZONE1"), ("gate2", ""), ("gate3", "ZONE2"), ("gate4", "ZONE2"),
                       ("gate5", "ZONE3"), ("gate6", "ZONE3"), ("gate7", "")):
        state.barriers[name] = Barrier(name, zone_parent=zone)
    state.spots["ENTRY1"] = Spot("ENTRY1", purpose="EntrySpot")
    commands = _Commands()
    gate_states = []

    async def barrier_open(name):
        commands.append(("open", name))
        # The simulator reports the barrier Open a moment after the command.
        asyncio.get_running_loop().call_later(0.03, state.update_barrier_state, name, "Open")

    async def barrier_close(name):
        commands.append(("close", name))

    async def car_goto(plate, destination):
        commands.append(("goto", plate, destination))
        gate_states.append({n: b.state.value for n, b in state.barriers.items()})

    monkeypatch.setattr(main.client, "barrier_open", barrier_open)
    monkeypatch.setattr(main.client, "barrier_close", barrier_close)
    monkeypatch.setattr(main.client, "car_goto", car_goto)
    monkeypatch.setattr(main, "_persist", lambda plate: None)
    monkeypatch.setattr(main, "GATE_OPEN_WAIT_S", 2.0)
    commands.gate_states = gate_states
    monkeypatch.setattr(main, "_live_bays_synced", True)
    yield commands
    for task in list(main._entry_watchers):
        task.cancel()


def _fill(zone, names, occupied=0):
    for i, name in enumerate(names):
        state.spots[name] = Spot(name, zone_parent=zone,
                                 status=SpotStatus.OCCUPIED if i < occupied else SpotStatus.AVAILABLE)


def test_car_goes_to_the_emptiest_zone_through_that_zones_gate(lvl2):
    _fill("ZONE1", ["S1", "S3", "S4"], occupied=2)       # 2/3 full
    _fill("ZONE2", ["bay36", "bay37", "bay39"], occupied=1)  # 1/3
    _fill("ZONE3", ["P69", "P70", "P71"], occupied=0)    # empty

    async def scenario():
        result = await main.dispatch_entry("ZON 001", "ENTRY1", "Normal")
        await asyncio.sleep(0.05)
        return result

    result = asyncio.run(scenario())
    assert result["dispatched"] and result["target"] in {"P69", "P70", "P71"}
    # ZONE3 is entered through gate5, in front of ENTRY3. At ENTRY1 no gate
    # opens: the car is sent to ENTRY3 first (see the two-hop tests below).
    assert [c for c in lvl2 if c[0] == "open"] == [], lvl2
    assert ("goto", "ZON 001", "ENTRY3") in lvl2


def test_held_zone_gate_diverts_to_the_next_zone(lvl2):
    _fill("ZONE1", ["S1", "S3"], occupied=1)
    _fill("ZONE3", ["P69", "P70"], occupied=0)
    state.barriers["gate5"].operator_override = True     # ZONE3 closed by staff

    result = asyncio.run(main.dispatch_entry("ZON 002", "ENTRY1", "Normal"))
    assert result["target"] in {"S1", "S3"}


def test_every_zone_gate_held_keeps_the_car_waiting_not_turned_away(lvl2):
    _fill("ZONE1", ["S1"], occupied=0)
    state.barriers["gate1"].operator_override = True

    result = asyncio.run(main.dispatch_entry("ZON 003", "ENTRY1", "Normal"))
    assert not result["dispatched"] and not result.get("turned_away")
    assert result["reason"] == "Held closed by operator"


def _sensor(plate, sensor, direction):
    return {"EventClass": "car_spot_action", "CarPlateNumber": plate, "SpotName": sensor,
            "SpotType": "EntrySpot", "Direction": direction}


def test_zone_gate_closes_as_soon_as_its_car_has_left_the_sensor_box(lvl2):
    _fill("ZONE3", ["P69"])
    _assigned("ZON 004", "P69", sensor="ENTRY3")
    main._waiting_at["ZON 004"] = "ENTRY3"

    async def scenario():
        leg = asyncio.create_task(main._resend_if_still_at_entry("ZON 004", "P69", initial=True, sensor="ENTRY3"))
        await asyncio.sleep(0.1)
        assert ("open", "gate5") in lvl2
        await main._handle_car_spot_action(_sensor("ZON 004", "ENTRY3", "CarOut"))
        assert ("close", "gate5") not in lvl2, "the car is between the sensor and the gate"
        await asyncio.sleep(0.3)
        await leg

    asyncio.run(scenario())
    assert ("close", "gate5") in lvl2
    assert state.sessions["ZON 004"].parked_at is None, "closed before the car parked"


def test_the_next_car_keeps_the_gate_open_and_closes_it_after_itself(lvl2):
    _fill("ZONE3", ["P69", "P70"])
    for plate, bay in (("ZON 005", "P69"), ("ZON 006", "P70")):
        _assigned(plate, bay, sensor="ENTRY3")
        main._waiting_at[plate] = "ENTRY3"

    async def scenario():
        for plate, bay in (("ZON 005", "P69"), ("ZON 006", "P70")):
            main._start_dispatch_retry(plate, bay, sensor="ENTRY3")
        await asyncio.sleep(0.1)
        await main._handle_car_spot_action(_sensor("ZON 005", "ENTRY3", "CarOut"))
        await asyncio.sleep(0.3)
        assert ("close", "gate5") not in lvl2, "ZON 006 is still waiting to go through"
        await main._handle_car_spot_action(_sensor("ZON 006", "ENTRY3", "CarOut"))
        await asyncio.sleep(0.3)

    asyncio.run(scenario())
    assert lvl2.count(("close", "gate5")) == 1


def test_leaving_entry1_does_not_close_a_farther_zone_gate(lvl2, monkeypatch):
    monkeypatch.setattr(main, "HOP_ATTEMPTS", 0)          # the old way: gate5 opened from ENTRY1
    _fill("ZONE3", ["P69"])
    _assigned("ZON 007", "P69")

    async def scenario():
        main._start_dispatch_retry("ZON 007", "P69")
        await asyncio.sleep(0.1)
        await main._handle_car_spot_action(_sensor("ZON 007", "ENTRY1", "CarOut"))
        await asyncio.sleep(0.3)
        assert ("close", "gate5") not in lvl2, "the car has not reached gate5 yet"
        await main._handle_car_spot_action(_sensor("ZON 007", "ENTRY3", "CarIn"))
        await main._handle_car_spot_action(_sensor("ZON 007", "ENTRY3", "CarOut"))
        await asyncio.sleep(0.3)

    asyncio.run(scenario())
    assert ("close", "gate5") in lvl2


def test_idle_close_never_touches_the_main_gate_or_unavailable_gates(lvl2):
    for name in ("gate1", "gate2", "gate6", "gate7"):
        state.barriers[name].state = BarrierPosition.OPEN
    state.barriers["gate2"].broken = True
    state.pending_repairs["gate6"] = "BarrierGate"

    asyncio.run(main._close_idle_gates())
    assert lvl2 == [("close", "gate1")]


def test_exit_gate_stays_open_while_a_paid_car_is_waiting_to_leave(lvl2):
    state.barriers["gate6"].state = BarrierPosition.OPEN
    session = VehicleSession(plate="ZON 006", entry_gate="ENTRY1", phase=SessionPhase.CHARGED, exit_gate="Exit100")
    session.paid = True
    state.sessions["ZON 006"] = session

    asyncio.run(main._close_idle_gates())
    assert ("close", "gate6") not in lvl2
    state.sessions.clear()
    asyncio.run(main._close_idle_gates())
    assert ("close", "gate6") in lvl2


def test_level_start_closes_every_gate_including_the_main_gate_once(lvl2):
    for barrier in state.barriers.values():
        barrier.state = BarrierPosition.OPEN
    state.barriers["gate4"].under_maintenance = True
    main._gates_closed_for_level = False

    asyncio.run(main._close_gates_for_level_start())
    closed = {c[1] for c in lvl2 if c[0] == "close"}
    assert closed == {"gate1", "gate2", "gate3", "gate5", "gate6", "gate7"}

    lvl2.clear()
    state.barriers["gate7"].state = BarrierPosition.OPEN   # operator opened it
    asyncio.run(main._close_gates_for_level_start())
    assert lvl2 == [], "a later sync must not shut the main gate on the operator"


# --------------------------------------------------------------------------- #
# Gates must be Open before a goto: the simulator plans the route at goto time
# and treats a closed or rising gate as a wall ("Paths found: 0" at the entry,
# "No valid escape spot found" at the exit - both seen in its console log).
# --------------------------------------------------------------------------- #
def _assigned(plate, bay, sensor="ENTRY1"):
    state.reserve_spot(bay, plate)
    session = VehicleSession(plate=plate, entry_gate=sensor, phase=SessionPhase.ASSIGNED, assigned_spot=bay)
    state.sessions[plate] = session
    state.active_dispatches[plate] = bay
    return session


def test_zone1_car_waits_for_gate1_to_report_open_before_its_goto(lvl2):
    _fill("ZONE1", ["S1"])
    _assigned("ZON 010", "S1")
    asyncio.run(main._resend_if_still_at_entry("ZON 010", "S1", initial=True))
    assert lvl2[0] == ("open", "gate1")
    first_goto = next(i for i, c in enumerate(lvl2) if c[0] == "goto")
    assert lvl2.gate_states[0]["gate1"] == "Open", "goto sent while gate1 was still rising"
    assert lvl2[first_goto] == ("goto", "ZON 010", "S1")


def test_zone3_car_is_sent_to_entry3_and_gate5_opens_only_when_it_arrives(lvl2):
    _fill("ZONE3", ["P69"])
    _assigned("ZON 011", "P69")

    async def scenario():
        main._start_dispatch_retry("ZON 011", "P69")      # as dispatch_entry does
        hop = main._dispatch_tasks["ZON 011"]
        await asyncio.sleep(0.05)
        assert lvl2 == [("goto", "ZON 011", "ENTRY3")], "no gate may open while the car drives down"
        # The car pulls away from ENTRY1, passes ENTRY2 and stops at ENTRY3.
        for sensor, direction in (("ENTRY1", "CarOut"), ("ENTRY2", "CarIn"), ("ENTRY2", "CarOut"), ("ENTRY3", "CarIn")):
            await main._handle_car_spot_action({"EventClass": "car_spot_action", "CarPlateNumber": "ZON 011",
                                                "SpotName": sensor, "SpotType": "EntrySpot", "Direction": direction})
        await asyncio.sleep(0.2)
        assert hop.done(), "the ENTRY1 leg must end once the car waits at ENTRY3"

    asyncio.run(scenario())
    assert lvl2.index(("goto", "ZON 011", "ENTRY3")) < lvl2.index(("open", "gate5")) < lvl2.index(("goto", "ZON 011", "P69"))
    assert lvl2.count(("goto", "ZON 011", "ENTRY3")) == 1, lvl2
    gotos = [c for c in lvl2 if c[0] == "goto"]
    assert lvl2.gate_states[gotos.index(("goto", "ZON 011", "P69"))]["gate5"] == "Open"
    assert state.sessions["ZON 011"].staged_via == "ENTRY3"


def test_hop_refused_by_the_simulator_falls_back_to_opening_the_zone_gate(lvl2, monkeypatch):
    monkeypatch.setattr(main, "HOP_ATTEMPTS", 2)
    _fill("ZONE3", ["P69"])
    _assigned("ZON 012", "P69")
    asyncio.run(main._resend_if_still_at_entry("ZON 012", "P69", initial=True))   # car never leaves ENTRY1
    assert lvl2[:2] == [("goto", "ZON 012", "ENTRY3")] * 2
    assert ("open", "gate5") in lvl2 and ("goto", "ZON 012", "P69") in lvl2


def test_paid_car_is_only_sent_out_once_its_exit_gate_reports_open(lvl2):
    session = VehicleSession(plate="ZON 013", entry_gate="ENTRY1", phase=SessionPhase.CHARGED, exit_gate="Exit100")
    session.paid = True
    state.sessions["ZON 013"] = session

    asyncio.run(main._release_paid(session))
    assert lvl2 == [("open", "gate6"), ("goto", "ZON 013", "leavepark")]
    assert lvl2.gate_states[0]["gate6"] == "Open"
    assert session.released
