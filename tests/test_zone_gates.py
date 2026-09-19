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
    state.zone_maintenance.clear()
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
    # ZONE3 is entered through gate5: it opens, then the car is sent. The
    # two-hop via ENTRY3 is off by default (4.27) - the simulator treats a
    # goto to ENTRY3 as parking there.
    assert lvl2[:2] == [("open", "gate5"), ("goto", "ZON 001", result["target"])], lvl2


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


def test_zone3_car_is_sent_to_entry3_and_gate5_opens_only_when_it_arrives(lvl2, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, zone_gate_at_sensor=True))
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
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, zone_gate_at_sensor=True))
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


# --------------------------------------------------------------------------- #
# Zone maintenance (4.26): a zone whose gate needs repair is closed to new
# cars, drained, and reopened only once both its gates are repaired.
# --------------------------------------------------------------------------- #
def test_each_zone_knows_its_entry_and_exit_gate(lvl2):
    assert main._zone_gates("ZONE1") == ("gate1", "gate2")   # gate2 has no zone tag: found via EXIT_EXIT
    assert main._zone_gates("ZONE2") == ("gate3", "gate4")
    assert main._zone_gates("ZONE3") == ("gate5", "gate6")
    assert main._zone_of_gate("gate6") == "ZONE3" and main._zone_of_gate("gate7") is None


def test_a_broken_exit_gate_closes_its_zone_and_is_repaired_at_once(lvl2):
    _fill("ZONE1", ["S1", "S3"], occupied=1)
    _fill("ZONE3", ["P69", "P70"])                            # emptiest: would normally win
    asyncio.run(main._handle_component_broken({"Type": "BarrierGate", "Name": "gate6"}))
    assert "ZONE3" in state.zone_maintenance
    assert "gate6" in state.pending_repairs, "a broken gate cannot wait for the zone to drain"
    result = asyncio.run(main.dispatch_entry("ZON 020", "ENTRY1", "Normal"))
    assert result["target"] == "S3", "cars are sent away from the zone under maintenance"


def test_zone_maintenance_repairs_entry_then_exit_straight_away_then_reopens(lvl2):
    # 4.32: the exit gate is no longer held back until the zone empties.
    _fill("ZONE3", ["P69", "P70"])
    session = VehicleSession(plate="ZON 021", entry_gate="ENTRY1", phase=SessionPhase.PARKED, assigned_spot="P69")
    session.zone, session.parked_at = "ZONE3", 1.0
    state.sessions["ZON 021"] = session
    state.mark_spot_occupied("P69", "ZON 021")          # a car is still parked in ZONE3

    asyncio.run(main._queue_repair("BarrierGate", "gate5"))
    assert state.zone_maintenance["ZONE3"]
    assert _in_repair() == ["gate5"], "entry first; one gate at a time"
    asyncio.run(main._handle_component_fixed({"Type": "BarrierGate", "Name": "gate5"}))
    assert _in_repair() == ["gate6"], "exit straight after, without waiting for ZONE3 to empty"
    assert "ZONE3" in state.zone_maintenance, "entry stays closed until the exit is fixed too"
    asyncio.run(main._handle_component_fixed({"Type": "BarrierGate", "Name": "gate6"}))
    assert "ZONE3" not in state.zone_maintenance
    assert asyncio.run(main.dispatch_entry("ZON 022", "ENTRY1", "Normal"))["target"] == "P70"


def test_entry_gate_repair_waits_while_a_car_is_still_driving_in(lvl2):
    _fill("ZONE3", ["P69", "P70"])
    _assigned("ZON 023", "P70")                              # dispatched, not yet parked
    asyncio.run(main._queue_repair("BarrierGate", "gate5"))
    assert "gate5" not in state.pending_repairs
    state.mark_parked("ZON 023", "P70")
    asyncio.run(main._advance_zone_maintenance())
    assert "gate5" in state.pending_repairs


def test_the_main_gate_never_starts_zone_maintenance(lvl2):
    asyncio.run(main._handle_component_broken({"Type": "BarrierGate", "Name": "gate7"}))
    assert not state.zone_maintenance


# --------------------------------------------------------------------------- #
# Balanced gate repairs (4.28): one gate in repair at a time, most-worn first.
# --------------------------------------------------------------------------- #
def _in_repair():
    return sorted(n for n in state.pending_repairs if n in state.barriers)


def test_opens_since_repair_counts_each_open_and_resets_on_repair(lvl2):
    for _ in range(3):
        state.update_barrier_state("gate3", "Opening")
        state.update_barrier_state("gate3", "Open")
        state.update_barrier_state("gate3", "Closing")
        state.update_barrier_state("gate3", "Closed")
    state.update_barrier_state("gate3", "Open")             # a webhook-only open counts too
    assert state.barriers["gate3"].opens_since_repair == 4
    state.set_component_fixed("BarrierGate", "gate3")
    assert state.barriers["gate3"].opens_since_repair == 0


def test_only_one_gate_is_ever_in_repair_even_when_another_breaks(lvl2):
    _fill("ZONE1", ["S1"])
    _fill("ZONE2", ["bay36"])
    asyncio.run(main._handle_component_broken({"Type": "BarrierGate", "Name": "gate1"}))
    asyncio.run(main._handle_component_broken({"Type": "BarrierGate", "Name": "gate3"}))
    assert _in_repair() == ["gate1"], "gate3 waits for the repair slot"
    assert list(state.zone_maintenance) == ["ZONE1"], "a breakdown elsewhere never closes a second zone"
    asyncio.run(main._handle_component_fixed({"Type": "BarrierGate", "Name": "gate1"}))
    assert _in_repair() == ["gate3"], "the broken gate jumps the queue"
    asyncio.run(main._handle_component_fixed({"Type": "BarrierGate", "Name": "gate3"}))
    assert _in_repair() == ["gate2"], "then ZONE1's maintenance carries on with its exit gate"


