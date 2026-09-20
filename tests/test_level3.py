"""Level 3: double parking, payment re-requests, exit failover, security log.

Each test states the situation the airport-scale brief describes and checks the
one behaviour that situation demands, not the plumbing around it.
"""
import asyncio
import dataclasses
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import db, layout, main, reachability
from app.state import Barrier, SpotStatus, Spot, state


@pytest.fixture
def site(monkeypatch):
    """An empty two-bay site with autopilot on and the simulator mocked out."""
    for mapping in (state.spots, state.barriers, state.sessions, state.active_dispatches):
        mapping.clear()
    state.pending_repairs.clear()
    state.crowded_spots = set()
    with db._lock, db._conn:
        db._conn.execute("DELETE FROM incidents")
        db._conn.execute("DELETE FROM security_events")
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, autopilot=True,
                                                              payment_retry_delay_s=0))
    for command in ("car_goto", "car_charge", "barrier_open", "barrier_close"):
        monkeypatch.setattr(main.client, command, AsyncMock())
    yield


def _park(plate, spot, zone="ZONE1"):
    state.spots.setdefault(spot, Spot(spot, zone_parent=zone))
    asyncio.run(main._handle_car_spot_action(
        {"CarPlateNumber": plate, "SpotName": spot, "SpotType": "Park", "Direction": "CarIn"}))


# --------------------------------------------------------------- double park
def test_second_bay_is_kept_and_raised_as_an_incident(site):
    state.start_session("WCT 759", gate="ENTRY1", car_type="Normal")
    _park("WCT 759", "S1")
    _park("WCT 759", "S2")

    assert state.spots["S1"].status == SpotStatus.OCCUPIED, "the first bay still has a car in it"
    assert state.spots["S2"].status == SpotStatus.OCCUPIED
    assert [row["spots"] for row in state.double_parked()] == [["S1", "S2"]]
    assert db.open_incident_id("double_park", "WCT 759") is not None


def test_a_reservation_the_car_never_used_is_still_freed(site):
    state.start_session("ABC 123", gate="ENTRY1", car_type="Normal")
    state.spots["S1"] = Spot("S1")
    state.spots["S2"] = Spot("S2")
    state.reserve_spot("S1", "ABC 123")
    state.assign_spot("ABC 123", "S1")

    _park("ABC 123", "S2")

    assert state.spots["S1"].status == SpotStatus.AVAILABLE, "an unused reservation must not leak"
    assert state.double_parked() == []
    assert db.open_incident_id("double_park", "ABC 123") is None


def test_leaving_one_bay_closes_the_incident(site):
    state.start_session("XYZ 999", gate="ENTRY1", car_type="Normal")
    _park("XYZ 999", "S1")
    _park("XYZ 999", "S2")
    assert db.open_incident_id("double_park", "XYZ 999") is not None

    asyncio.run(main._handle_car_spot_action(
        {"CarPlateNumber": "XYZ 999", "SpotName": "S1", "SpotType": "Park", "Direction": "CarOut"}))

    assert db.open_incident_id("double_park", "XYZ 999") is None


# ------------------------------------------------------------ payment retry
def test_a_wrong_amount_is_invoiced_again_when_enabled(site, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, payment_retry_max=2))
    session = state.start_session("PAY 001", gate="ENTRY1", car_type="Normal")
    session.expected_amount, session.expected_parking, session.expected_charging = 8.0, 8.0, 0.0
    session.charge_attempted = True

    asyncio.run(main._ask_to_pay_again(session, paid_amount=1.0))

    main.client.car_charge.assert_awaited_once_with("PAY 001", 8.0, 0.0)
    assert session.payment_retries == 1
    assert db.query("SELECT detail FROM incidents WHERE kind = 'payment_retry'")[0]["detail"].startswith(
        "Asked PAY 001 to pay again")


def test_by_default_a_wrong_amount_is_not_re_invoiced_automatically(site):
    """Measured on the live binary: re-invoicing costs RM50 ("Car has already
    paid for parking."), so the car is held and a human decides."""
    assert main.settings.payment_retry_max == 0
    session = state.start_session("PAY 002", gate="ENTRY1", car_type="Normal")
    session.expected_amount, session.expected_parking, session.expected_charging = 5.0, 5.0, 0.0

    asyncio.run(main._ask_to_pay_again(session, paid_amount=0.5))

    main.client.car_charge.assert_not_awaited()
    assert db.open_incident_id("payment_unresolved", "PAY 002") is not None


