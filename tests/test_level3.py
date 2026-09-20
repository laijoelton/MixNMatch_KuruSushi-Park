"""Level 3: double parking, payment re-requests, exit failover, security log.

Each test states the situation the airport-scale brief describes and checks the
one behaviour that situation demands, not the plumbing around it.
"""
import asyncio
import dataclasses
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import db, main
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
