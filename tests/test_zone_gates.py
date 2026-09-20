"""Zone balancing and per-car gate control.

Level 2 has one road in (the operator-only main gate, gate7) and a gate per
zone on each side: entry gates 1/3/5 and exit gates 2/4/6. A car goes to the
zone with the lowest (occupied + reserved + broken + under repair) / bays
ratio; that zone's entry gate opens for it and closes once it has parked.
"""
import asyncio
import dataclasses
import time

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


def test_stage1_cascades_by_priority_under_the_ceiling():
    ratios = {"ZONE1": 0.35, "ZONE2": 0.10, "ZONE3": 0.05}
    # ZONE3 has the lowest ratio, but stage 1 ignores that below the ceiling
    # and takes the highest-priority (lowest-numbered) zone instead.
    assert zones.pick_zone_staged({"ZONE1", "ZONE2", "ZONE3"}, ratios) == ("ZONE1", "stage1")


def test_stage1_covers_the_gap_between_the_two_original_thresholds():
    # No zone is below 30%, but ZONE1 sits between 30% and the 40% ceiling.
    # It is still routed by priority rather than left undefined.
    ratios = {"ZONE1": 0.38, "ZONE2": 0.50}
    assert zones.pick_zone_staged({"ZONE1", "ZONE2"}, ratios) == ("ZONE1", "stage1")


def test_stage2_balances_globally_once_every_zone_is_at_the_ceiling():
    ratios = {"ZONE1": 0.90, "ZONE2": 0.41, "ZONE3": 0.55}
    # Priority order (ZONE1 first) is broken here: ZONE2 has the lowest ratio.
    assert zones.pick_zone_staged({"ZONE1", "ZONE2", "ZONE3"}, ratios) == ("ZONE2", "stage2")


def test_stage3_reports_exhaustion_without_guessing():
    assert zones.pick_zone_staged({"ZONE1", "ZONE2"}, {"ZONE1": 1.0, "ZONE2": 1.0}) == (None, "stage3")
    assert zones.pick_zone_staged(set(), {}) == (None, "stage3")


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
    state.stuck_repairs.clear()
    main._repair_seen_at.clear()
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
    # ZONE1 and ZONE2 are pinned at/above ZONE_BALANCE_CEILING (40%, default)
    # so ZONE3 is the only zone in the stage-1 cascading pool: since 4.41,
    # two zones both under the ceiling are ranked by priority, not by ratio -
    # see test_staged_dispatch_prefers_priority_zone_under_the_ceiling for that.
    _fill("ZONE1", ["S1", "S3", "S4"], occupied=3)       # full
    _fill("ZONE2", ["bay36", "bay37", "bay39"], occupied=2)  # 2/3, at the ceiling
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


def test_staged_dispatch_prefers_priority_zone_under_the_ceiling(lvl2):
    # ZONE3 has the lowest ratio (0%), but stage 1 caps at ZONE_BALANCE_CEILING
    # (40%) and both zones qualify, so priority (ZONE1 first) wins - unlike the
    # old always-lowest-ratio pick_zone, which would have sent this to ZONE3.
    _fill("ZONE1", ["S1", "S3", "S4", "S5", "S6"], occupied=1)   # 20%
    _fill("ZONE3", ["P69", "P70"], occupied=0)                  # 0%

    result = asyncio.run(main.dispatch_entry("ZON 010", "ENTRY1", "Normal"))
    assert result["dispatched"] and result["target"] in {"S1", "S3", "S4", "S5", "S6"}


def test_strict_category_filtering_is_the_default_and_drops_the_dispatch(lvl2):
    _fill("ZONE1", ["S1"], occupied=0)   # only a plain "Any" bay, no accessible bay anywhere

    result = asyncio.run(main.dispatch_entry("ZON 011", "ENTRY1", "Disabled"))
    assert not result["dispatched"]
    assert result["target"] is None
    assert any("Full capacity" in row["message"] and row["level"] == "warn"
               for row in state.activity_log)


def test_strict_category_filtering_can_be_disabled_as_an_escape_hatch(lvl2, monkeypatch):
    monkeypatch.setattr(main, "settings",
                        dataclasses.replace(main.settings, strict_category_filtering=False))
    _fill("ZONE1", ["S1"], occupied=0)   # only a plain "Any" bay, no accessible bay anywhere

    result = asyncio.run(main.dispatch_entry("ZON 012", "ENTRY1", "Disabled"))
    assert result["dispatched"] and result["target"] == "S1"


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