def test_staff_can_ask_for_payment_again(site):
    session = state.start_session("PAY 003", gate="ENTRY1", car_type="Normal")
    session.expected_amount, session.expected_parking, session.expected_charging = 6.0, 6.0, 0.0

    result = asyncio.run(main.request_payment_again("PAY 003", {"username": "admin", "role": "admin"}))

    assert result["expected"] == 6.0 and result["attempt"] == 1
    main.client.car_charge.assert_awaited_once_with("PAY 003", 6.0, 0.0)
    assert db.query("SELECT detail FROM incidents WHERE kind = 'payment_retry'")[0]["detail"].startswith(
        "Staff asked PAY 003")


# ------------------------------------------------------------- exit failover
def test_a_paid_car_is_re_routed_around_a_broken_exit(site):
    state.barriers["gateBroken"] = Barrier("gateBroken", broken=True)
    state.barriers["gateOk"] = Barrier("gateOk")
    state.spots["EXIT1"] = Spot("EXIT1", purpose="ExitSpot")
    state.spots["ESCAPE1"] = Spot("ESCAPE1", purpose="LeaveParking")
    session = state.start_session("OUT 001", gate="ENTRY1", car_type="Normal")
    session.exit_gate = "EXIT1"
    session.paid = True

    asyncio.run(main._reroute_to_other_exit(session, "gateBroken", "gate broken"))

    main.client.car_goto.assert_awaited_once_with("OUT 001", "ESCAPE1")
    assert session.released is True
    assert db.query("SELECT detail FROM incidents WHERE kind = 'exit_failover'")[0]["detail"] == (
        "OUT 001 re-routed from gateBroken to ESCAPE1 (gate broken)")


def test_no_usable_exit_means_the_car_waits(site, monkeypatch):
    state.barriers["gateBroken"] = Barrier("gateBroken", broken=True)
    state.spots["ESCAPE1"] = Spot("ESCAPE1", purpose="LeaveParking")
    session = state.start_session("OUT 002", gate="ENTRY1", car_type="Normal")
    # The only way out is behind the gate that failed.
    monkeypatch.setattr(main, "_barrier_for_sensor", lambda _sensor: "gateBroken")

    assert main._alternative_exit("gateBroken") is None
    assert asyncio.run(main._reroute_to_other_exit(session, "gateBroken", "gate broken")) is False
    main.client.car_goto.assert_not_awaited()


# ----------------------------------------------------------- security events
def test_duplicate_deliveries_are_counted_not_just_refused():
    db.record_security_event("duplicate_event", event_id="E1", event_class="car_spot_action",
                             detail="EventId already accepted")
    db.record_security_event("duplicate_event", event_id="E1", event_class="car_spot_action",
                             detail="EventId already accepted")
    row = db.query("SELECT occurrences FROM security_events WHERE kind='duplicate_event' AND event_id='E1'")[0]
    assert row["occurrences"] == 2, "one row per EventId, with a count - not one row per delivery"


def test_security_page_is_audit_only():
    client = TestClient(main.app, follow_redirects=False)
    assert client.post("/api/auth/login", json={"username": "operator", "password": "operator123"}).status_code == 200
    assert client.get("/api/security").status_code == 403

    admin = TestClient(main.app, follow_redirects=False)
    assert admin.post("/api/auth/login", json={"username": "admin", "password": "admin123"}).status_code == 200
    body = admin.get("/api/security").json()
    assert {"items", "totals", "sequence_gaps"} <= set(body)


# --------------------------------------------------------- component summary
def test_component_summary_counts_each_family(site):
    state.spots["S1"] = Spot("S1")
    state.spots["S2"] = Spot("S2", broken=True)
    state.barriers["G1"] = Barrier("G1")
    state.barriers["G2"] = Barrier("G2", under_maintenance=True)

    summary = {row["family"]: row for row in state.component_summary()}

    assert (summary["Bays"]["total"], summary["Bays"]["available"], summary["Bays"]["broken"]) == (2, 1, 1)
    assert (summary["Gates"]["available"], summary["Gates"]["under_maintenance"]) == (1, 1)