def test_maintenance_rotates_through_the_zones_one_at_a_time(lvl2):
    for zone, bays in (("ZONE1", ["S1"]), ("ZONE2", ["bay36"]), ("ZONE3", ["P69"])):
        _fill(zone, bays)
    for gate in ("gate1", "gate3", "gate5"):
        state.barriers[gate].opens_since_repair = 3
    main._maintenance_rotation.clear()
    visited = []
    for _ in range(3):
        asyncio.run(main._schedule_gate_repairs())
        (zone,) = state.zone_maintenance
        visited.append(zone)
        entry, exit_gate = main._zone_gates(zone)
        asyncio.run(main._handle_component_fixed({"Type": "BarrierGate", "Name": entry}))
        asyncio.run(main._handle_component_fixed({"Type": "BarrierGate", "Name": exit_gate}))
        assert zone not in state.zone_maintenance
    assert visited == ["ZONE1", "ZONE2", "ZONE3"], "the other two zones keep filling while one is repaired"


def test_a_zone_whose_gates_were_not_used_since_repair_is_skipped(lvl2):
    _fill("ZONE1", ["S1"])
    _fill("ZONE2", ["bay36"])
    state.barriers["gate3"].opens_since_repair = 2          # only ZONE2 has been used
    main._maintenance_rotation.clear()
    asyncio.run(main._schedule_gate_repairs())
    assert list(state.zone_maintenance) == ["ZONE2"]


def test_the_main_gate_is_never_repaired_preventively(lvl2, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, gate_repair_min_opens=5))
    state.barriers["gate7"].opens_since_repair = 50
    asyncio.run(main._schedule_gate_repairs())
    assert not _in_repair()


def test_a_steady_stream_keeps_the_zone_gate_open(lvl2):
    # 4.32: while another car is already heading into the zone, do not close.
    _fill("ZONE3", ["P69", "P70"])
    for plate, bay in (("ZON 030", "P69"), ("ZON 031", "P70")):
        _assigned(plate, bay)

    async def scenario():
        main._start_dispatch_retry("ZON 030", "P69")
        await asyncio.sleep(0.1)
        await main._handle_car_spot_action(_sensor("ZON 030", "ENTRY3", "CarIn"))
        await main._handle_car_spot_action(_sensor("ZON 030", "ENTRY3", "CarOut"))
        await asyncio.sleep(0.3)
        assert ("close", "gate5") not in lvl2, "ZON 031 is on its way to ZONE3"
        await main._handle_car_spot_action(_sensor("ZON 031", "ENTRY3", "CarIn"))
        await main._handle_car_spot_action(_sensor("ZON 031", "ENTRY3", "CarOut"))
        await asyncio.sleep(0.3)

    asyncio.run(scenario())
    assert lvl2.count(("close", "gate5")) == 1, "closed once, after the last car of the stream"


# --------------------------------------------------------------------------- #
# 4.33: staff gate commands beat the automation.
# --------------------------------------------------------------------------- #
ADMIN = {"username": "admin", "role": "admin"}


def test_a_gate_opened_by_staff_stays_open(lvl2):
    asyncio.run(main.manual_barrier_open("gate4", ADMIN))
    state.update_barrier_state("gate4", "Open")
    asyncio.run(main._close_idle_gates())
    assert ("close", "gate4") not in lvl2, "the automation must not shut a staff-opened gate"
    assert state.snapshot()["barriers"][[b["name"] for b in state.snapshot()["barriers"]].index("gate4")]["hold_reason"] == "Held open by operator"


def test_staff_can_close_a_gate_that_is_only_queued_for_repair(lvl2):
    state.barriers["gate3"].state = BarrierPosition.OPEN
    state.pending_repairs["gate3"] = "BarrierGate"          # queued by the rotation, not started
    result = asyncio.run(main.manual_barrier_close("gate3", ADMIN))
    assert result["operator_override"] and ("close", "gate3") in lvl2
    assert "gate3" not in state.pending_repairs, "staff command cancels the queued repair"


def test_a_gate_being_repaired_still_refuses_commands(lvl2):
    state.barriers["gate3"].under_maintenance = True
    with pytest.raises(main.HTTPException) as refused:
        asyncio.run(main.manual_barrier_close("gate3", ADMIN))
    assert refused.value.status_code == 409


def test_the_rotation_skips_a_zone_with_a_staff_held_gate(lvl2):
    _fill("ZONE1", ["S1"])
    _fill("ZONE2", ["bay36"])
    state.barriers["gate1"].opens_since_repair = 3
    state.barriers["gate3"].opens_since_repair = 3
    state.barriers["gate1"].operator_open = True
    main._maintenance_rotation.clear()
    asyncio.run(main._schedule_gate_repairs())
    assert list(state.zone_maintenance) == ["ZONE2"]


def test_automatic_hands_the_gate_back(lvl2):
    asyncio.run(main.manual_barrier_open("gate4", ADMIN))
    state.update_barrier_state("gate4", "Open")
    asyncio.run(main.manual_barrier_auto("gate4", ADMIN))
    assert not state.barriers["gate4"].operator_open and not state.barriers["gate4"].operator_override
    assert ("close", "gate4") in lvl2, "back under automation: an idle gate closes"


def test_gate_auto_route_is_staff_only():
    from app import policy
    assert policy.allowed({"role": "facility_operator"}, "/api/barriers/gate4/auto", "POST")
    assert not policy.allowed({"role": "auditor"}, "/api/barriers/gate4/auto", "POST")
