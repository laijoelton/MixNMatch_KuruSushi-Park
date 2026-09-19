"""A car sent through a barrier that is still opening never moves: the
simulator drops the goto silently. Dispatch must wait for the barrier, and a
car that never leaves the entry must be sent again."""
import asyncio
import dataclasses

import pytest

from app import main
from app.state import Barrier, BarrierPosition, Spot, VehicleSession, SessionPhase, state


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, autopilot=True, entry_max_attempts=3, game_speed=8))
    monkeypatch.setattr(main, "GATE_OPEN_WAIT_S", 1.0)
    monkeypatch.setattr(main, "ENTRY_RETRY_S", 0.2)
    state.barriers.clear()
    state.sessions.clear()
    state.spots.clear()
    state.pending_repairs.clear()
    main._left_entry.clear()


def test_waits_until_barrier_reports_open(fast):
    state.barriers["gateA"] = Barrier(name="gateA", state=BarrierPosition.OPENING)

    async def scenario():
        async def simulator_reports_open():
            await asyncio.sleep(0.02)
            state.update_barrier_state("gateA", "Open")
        opener = asyncio.create_task(simulator_reports_open())
        opened = await main._wait_for_barrier_open("gateA")
        await opener
        return opened

    assert asyncio.run(scenario()) is True


def test_wait_gives_up_instead_of_hanging(fast):
    state.barriers["gateA"] = Barrier(name="gateA", state=BarrierPosition.CLOSED)
    assert asyncio.run(main._wait_for_barrier_open("gateA")) is False


def test_open_or_unknown_or_broken_barrier_does_not_wait(fast):
    state.barriers["gateA"] = Barrier(name="gateA", state=BarrierPosition.OPEN)
    assert asyncio.run(main._wait_for_barrier_open("gateA")) is True
    assert asyncio.run(main._wait_for_barrier_open("nope")) is True
    state.barriers["gateB"] = Barrier(name="gateB", state=BarrierPosition.CLOSED, broken=True)
    assert asyncio.run(main._wait_for_barrier_open("gateB")) is True


def _stuck_session(plate, spot):
    state.spots[spot] = Spot(spot)
    assert state.reserve_spot(spot, plate)
    session = VehicleSession(plate=plate, entry_gate="ENTRY1")
    session.phase = SessionPhase.ASSIGNED
    session.assigned_spot = spot
    state.sessions[plate] = session


def test_car_that_never_leaves_entry_is_sent_again(fast, monkeypatch):
    sent = []

    async def goto(plate, destination):
        sent.append((plate, destination))

    monkeypatch.setattr(main.client, "car_goto", goto)
    _stuck_session("NQP 718", "S9")
    asyncio.run(main._resend_if_still_at_entry("NQP 718", "S9"))
    assert sent == [("NQP 718", "S9")] * 3


def test_car_that_left_entry_is_not_resent(fast, monkeypatch):
    sent = []

    async def goto(plate, destination):
        sent.append((plate, destination))

    monkeypatch.setattr(main.client, "car_goto", goto)
    _stuck_session("WXC 865", "S8")
    main._left_entry.add("WXC 865")
    asyncio.run(main._resend_if_still_at_entry("WXC 865", "S8"))
    assert sent == []