def test_wear_left_the_live_frame(site):
    assert "wear" not in state.snapshot(), "wear is fetched from /api/wear, not pushed every second"


# ------------------------------------------------------- reachability (4.39)
def test_level3_road_network_is_two_disconnected_halves():
    """The fault behind "cars stop getting in": ENTRY1-3 and the outdoor
    entrances serve different halves of the site, and nothing in the REST API
    says so."""
    table = reachability.table_for("lvl3")
    indoor = set(table["ENTRY1"])
    outdoor = set(table["OENTRY1"])
    assert len(indoor) == 90 and len(outdoor) == 160
    assert not indoor & outdoor, "an indoor car can never reach an outdoor bay"
    assert set(table["ENTRY2"]) == indoor
    assert all(set(table[name]) == outdoor for name in ("OENTRY1", "OENTRY2", "OENTRY3", "OENTRY4"))
    assert "Entry104" not in table, "ZONE4's right edge is exit-only"


def test_open_all_commands_all_twenty_level3_gates(site):
    for gate in layout.load_geometry("lvl3")["gates"]:
        state.barriers[gate["name"]] = Barrier(gate["name"], zone_parent=gate["zone"])

    result = asyncio.run(main.open_all_gates({"username": "admin", "role": "admin"}))

    assert len(result["opened"]) == 20
    assert set(result["opened"]) == {f"gate{i}" for i in range(1, 21)}
    assert main.client.barrier_open.await_count == 20


def test_level1_and_2_are_one_network():
    assert len(reachability.table_for("lvl1")["ENTRY1"]) == 30
    assert {len(v) for v in reachability.table_for("lvl2").values()} == {90}


def test_dispatch_never_sends_a_car_to_an_unreachable_bay(site, monkeypatch):
    monkeypatch.setattr(main, "running_level", lambda: "lvl3")
    monkeypatch.setattr(main, "_live_bays_synced", True)
    table = reachability.table_for("lvl3")
    indoor, outdoor = set(table["ENTRY1"]), set(table["OENTRY1"])
    # One free bay on each side; the outdoor half is emptier, so the old
    # zone-ratio rule would have picked the outdoor one every time.
    inside, outside = sorted(indoor)[0], sorted(outdoor)[0]
    state.spots[inside] = Spot(inside, zone_parent="ZONE1")
    state.spots[outside] = Spot(outside, zone_parent="ZONE5")

    result = asyncio.run(main.dispatch_entry("RCH 001", "ENTRY1", "Normal"))

    assert result["target"] == inside


def test_an_ev_is_given_a_charging_bay(site):
    state.spots["ANY1"] = Spot("ANY1", parking_for_car_type="Any")
    state.spots["EV1"] = Spot("EV1", parking_for_car_type="Electric")

    result = asyncio.run(main.dispatch_entry("EVC 001", "ENTRY1", "Electric"))

    assert result["target"] == "EV1"


# ------------------------------------------------- occupied bay and reaping
def test_an_occupied_bay_penalty_re_dispatches_instead_of_retrying(site):
    state.spots["S1"] = Spot("S1", zone_parent="ZONE1")
    state.spots["S2"] = Spot("S2", zone_parent="ZONE1")
    session = state.start_session("COL 001", gate="ENTRY1", car_type="Normal")
    state.reserve_spot("S1", "COL 001")
    state.assign_spot("COL 001", "S1")

    asyncio.run(main._handle_penalty({
        "Reason": "Car:(COL 001) attempted to park in an occupied spot:(S1).",
        "FineAmount": 50.0, "Type": "Car", "ComponentName": "COL001"}))

    assert state.spots["S1"].status == SpotStatus.OCCUPIED, "believe the simulator, not our view"
    assert session.assigned_spot == "S2", "the car gets a different bay instead of retrying"


