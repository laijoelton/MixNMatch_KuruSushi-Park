"""Standalone mock of the Grand Park Auto REST API and webhook emitter.

The real simulator (``ParkingSimulator-win-x64``) is a Windows-only closed
binary, which makes it useless for CI or cross-platform development. This
server reproduces its documented REST surface (auth, list-*, car goto/charge,
barrier/fan/spot control) with in-memory state that behaves consistently
enough to exercise ``app/client.py``, ``app/routing.py`` and the dispatcher's
webhook handlers end to end - and adds control endpoints (``/_mock/*``, not
part of the real API) to drive scripted scenarios from a test.

Run standalone::

    uvicorn tests.mock_sim_server:app --port 9898

Point the dispatcher at it with ``SIMULATOR_BASE_URL=http://127.0.0.1:9898``,
or import ``app`` directly for in-process ASGI testing (see
``tests/run_route_bench.py``).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

MOCK_TOKEN = "mock-token"


# --------------------------------------------------------------------------- #
# In-memory world
# --------------------------------------------------------------------------- #
@dataclass
class MockSpot:
    name: str
    purpose: str = "Park"
    parking_for_car_type: str = "Any"
    zone_parent: str = "ZONE1"
    detected: list[str] = field(default_factory=list)
    broken: bool = False
    under_maintenance: bool = False


@dataclass
class MockBarrier:
    name: str
    zone_parent: str = "ZONE1"
    state: str = "Open"
    broken: bool = False
    under_maintenance: bool = False


@dataclass
class MockFan:
    name: str
    zone_parent: str = "ZONE1"
    is_on: bool = False
    broken: bool = False
    under_maintenance: bool = False


class World:
    """Everything the mock knows. Reset between tests via /_mock/reset."""

    def __init__(self) -> None:
        self.spots: dict[str, MockSpot] = {}
        self.barriers: dict[str, MockBarrier] = {}
        self.fans: dict[str, MockFan] = {}
        self.webhook_url: Optional[str] = None
        self.sequence_id = 0
        self.sent_webhooks: list[dict[str, Any]] = []
        self.seed_default()

    def seed_default(self, park_spots: int = 10) -> None:
        self.spots = {"ENTRY1": MockSpot("ENTRY1", purpose="EntrySpot", zone_parent=""),
                      "EXIT1": MockSpot("EXIT1", purpose="ExitSpot", zone_parent="ZONE1")}
        for i in range(1, park_spots + 1):
            name = f"S{i}"
            self.spots[name] = MockSpot(name, purpose="Park", zone_parent="ZONE1")
        self.barriers = {"gateA": MockBarrier("gateA")}
        self.fans = {"fan0": MockFan("fan0")}

    def next_sequence(self) -> int:
        self.sequence_id += 1
        return self.sequence_id


world = World()
app = FastAPI(title="Grand Park Auto (mock)")


def _require_auth(authorization: Optional[str]) -> None:
    if authorization != f"Bearer {MOCK_TOKEN}":
        raise HTTPException(401, "invalid or missing bearer token")


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
class LoginIn(BaseModel):
    email: str
    password: str


@app.post("/api/v1/auth/login")
async def login(body: LoginIn) -> dict[str, str]:
    return {"token": MOCK_TOKEN}


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
@app.get("/api/v1/list-parking-spots")
async def list_parking_spots(authorization: Optional[str] = Header(None)) -> list[dict[str, Any]]:
    _require_auth(authorization)
    return [
        {"name": s.name, "purpose": s.purpose, "parkingForCarType": s.parking_for_car_type,
         "zoneParent": s.zone_parent, "detectedCars": list(s.detected),
         "broken": s.broken, "isUnderMaintenance": s.under_maintenance}
        for s in world.spots.values()
    ]


@app.get("/api/v1/list-barriers")
async def list_barriers(authorization: Optional[str] = Header(None)) -> list[dict[str, Any]]:
    _require_auth(authorization)
    return [
        {"name": b.name, "zoneParent": b.zone_parent, "broken": b.broken,
         "isUnderMaintenance": b.under_maintenance, "state": b.state}
        for b in world.barriers.values()
    ]


@app.get("/api/v1/list-exhaust-fans")
async def list_fans(authorization: Optional[str] = Header(None)) -> list[dict[str, Any]]:
    _require_auth(authorization)
    return [
        {"name": f.name, "zoneParent": f.zone_parent, "broken": f.broken,
         "isUnderMaintenance": f.under_maintenance, "isOn": f.is_on}
        for f in world.fans.values()
    ]


@app.get("/api/v1/list-zones")
async def list_zones(authorization: Optional[str] = Header(None)) -> list[dict[str, Any]]:
    _require_auth(authorization)
    zone_names = sorted({s.zone_parent for s in world.spots.values() if s.zone_parent})
    return [{"name": z, "gasCarbonMonoxideLevel": 0, "risk": "Safe"} for z in zone_names]


@app.get("/api/v1/list-alarms")
async def list_alarms(authorization: Optional[str] = Header(None)) -> list[dict[str, Any]]:
    _require_auth(authorization)
    out = []
    for s in world.spots.values():
        if s.broken:
            out.append({"name": s.name, "problem": "Require Maintenance"})
    return out


@app.get("/api/v1/test")
async def test_webhook(authorization: Optional[str] = Header(None)) -> str:
    _require_auth(authorization)
    await _emit({"EventClass": "test_webhook"})
    return "Please wait for webhook call on your configured WebhookUrl"


# --------------------------------------------------------------------------- #
# Car control
# --------------------------------------------------------------------------- #
@app.post("/api/v1/car/{name}/goto/{destination}")
async def car_goto(name: str, destination: str, authorization: Optional[str] = Header(None)) -> None:
    _require_auth(authorization)
    for spot in world.spots.values():
        if name in spot.detected:
            spot.detected.remove(name)
    if destination in ("exit", "leavepark"):
        return
    target = world.spots.get(destination)
    if target is None:
        raise HTTPException(404, f"unknown destination {destination}")
    if target.detected:
        await _emit({"EventClass": "penalty", "Reason": "A car was sent to an occupied spot.",
                     "FineAmount": "10", "Type": "ParkingSpot", "ComponentName": destination})
    target.detected.append(name)
    spot_type = target.purpose
    await _emit({"EventClass": "car_spot_action", "CarPlateNumber": name, "SpotName": destination,
                "SpotType": spot_type, "CarType": "Normal", "Direction": "CarIn",
                "PlannedParkingDurationInMinutes": "1"})


class ChargeIn(BaseModel):
    pass


@app.post("/api/v1/car/{name}/charge")
async def car_charge(name: str, parkingCost: float = 0.0, chargingCost: float = 0.0,
                     authorization: Optional[str] = Header(None)) -> None:
    _require_auth(authorization)
    await _emit({"EventClass": "payment_made", "CarPlateNumber": name,
                "Amount": f"{parkingCost + chargingCost:.2f}", "Reason": "Car Payment"})


# --------------------------------------------------------------------------- #
# Barrier / fan / spot control
# --------------------------------------------------------------------------- #
@app.post("/api/v1/barrier-gates/{name}/open")
async def barrier_open(name: str, authorization: Optional[str] = Header(None)) -> None:
    _require_auth(authorization)
    barrier = world.barriers.setdefault(name, MockBarrier(name))
    barrier.state = "Open"
    await _emit({"EventClass": "gate_action", "Name": name, "Action": "Open"})


@app.post("/api/v1/barrier-gates/{name}/close")
async def barrier_close(name: str, authorization: Optional[str] = Header(None)) -> None:
    _require_auth(authorization)
    barrier = world.barriers.setdefault(name, MockBarrier(name))
    barrier.state = "Closed"
    await _emit({"EventClass": "gate_action", "Name": name, "Action": "Closed"})


@app.post("/api/v1/barrier-gates/{name}/repair")
async def barrier_repair(name: str, authorization: Optional[str] = Header(None)) -> None:
    _require_auth(authorization)
    barrier = world.barriers.get(name)
    if barrier:
        barrier.broken = False
        await _emit({"EventClass": "component_fixed", "Type": "BarrierGate", "Name": name, "RepairCost": "10.00"})


@app.post("/api/v1/exhaust-fans/{name}/on")
async def fan_on(name: str, authorization: Optional[str] = Header(None)) -> None:
    _require_auth(authorization)
    world.fans.setdefault(name, MockFan(name)).is_on = True


@app.post("/api/v1/exhaust-fans/{name}/off")
async def fan_off(name: str, authorization: Optional[str] = Header(None)) -> None:
    _require_auth(authorization)
    world.fans.setdefault(name, MockFan(name)).is_on = False


@app.post("/api/v1/exhaust-fans/{name}/repair")
async def fan_repair(name: str, authorization: Optional[str] = Header(None)) -> None:
    _require_auth(authorization)
    fan = world.fans.get(name)
    if fan:
        fan.broken = False
        await _emit({"EventClass": "component_fixed", "Type": "ExhaustFan", "Name": name, "RepairCost": "10.00"})


@app.post("/api/v1/parking-spots/{name}/repair")
async def spot_repair(name: str, authorization: Optional[str] = Header(None)) -> None:
    _require_auth(authorization)
    spot = world.spots.get(name)
    if spot:
        spot.broken = False
        await _emit({"EventClass": "component_fixed", "Type": "ParkingSpot", "Name": name, "RepairCost": "10.00"})


# --------------------------------------------------------------------------- #
# Mock-only test control (not part of the real API)
# --------------------------------------------------------------------------- #
class ResetIn(BaseModel):
    park_spots: int = 10
    webhook_url: Optional[str] = None


@app.post("/_mock/reset")
async def mock_reset(body: ResetIn) -> dict[str, Any]:
    world.seed_default(body.park_spots)
    world.sequence_id = 0
    world.sent_webhooks.clear()
    if body.webhook_url is not None:
        world.webhook_url = body.webhook_url
    return {"ok": True, "spots": len(world.spots)}


class BreakIn(BaseModel):
    name: str
    component_type: str = "ParkingSpot"
    fine_amount: float = 10.0


@app.post("/_mock/break")
async def mock_break(body: BreakIn) -> dict[str, Any]:
    if body.component_type == "ParkingSpot" and body.name in world.spots:
        world.spots[body.name].broken = True
    elif body.component_type == "BarrierGate" and body.name in world.barriers:
        world.barriers[body.name].broken = True
    elif body.component_type == "ExhaustFan" and body.name in world.fans:
        world.fans[body.name].broken = True
    await _emit({"EventClass": "component_broken", "Type": body.component_type, "Name": body.name,
                "FineAmount": f"{body.fine_amount:.2f}"})
    return {"ok": True}


class EmitIn(BaseModel):
    payload: dict[str, Any]


@app.post("/_mock/emit")
async def mock_emit(body: EmitIn) -> dict[str, Any]:
    """Fire an arbitrary webhook payload (EventId/SequenceId/ServerDateTime
    auto-filled if absent) - for scripting scenarios the REST surface above
    cannot express directly, e.g. a car leaving its spot or reaching an exit."""
    await _emit(dict(body.payload))
    return {"ok": True}


@app.get("/_mock/webhooks")
async def mock_webhooks() -> list[dict[str, Any]]:
    return world.sent_webhooks


# --------------------------------------------------------------------------- #
# Webhook delivery
# --------------------------------------------------------------------------- #
async def _emit(payload: dict[str, Any]) -> None:
    payload.setdefault("EventId", str(uuid.uuid4()))
    payload["SequenceId"] = world.next_sequence()
    payload.setdefault("ServerDateTime", time.strftime("%Y-%m-%d %H:%M:%S"))
    payload.setdefault("Signature", None)
    world.sent_webhooks.append(payload)

    if not world.webhook_url:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(world.webhook_url, json=payload)
    except httpx.HTTPError:
        pass  # A dropped webhook must not break the mock - same contract as the real simulator.


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=9898)
