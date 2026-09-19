import asyncio
import dataclasses
from unittest.mock import AsyncMock

import pytest

from app import db, main
from app.state import Barrier, BarrierPosition, ParkingState, Spot, SpotStatus


@pytest.fixture
def recovery(monkeypatch):
    state = ParkingState()
    monkeypatch.setattr(main, "state", state)
    monkeypatch.setattr(main, "settings", dataclasses.replace(
        main.settings, autopilot=True, game_speed=8, exit_charge_delay_s=0))
    for command in ("car_charge", "car_goto", "barrier_open", "barrier_close"):
        monkeypatch.setattr(main.client, command, AsyncMock())
    monkeypatch.setattr(main, "_barrier_for_sensor", lambda sensor: "RECOVERY_GATE")
    with db._lock, db._conn:
        db._conn.execute("DELETE FROM active_sessions")
    yield state
    with db._lock, db._conn:
        db._conn.execute("DELETE FROM active_sessions")


@pytest.mark.parametrize("broken,maintenance", [(False, False), (True, False), (False, True)])
def test_live_occupied_bay_survives_restoration_and_reservation_expiry(recovery, broken, maintenance):
    state = recovery
    state.spots["R1"] = Spot("R1")
    session = state.start_session("MISSED_PARK", "ENTRY1")
    state.reserve_spot("R1", session.plate)
    state.assign_spot(session.plate, "R1")
    session.created_at -= 1000
    db.save_active_session(session)
    # A live count identifies occupancy but cannot prove which vehicle parked.
    state.load_spots([{"name": "R1", "detectedCars": 1, "broken": broken,
                       "isUnderMaintenance": maintenance}])
    main.restore_sessions()
    assert session.parked_at is None
    assert state.spots["R1"].status == SpotStatus.OCCUPIED
    asyncio.run(main.reap_orphans())
    assert state.spots["R1"].status == SpotStatus.OCCUPIED
    assert not state.spots["R1"].dispatchable
    assert state.spots["R1"].broken == broken
    assert state.spots["R1"].under_maintenance == maintenance


def test_recovery_does_not_resend_assignment_into_live_occupied_bay(recovery):
    state = recovery
    state.spots["R1"] = Spot("R1")
    session = state.start_session("MISSED_SENSORS", "ENTRY1")
    state.assign_spot(session.plate, "R1")
    state.load_spots([{"name": "R1", "detectedCars": 1}])
    main.restore_sessions()
    async def scenario():
        await main._resend_if_still_at_entry(session.plate, "R1", initial=True)
    asyncio.run(scenario())
    main.client.car_goto.assert_not_awaited()


@pytest.mark.parametrize("resume", ["payment", "reconnect"])
def test_paid_release_resumes_without_a_second_charge(recovery, resume):
    state = recovery
    state.barriers["RECOVERY_GATE"] = Barrier("RECOVERY_GATE", state=BarrierPosition.OPEN)
    session = state.start_session("PAID_RECOVERY", "ENTRY1", planned_minutes=2)
    session.exit_gate = "EXIT1"

    async def scenario():
        await main._charge_at_exit(session.plate, session.exit_gate)
        payment = {"CarPlateNumber": session.plate, "Amount": session.expected_amount}
        main.client.car_goto.side_effect = RuntimeError("connection lost before release")
        await main._handle_payment_made(payment)
        assert session.paid and not session.released
        main.client.car_goto.side_effect = None
        if resume == "payment":
            await main._handle_payment_made(payment)
        else:
            state.sessions.clear()
            main.restore_sessions()
            await main._ensure_barriers_open()
        assert state.sessions[session.plate].released
        assert main.client.car_goto.await_count == 2
        main.client.car_charge.assert_awaited_once()
        # Further payment deliveries or reconnects must not release twice.
        await main._handle_payment_made(payment)
        await main._ensure_barriers_open()
        assert main.client.car_goto.await_count == 2

    asyncio.run(scenario())


def test_gate_repair_completion_resumes_paid_exit(recovery):
    state = recovery
    state.barriers["RECOVERY_GATE"] = Barrier("RECOVERY_GATE", state=BarrierPosition.CLOSED,
                                               under_maintenance=True)
    session = state.start_session("PAID_AFTER_REPAIR", "ENTRY1", planned_minutes=2)
    session.exit_gate = "EXIT1"
    session.paid = True
    async def scenario():
        await main._handle_component_fixed({"Type": "BarrierGate", "Name": "RECOVERY_GATE"})
        assert session.released
        main.client.car_goto.assert_awaited_once_with(session.plate, "leavepark")
    asyncio.run(scenario())


def test_gate_repair_completion_resumes_assigned_entry(recovery, monkeypatch):
    state = recovery
    state.spots["R1"] = Spot("R1")
    state.barriers["RECOVERY_GATE"] = Barrier("RECOVERY_GATE", under_maintenance=True)
    session = state.start_session("ENTRY_AFTER_REPAIR", "ENTRY1")
    assert state.reserve_spot("R1", session.plate)
    state.assign_spot(session.plate, "R1")
    resumed = []
    monkeypatch.setattr(main, "_start_dispatch_retry", lambda plate, spot: resumed.append((plate, spot)))
    asyncio.run(main._handle_component_fixed({"Type": "BarrierGate", "Name": "RECOVERY_GATE"}))
    assert resumed == [(session.plate, "R1")]


def test_confirmed_exit_is_not_reaped_while_waiting_for_payment(recovery):
    state = recovery
    state.spots["R1"] = Spot("R1")
    state.barriers["RECOVERY_GATE"] = Barrier("RECOVERY_GATE", state=BarrierPosition.OPEN)
    session = state.start_session("EXIT_WAIT", "ENTRY1", planned_minutes=2)
    state.mark_parked(session.plate, "R1")
    state.mark_left_spot(session.plate)
    session.left_spot_at -= 1000
    session.exit_confirmed = True
    session.exit_gate = "EXIT1"

    async def scenario():
        await main._charge_at_exit(session.plate, session.exit_gate)
        await main.reap_orphans()
        assert state.get_session(session.plate) is session
        await main._handle_payment_made({"CarPlateNumber": session.plate,
                                        "Amount": session.expected_amount})
        assert session.paid and session.released
        main.client.car_charge.assert_awaited_once()

    asyncio.run(scenario())


def test_concurrent_payment_and_recovery_only_release_once(recovery):
    state = recovery
    state.barriers["RECOVERY_GATE"] = Barrier("RECOVERY_GATE", state=BarrierPosition.OPEN)
    session = state.start_session("CONCURRENT", "ENTRY1", planned_minutes=2)
    session.exit_gate = "EXIT1"

    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        async def delayed_release(*args):
            entered.set()
            await finish.wait()
        main.client.car_goto.side_effect = delayed_release
        await main._charge_at_exit(session.plate, session.exit_gate)
        payment = {"CarPlateNumber": session.plate, "Amount": session.expected_amount}
        initial = asyncio.create_task(main._handle_payment_made(payment))
        await entered.wait()
        await main._ensure_barriers_open()
        await main._handle_payment_made(payment)
        finish.set()
        await initial
        main.client.car_goto.assert_awaited_once()

    asyncio.run(scenario())