def test_a_car_still_driving_keeps_its_bay_longer(site, monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(
        main.settings, reservation_ttl_s=100, game_speed=1, en_route_grace_factor=3))
    waiting = state.start_session("WAI 001", gate="ENTRY1", car_type="Normal")
    driving = state.start_session("DRV 001", gate="ENTRY1", car_type="Normal")
    driving.entry_departed = True

    assert main._arrival_grace_s(waiting) == 100
    assert main._arrival_grace_s(driving) == 300


def test_a_special_car_is_not_turned_away_when_its_preferred_bay_is_unreachable(site, monkeypatch):
    """Reachability must be applied before the car-type preference. Narrowing to
    charging bays on the far side of the site leaves nothing to dispatch, and
    the car is refused from a half-empty car park."""
    monkeypatch.setattr(main, "running_level", lambda: "lvl3")
    monkeypatch.setattr(main, "_live_bays_synced", True)
    table = reachability.table_for("lvl3")
    here = sorted(table["ENTRY1"])[0]          # reachable, ordinary bay
    far = sorted(table["OENTRY1"])[0]          # unreachable from ENTRY1
    state.spots[here] = Spot(here, zone_parent="ZONE1", parking_for_car_type="Any")
    state.spots[far] = Spot(far, zone_parent="ZONE5", parking_for_car_type="Electric")

    result = asyncio.run(main.dispatch_entry("EVR 001", "ENTRY1", "Electric"))

    assert result["target"] == here, "an ordinary bay it can reach beats a charging bay it cannot"


def test_a_refused_invoice_is_re_sent_with_the_simulator_s_figure(site):
    """The simulator names the correct amount in the penalty. Not using it left
    the car with an invoice it would not pay, and it drove off unpaid."""
    session = state.start_session("COR 001", gate="ENTRY1", car_type="Electric")
    session.expected_amount, session.expected_parking, session.expected_charging = 6.6, 3.3, 3.3
    session.charged = True

    asyncio.run(main._handle_penalty({
        "Reason": "Car is being charged wrongly with amount: (6.60). Car type is (Electric) "
                  "so charge should be: (2.00)",
        "FineAmount": 10.0, "Type": "Car", "ComponentName": "COR001"}))

    main.client.car_charge.assert_awaited_once_with("COR 001", 2.0, 0.0)
    assert session.expected_amount == 2.0, "the payment check must expect the corrected figure"

    # Once only: a second correction must not start a charging loop.
    main.client.car_charge.reset_mock()
    asyncio.run(main._apply_charge_correction({"ComponentName": "COR001"}, 2.0, 3.0))
    main.client.car_charge.assert_not_awaited()


def test_a_car_that_already_paid_is_never_re_invoiced_by_a_correction(site):
    session = state.start_session("COR 002", gate="ENTRY1", car_type="Normal")
    session.expected_amount, session.paid = 3.0, True

    asyncio.run(main._apply_charge_correction({"ComponentName": "COR002"}, 3.0, 0.0))

    main.client.car_charge.assert_not_awaited()


def test_a_car_that_left_the_site_frees_every_bay_it_held(site):
    """A double-parked car sends one CarOut, not two. Without this the second
    bay is held for the rest of the run and the zone reads FULL while empty."""
    state.spots["S1"] = Spot("S1", zone_parent="ZONE1")
    state.spots["S2"] = Spot("S2", zone_parent="ZONE1")
    state.start_session("GON 001", gate="ENTRY1", car_type="Normal")
    state.mark_spot_occupied("S1", "GON 001")
    state.mark_spot_occupied("S2", "GON 001")

    state.complete_session("GON 001", release_bays=True)

    assert state.spots["S1"].status == SpotStatus.AVAILABLE
    assert state.spots["S2"].status == SpotStatus.AVAILABLE


def test_a_returning_plate_does_not_free_the_bay_of_the_car_still_in_it(site):
    """Plates are recycled (4.19): the car in that bay may be a different one."""
    state.spots["S1"] = Spot("S1", zone_parent="ZONE1")
    state.start_session("OLD 001", gate="ENTRY1", car_type="Normal")
    state.mark_spot_occupied("S1", "OLD 001")

    state.complete_session("OLD 001")

    assert state.spots["S1"].status == SpotStatus.OCCUPIED
