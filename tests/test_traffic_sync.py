"""The dispatcher must recover when it started before a level was running,
and must turn cars away (not strand them) when the lot is genuinely full."""
import dataclasses
import itertools

import pytest
from fastapi.testclient import TestClient

from app import main
from app.state import SpotStatus, state

_ids = itertools.count(1)

LEVEL_SPOTS = [
    {"name": "ENTRY1", "purpose": "EntrySpot", "parkingForCarType": "Any", "zoneParent": "", "detectedCars": 3},
    {"name": "S1", "purpose": "Park", "parkingForCarType": "Any", "zoneParent": "ZONE1", "detectedCars": 0},
    {"name": "S2", "purpose": "Park", "parkingForCarType": "Any", "zoneParent": "ZONE1", "detectedCars": 0},
]


def _arrival(plate):
    return {"EventClass": "car_spot_action", "CarPlateNumber": plate, "SpotName": "ENTRY1",
            "SpotType": "EntrySpot", "CarType": "Normal", "Direction": "CarIn",
            "PlannedParkingDurationInMinutes": "2", "EventId": f"traffic-{next(_ids)}",
            "SequenceId": next(_ids), "Signature": "x", "ServerDateTime": "2026-09-19 17:00:00"}


@pytest.fixture
def sim(monkeypatch):
    """Fake simulator: records calls; serves LEVEL_SPOTS once a level is 'running'."""
    calls = {"list": 0, "goto": []}
    level = {"running": True}

    async def list_spots():
        calls["list"] += 1
        return LEVEL_SPOTS if level["running"] else []

    async def empty():
        return []

    async def goto(plate, destination):
        calls["goto"].append((plate, destination))

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(main.client, "list_parking_spots", list_spots)
    monkeypatch.setattr(main.client, "list_barriers", empty)
    monkeypatch.setattr(main.client, "list_zones", empty)
    monkeypatch.setattr(main.client, "list_exhaust_fans", empty)
    monkeypatch.setattr(main.client, "car_goto", goto)
    monkeypatch.setattr(main.client, "barrier_open", noop)
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, autopilot=True))
    monkeypatch.setattr(main, "TRAFFIC_SYNC_COOLDOWN_S", 0.0)
    state.spots.clear()
    state.sessions.clear()
    main._live_bays_synced = False
    return calls, level


def test_first_arrival_loads_bays_when_started_early(sim):
    calls, _ = sim
    client = TestClient(main.app)
    client.post("/webhooks/simulator", json=_arrival("AAA 111"))
    assert calls["list"] == 1
    assert any(s.purpose == "Park" for s in state.spots.values())
    assert calls["goto"] and calls["goto"][0][1] in ("S1", "S2")  # the car was actually dispatched

    client.post("/webhooks/simulator", json=_arrival("BBB 222"))
    assert calls["list"] == 1  # real bays are known now - no further paid list calls


def test_retries_while_level_not_started(sim):
    calls, level = sim
    level["running"] = False
    client = TestClient(main.app)
    client.post("/webhooks/simulator", json=_arrival("CCC 333"))
    assert not any(s.purpose == "Park" for s in state.spots.values())
    level["running"] = True
    client.post("/webhooks/simulator", json=_arrival("DDD 444"))
    assert any(s.purpose == "Park" for s in state.spots.values())


def test_full_lot_turns_car_away(sim):
    calls, _ = sim
    client = TestClient(main.app)
    client.post("/webhooks/simulator", json=_arrival("EEE 555"))   # loads bays, takes one
    for spot in state.spots.values():
        if spot.purpose == "Park":
            spot.status = SpotStatus.OCCUPIED
    client.post("/webhooks/simulator", json=_arrival("FFF 666"))
    assert ("FFF 666", "leavepark") in calls["goto"]
    assert state.get_session("FFF 666") is None
