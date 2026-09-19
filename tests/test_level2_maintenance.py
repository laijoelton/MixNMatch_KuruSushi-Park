"""Level 2 regressions: simulated wear, safe repairs and failure isolation."""
import asyncio
import dataclasses
from unittest.mock import AsyncMock

import pytest

from app import db, main
from app import state as model
from app.queue_worker import PriorityTaskQueue
from app.state import Barrier, ExhaustFan, Light, ParkingState, Spot, state


@pytest.fixture
def maintenance(monkeypatch):
    for mapping in (state.spots, state.barriers, state.fans, state.lights, state.sessions,
                    state.active_dispatches, state.deferred_repairs):
        mapping.clear()
    if hasattr(state, "pending_repairs"):
        state.pending_repairs.clear()
    with db._lock, db._conn:
        db._conn.execute("DELETE FROM meta WHERE key LIKE 'pending_proactive_repair:%'")
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, autopilot=True,
                        wear_cycle_threshold=100, wear_runtime_threshold_s=100))
    for command in ("spot_repair", "fan_repair", "fan_off", "fan_on", "barrier_repair", "barrier_open"):
        monkeypatch.setattr(main.client, command, AsyncMock())
    tasks = []
    async def submit(label, action, priority=50, **kwargs):
        tasks.append((action, kwargs))
    monkeypatch.setattr(main.maintenance_queue, "submit", submit)
    yield tasks
    state.pending_repairs.clear()


def test_runtime_wear_uses_simulated_seconds(monkeypatch):
    monkeypatch.setattr(model, "settings", dataclasses.replace(model.settings, game_speed=8))
    clock = [100.0]
    monkeypatch.setattr(model.time, "monotonic", lambda: clock[0])
    park = ParkingState()
    park.fans["F"] = ExhaustFan("F")
    park.lights["L"] = Light("L", is_on=False, turned_on_at=None)
    park.set_fan_on("F", True)
    park.set_light_on("L", True)
    clock[0] += 10
    assert {r["runtime_seconds"] for r in park.wear_snapshot()} == {80}
    assert park.set_fan_on("F", False)[1] == 80
    assert park.set_light_on("L", False)[1] == 80


def test_gate_command_and_confirmation_count_one_cycle(maintenance):
    state.barriers["G"] = Barrier("G")
    asyncio.run(main.manual_barrier_open("G", {"username": "admin", "role": "admin"}))
    asyncio.run(main._handle_gate_action({"Name": "G", "Action": "Opening"}))
    asyncio.run(main._handle_gate_action({"Name": "G", "Action": "Open"}))
    assert state.barriers["G"].cycle_count == 1
    assert db.query("SELECT cycle_count FROM component_wear WHERE name='G'")[0]["cycle_count"] == 1


def test_queued_repair_reserves_bay_and_deduplicates(maintenance):
    state.spots["S"] = Spot("S")
    async def scenario():
        await main._queue_repair("ParkingSpot", "S")
        await main._queue_repair("ParkingSpot", "S")
        assert "S" not in state.available_spots()
        assert not state.reserve_spot("S", "CAR")
        assert len(maintenance) == 1
        await maintenance[0][0]()
        assert state.spots["S"].under_maintenance
    asyncio.run(scenario())


def test_repair_stops_running_fan_before_repair(maintenance):
    state.fans["F"] = ExhaustFan("F", is_on=True)
    calls = []
    async def off(name):
        calls.append("off")
    async def repair(name):
        calls.append("repair")
        assert not state.fans[name].is_on
    main.client.fan_off.side_effect = off
    main.client.fan_repair.side_effect = repair
    async def scenario():
        await main._queue_repair("ExhaustFan", "F")
        await maintenance[0][0]()
    asyncio.run(scenario())
    assert calls == ["off", "repair"]


def test_dropped_proactive_repair_can_be_scheduled_again(maintenance):
    # 4.28: gates are ranked by opens since repair, not the 85%-of-cycles rule.
    state.barriers["WORN"] = Barrier("WORN", opens_since_repair=main.settings.gate_repair_min_opens)
    async def scenario():
        await main.check_wear()
        callback = maintenance[0][1].get("on_drop")
        assert callback is not None
        await callback()
        await main.check_wear()
        assert len(maintenance) == 2
    asyncio.run(scenario())


def test_broken_component_discovered_at_sync_is_repaired(maintenance):
    state.barriers["BROKEN"] = Barrier("BROKEN", broken=True)
    asyncio.run(main.check_wear())
    assert len(maintenance) == 1


def test_manual_fan_rejects_broken_component(maintenance):
    state.fans["F"] = ExhaustFan("F", broken=True)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.manual_fan_on("F", {"username": "admin", "role": "admin"}))
    assert exc.value.status_code == 409
    main.client.fan_on.assert_not_awaited()


def test_retry_backoff_does_not_block_other_repairs():
    async def scenario():
        queue = PriorityTaskQueue(max_attempts=2, retry_delay_s=60)
        failed = asyncio.Event()
        repaired = asyncio.Event()
        async def failure():
            failed.set()
            raise RuntimeError("unavailable")
        async def success():
            repaired.set()
        queue.start()
        try:
            await queue.submit("failed", failure)
            await asyncio.wait_for(failed.wait(), 1)
            await queue.submit("healthy", success)
            await asyncio.wait_for(repaired.wait(), 0.3)
        finally:
            await queue.stop()
    asyncio.run(scenario())


def test_light_fault_is_visible_and_not_operated(maintenance):
    from app.queue_worker import schedule_lights
    state.lights["L"] = Light("L", is_on=True)
    act = AsyncMock()
    async def scenario():
        await main._handle_component_broken({"Type": "Light", "Name": "L"})
        assert any(row["name"] == "L" and row["broken"] for row in state.broken_components())
        assert next(row for row in state.wear_snapshot() if row["name"] == "L")["broken"]
        await schedule_lights("2026-09-19 12:00:00", state, main.client, act)
        act.assert_not_awaited()
        await main._handle_component_fixed({"Type": "Light", "Name": "L"})
        assert not state.broken_components()
    asyncio.run(scenario())


def test_queued_gate_cannot_be_opened_during_repair(maintenance):
    # 4.33: a merely *queued* repair gives way to staff; a gate actually under
    # repair still refuses, because operating it is penalised.
    from fastapi import HTTPException
    state.barriers["G"] = Barrier("G")
    async def scenario():
        state.pending_repairs["G"] = "BarrierGate"
        await main.manual_barrier_open("G", {"username": "admin", "role": "admin"})
        assert "G" not in state.pending_repairs
        state.barriers["G"].under_maintenance = True
        main.client.barrier_open.reset_mock()
        with pytest.raises(HTTPException) as exc:
            await main.manual_barrier_open("G", {"username": "admin", "role": "admin"})
        assert exc.value.status_code == 409
        main.client.barrier_open.assert_not_awaited()
    asyncio.run(scenario())


def test_simulator_request_timeout_scales_with_game_speed(monkeypatch):
    from app import client as transport
    monkeypatch.setattr(transport, "settings", dataclasses.replace(transport.settings,
                        game_speed=8, request_timeout_s=16))
    async def scenario():
        simulator = transport.SimulatorClient()
        try:
            assert simulator._http.timeout.read == 2
        finally:
            await simulator.aclose()
    asyncio.run(scenario())
