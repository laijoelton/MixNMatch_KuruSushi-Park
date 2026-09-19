"""GATE_HOLD_OPEN keeps entry gates up between streams (spec Section 2)."""
import asyncio
import dataclasses

import pytest

from app import main
from app.state import state


def _hold(monkeypatch, enabled: bool):
    monkeypatch.setattr(main, "settings",
                        dataclasses.replace(main.settings, gate_hold_open=enabled))


def test_entry_gate_stays_open_when_holding(lvl2, monkeypatch):
    _hold(monkeypatch, True)
    state.update_barrier_state("gate5", "Open")
    asyncio.run(main._close_idle_gates())
    assert ("close", "gate5") not in lvl2


def test_entry_gate_still_closes_when_not_holding(lvl2, monkeypatch):
    _hold(monkeypatch, False)
    state.update_barrier_state("gate5", "Open")
    asyncio.run(main._close_idle_gates())
    assert ("close", "gate5") in lvl2


def test_exit_gate_always_closes_even_when_holding(lvl2, monkeypatch):
    # gate6 is ZONE3's exit: the CarEscapedWithoutPaying interlock. Never held.
    _hold(monkeypatch, True)
    state.update_barrier_state("gate6", "Open")
    asyncio.run(main._close_idle_gates())
    assert ("close", "gate6") in lvl2


def test_zone_maintenance_still_closes_a_held_entry_gate(lvl2, monkeypatch):
    _hold(monkeypatch, True)
    state.update_barrier_state("gate5", "Open")
    state.zone_maintenance["ZONE3"] = {"entry": "gate5", "exit": "gate6",
                                       "trigger": "test", "stage": "entry"}
    try:
        asyncio.run(main._close_idle_gates())
        assert ("close", "gate5") in lvl2, "a draining zone must still shut its gate"
    finally:
        # state.zone_maintenance is a module-level singleton outliving this
        # test; other test modules (e.g. test_level2_maintenance.py's
        # `maintenance` fixture) don't clear it, so a leaked ZONE3 entry here
        # crashes their _advance_zone_maintenance() on the missing "todo" key.
        state.zone_maintenance.pop("ZONE3", None)