def test_both_of_a_zones_gates_are_repaired_at_the_same_time(lvl2):
    # 4.42: no cap. The entry and exit gate go into repair together rather than
    # the exit waiting for the entry to finish.
    _fill("ZONE3", ["P69", "P70"])
    session = VehicleSession(plate="ZON 021", entry_gate="ENTRY1", phase=SessionPhase.PARKED, assigned_spot="P69")
    session.zone, session.parked_at = "ZONE3", 1.0
    state.sessions["ZON 021"] = session
    state.mark_spot_occupied("P69", "ZON 021")          # a car is still parked in ZONE3

    asyncio.run(main._queue_repair("BarrierGate", "gate5"))
    assert state.zone_maintenance["ZONE3"]
    assert _in_repair() == ["gate5", "gate6"], "both gates at once, without waiting for ZONE3 to empty"
    asyncio.run(main._handle_component_fixed({"Type": "BarrierGate", "Name": "gate6"}))
    assert "ZONE3" in state.zone_maintenance, "the entry gate is still being repaired"
    asyncio.run(main._handle_component_fixed({"Type": "BarrierGate", "Name": "gate5"}))
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
# Uncapped gate repairs (4.42): every gate that is broken or predicted to break
# goes into repair immediately, however many that is.
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


def test_two_broken_gates_are_repaired_simultaneously(lvl2):
    _fill("ZONE1", ["S1"])
    _fill("ZONE2", ["bay36"])
    asyncio.run(main._handle_component_broken({"Type": "BarrierGate", "Name": "gate1"}))
    asyncio.run(main._handle_component_broken({"Type": "BarrierGate", "Name": "gate3"}))
    # Both breakdowns, plus each zone's other gate, all go at once.
    assert _in_repair() == ["gate1", "gate2", "gate3", "gate4"], "nothing queues behind anything"
    assert sorted(state.zone_maintenance) == ["ZONE1", "ZONE2"], "both zones close together"


def test_a_breakdown_is_repaired_while_another_gate_is_already_in_repair(lvl2):
    _fill("ZONE1", ["S1"])
    _fill("ZONE2", ["bay36"])
    state.barriers["gate5"].under_maintenance = True          # already being repaired
    asyncio.run(main._handle_component_broken({"Type": "BarrierGate", "Name": "gate3"}))
    assert "gate3" in _in_repair(), "a broken gate never waits for another gate's repair"


def test_every_due_zone_starts_maintenance_together(lvl2):
    # 4.42 replaces 4.32's one-zone-at-a-time rotation: every zone whose gates
    # are predicted to fail is maintained at the same time, even if that is all
    # of them and the park stops taking cars for the length of a repair.
    for zone, bays in (("ZONE1", ["S1"]), ("ZONE2", ["bay36"]), ("ZONE3", ["P69"])):
        _fill(zone, bays)
    for gate in ("gate1", "gate3", "gate5"):
        state.barriers[gate].opens_since_repair = main.settings.gate_expected_break_opens
    main._maintenance_rotation.clear()
    asyncio.run(main._schedule_gate_repairs())
    assert sorted(state.zone_maintenance) == ["ZONE1", "ZONE2", "ZONE3"]
    assert _in_repair() == ["gate1", "gate2", "gate3", "gate4", "gate5", "gate6"]
    result = asyncio.run(main.dispatch_entry("ZON 030", "ENTRY1", "Normal"))
    assert not result["dispatched"] and result["reason"] == "Zone closed for maintenance"


def test_a_zone_whose_gates_are_not_predicted_to_fail_is_left_alone(lvl2):
    _fill("ZONE1", ["S1"])
    _fill("ZONE2", ["bay36"])
    # Only ZONE2's gates have been used enough to be predicted near failure.
    state.barriers["gate3"].opens_since_repair = main.settings.gate_expected_break_opens
    main._maintenance_rotation.clear()
    asyncio.run(main._schedule_gate_repairs())
    assert list(state.zone_maintenance) == ["ZONE2"]


def test_a_gate_repaired_moments_ago_is_never_queued_again(lvl2):
    # The floor that stops a repair loop: GATE_REPAIR_MIN_OPENS opens must have
    # happened since the last repair, whatever the predictor says.
    _fill("ZONE1", ["S1"])
    for gate in state.barriers.values():
        gate.opens_since_repair = 0
    main._maintenance_rotation.clear()
    asyncio.run(main._schedule_gate_repairs())
    assert not _in_repair() and not state.zone_maintenance


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
    state.pending_repairs["gate3"] = "BarrierGate"          # queued by the scheduler, not started
    result = asyncio.run(main.manual_barrier_close("gate3", ADMIN))
    assert result["operator_override"] and ("close", "gate3") in lvl2
    assert "gate3" not in state.pending_repairs, "staff command cancels the queued repair"


def test_a_gate_being_repaired_still_refuses_commands(lvl2):
    state.barriers["gate3"].under_maintenance = True
    with pytest.raises(main.HTTPException) as refused:
        asyncio.run(main.manual_barrier_close("gate3", ADMIN))
    assert refused.value.status_code == 409


