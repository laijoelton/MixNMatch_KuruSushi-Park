"""Test setup: point the app at a throwaway database before anything imports it."""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="pg-tests-"))
os.environ["DATABASE_PATH"] = str(_TMP / "test.db")
os.environ["AUTOPILOT"] = "false"
os.environ["SEED_FROM_LEVEL"] = ""
os.environ["SIMULATOR_LOG"] = ""  # never follow the real simulator console in tests
os.environ["DASHBOARD_ADMIN_PASSWORD"] = "admin123"
os.environ["DASHBOARD_OPERATOR_PASSWORD"] = "operator123"

# Everything below imports app modules, which must happen only after the
# environment overrides above are in place -- see the module docstring.
import asyncio
import dataclasses

import pytest

from app import main
from app.state import Barrier, Spot, state


# --------------------------------------------------------------------------- #
# Shared Level 2 dispatcher fixture (moved here from tests/test_zone_gates.py
# so tests/test_gate_hold_open.py can use it too; pytest injects a conftest.py
# fixture into every test module in this directory automatically).
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