def test_the_scheduler_skips_a_zone_with_a_staff_held_gate(lvl2):
    _fill("ZONE1", ["S1"])
    _fill("ZONE2", ["bay36"])
    state.barriers["gate1"].opens_since_repair = main.settings.gate_expected_break_opens
    state.barriers["gate3"].opens_since_repair = main.settings.gate_expected_break_opens
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


# --------------------------------------------------------------------------- #
# 4.42 (replacing 4.34): the zone is shut exactly while its entry gate cannot
# take a car, and reopens the moment that gate is fixed - the exit gate may
# still be in repair.
# --------------------------------------------------------------------------- #
def test_zone_reopens_when_its_entry_gate_is_fixed_even_if_the_exit_is_still_in_repair(lvl2):
    _fill("ZONE3", ["P69", "P70"])
    _fill("ZONE1", ["S1"])
    asyncio.run(main._queue_repair("BarrierGate", "gate5"))
    assert _in_repair() == ["gate5", "gate6"]
    assert asyncio.run(main.dispatch_entry("ZON 040", "ENTRY1", "Normal"))["target"] == "S1", "ZONE3 shut"

    lvl2.clear()
    asyncio.run(main._handle_component_fixed({"Type": "BarrierGate", "Name": "gate5"}))
    assert ("close", "gate5") in lvl2, "the repaired entry gate is closed outright, whatever we last saw"
    assert _in_repair() == ["gate6"], "the exit gate\'s repair carries on"
    result = asyncio.run(main.dispatch_entry("ZON 041", "ENTRY1", "Normal"))
    assert result["target"] in {"P69", "P70"}, "ZONE3 takes cars again while gate6 is repaired"


def test_the_zone_stays_shut_while_only_its_exit_gate_is_fixed_first(lvl2):
    _fill("ZONE3", ["P69", "P70"])
    _fill("ZONE1", ["S1"])
    asyncio.run(main._queue_repair("BarrierGate", "gate5"))
    asyncio.run(main._handle_component_fixed({"Type": "BarrierGate", "Name": "gate6"}))
    assert asyncio.run(main.dispatch_entry("ZON 042", "ENTRY1", "Normal"))["target"] == "S1", (
        "no car may be sent to a zone whose entry gate is still in repair")


# --------------------------------------------------------------------------- #
# 4.35: a repair that never finishes must still be surfaced. Since 4.42 it no
# longer blocks other repairs, but its zone would stay shut for the rest of the
# run if nothing gave up on it.
# --------------------------------------------------------------------------- #
def test_a_stuck_repair_is_flagged_for_staff(lvl2, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, gate_repair_stuck_s=400.0))
    _fill("ZONE1", ["S1"])
    _fill("ZONE2", ["bay36"])
    state.barriers["gate1"].under_maintenance = True          # e.g. restored half-repaired
    main._repair_seen_at["gate1"] = time.monotonic() - 60      # 60 s wall = 480 s simulated at speed 8
    asyncio.run(main._handle_component_broken({"Type": "BarrierGate", "Name": "gate3"}))
    assert "gate1" in state.stuck_repairs
    assert "gate3" in state.pending_repairs
    assert any(b["name"] == "gate1" and b["repair_stuck"] for b in state.snapshot()["barriers"])


def test_a_repair_still_within_its_time_is_not_flagged_stuck(lvl2, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, gate_repair_stuck_s=400.0))
    _fill("ZONE2", ["bay36"])
    state.barriers["gate1"].under_maintenance = True
    main._repair_seen_at["gate1"] = time.monotonic() - 5        # 40 s simulated: a normal repair
    asyncio.run(main._handle_component_broken({"Type": "BarrierGate", "Name": "gate3"}))
    assert "gate3" in state.pending_repairs, "a breakdown is repaired regardless"
    assert not state.stuck_repairs


def test_a_stuck_gate_ends_its_zone_maintenance_and_clears_when_fixed(lvl2, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, gate_repair_stuck_s=400.0))
    _fill("ZONE1", ["S1"])
    state.zone_maintenance["ZONE1"] = {"entry": "gate1", "exit": "gate2", "trigger": "t", "todo": {"gate1"}}
    state.barriers["gate1"].under_maintenance = True
    main._repair_seen_at["gate1"] = time.monotonic() - 60
    asyncio.run(main._schedule_gate_repairs())
    assert "ZONE1" not in state.zone_maintenance, "the zone must be able to reopen"
    asyncio.run(main._handle_component_fixed({"Type": "BarrierGate", "Name": "gate1"}))
    assert "gate1" not in state.stuck_repairs
