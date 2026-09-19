"""FastAPI dispatcher + dual dashboard: inbound webhooks, outbound REST, live UI.

This service is the sole external controller of the closed
``ParkingSimulator-win-x64`` binary. It never polls; every mutation to local
state happens strictly in reaction to an inbound webhook (see ``app/state.py``
docstring). Two browser-facing surfaces are layered on top of that headless
core without changing its behaviour:

* ``/`` - an operator HUD (live telemetry, digital twin, manual controls).
* ``/gate`` - a mobile-first cinema-style bay picker for walk-in check-in.

Both are pure read/observe layers over the same ``ParkingState`` the webhook
handlers mutate, pushed to connected browsers over ``/ws/live``.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.client import client
from app.config import settings
from app.queue_worker import maintenance_queue
from app.routing import rank_spots, ring
from app.state import BarrierPosition, state
from app.ws_manager import manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dispatcher.main")

ROOT_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT_DIR / "static"
TEMPLATES_DIR = ROOT_DIR / "templates"


def compute_parking_cost(entered_monotonic: float) -> float:
    minutes = max(0.0, (time.monotonic() - entered_monotonic) / 60.0)
    return round(max(settings.minimum_charge, minutes * settings.parking_rate_per_minute), 2)


def verify_signature(payload: dict[str, Any], provided: Optional[str]) -> bool:
    """Recompute the webhook signature per the organizer's documented recipe:
    sort field names alphabetically (excluding ``Signature``), join the
    corresponding values with ``|``, hash the result, compare to ``provided``.

    If ``WEBHOOK_SECRET`` is configured the hash is HMAC-keyed with it;
    otherwise a plain digest of the joined string is used, matching the
    unsigned examples in the organizer's docs.
    """
    if provided is None:
        return not settings.webhook_secret
    fields = {k: v for k, v in payload.items() if k != "Signature"}
    joined = "|".join(str(fields[k]) for k in sorted(fields.keys()))
    if settings.webhook_secret:
        digest = hmac.new(
            settings.webhook_secret.encode(), joined.encode(),
            getattr(hashlib, settings.webhook_hash_algo),
        ).hexdigest()
    else:
        digest = hashlib.new(settings.webhook_hash_algo, joined.encode()).hexdigest()
    return hmac.compare_digest(digest, provided)


async def sync_from_simulator() -> dict[str, int]:
    """One-shot list-* refresh. Called at startup and on manual operator request only -
    never on a timer, per the organizer's no-polling rule."""
    spots = await client.list_parking_spots()
    barriers = await client.list_barriers()
    zones = await client.list_zones()
    try:
        fans = await client.list_exhaust_fans()
    except Exception:  # noqa: BLE001 - exhaust fans are not present in every level
        fans = []

    state.load_spots(spots)
    state.load_barriers(barriers)
    state.load_zones(zones)
    state.load_fans(fans)
    ring.rebuild(list(state.spots.keys()) + list(state.barriers.keys()))
    await _ensure_barriers_open()
    counts = {"spots": len(state.spots), "barriers": len(state.barriers),
              "zones": len(state.zones), "fans": len(state.fans)}
    state.log_activity(f"Manual sync: {counts['spots']} spots, {counts['barriers']} barriers, "
                       f"{counts['zones']} zones, {counts['fans']} fans")
    return counts


async def _ensure_barriers_open() -> None:
    """Keep every operable barrier open by default.

    This is a fully automated, unmanned lot - there is no attendant to raise
    a physical arm per car. A barrier left ``Closed`` (the simulator's own
    default on some levels) silently strands every dispatched vehicle at the
    entry spot with no error and no further webhook, since the car can never
    physically reach its assigned bay. Broken or under-maintenance barriers
    are left alone; opening those would just trigger
    ``Penalty_OperateElementWhileUnderRepair``.
    """
    for barrier in list(state.barriers.values()):
        if barrier.broken or barrier.under_maintenance:
            continue
        if barrier.state != BarrierPosition.OPEN:
            try:
                await client.barrier_open(barrier.name)
                state.update_barrier_state(barrier.name, "Open")
                state.log_activity(f"Auto-opened barrier {barrier.name} (was {barrier.state.value})")
                log.info("auto-opened barrier %s to clear the entry path", barrier.name)
            except Exception:  # noqa: BLE001 - one stuck barrier must not block the others
                log.exception("failed to auto-open barrier %s", barrier.name)


async def _broadcast_loop() -> None:
    while True:
        try:
            snapshot = state.snapshot()
            snapshot["type"] = "frame"
            snapshot["server_time"] = time.time()
            snapshot["ws_clients"] = manager.count
            snapshot["maintenance_queue"] = maintenance_queue.stats
            await manager.broadcast(snapshot)
        except Exception:  # noqa: BLE001 - the broadcaster must never die
            log.exception("broadcast tick failed")
        await asyncio.sleep(settings.broadcast_interval_s)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await client.login()
    counts = await sync_from_simulator()
    log.info("startup sync complete: %d spots, %d barriers, %d zones, %d fans",
             counts["spots"], counts["barriers"], counts["zones"], counts["fans"])

    maintenance_queue.start()
    broadcaster = asyncio.create_task(_broadcast_loop(), name="dispatcher-broadcaster")
    try:
        yield
    finally:
        broadcaster.cancel()
        try:
            await broadcaster
        except asyncio.CancelledError:
            pass
        await maintenance_queue.stop()
        await client.aclose()


app = FastAPI(
    title="KuruSushi-Park Dispatcher",
    version="2.0.0",
    description="Supervisory dispatch layer over the Grand Park Auto simulator, with operator and gate dashboards.",
    lifespan=lifespan,
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR)) if TEMPLATES_DIR.exists() else None


# --------------------------------------------------------------------------- #
# Dispatch core (shared by the real webhook path and the manual/dry-run path)
# --------------------------------------------------------------------------- #
async def dispatch_entry(plate: str, gate_name: str, car_type: str = "Normal",
                         dry_run: bool = False) -> dict[str, Any]:
    state.start_session(plate, gate=gate_name)
    candidate_type = "Electric" if car_type.lower() == "electric" else "Any"
    candidates = state.available_spots(car_type=candidate_type)
    ranked = rank_spots(gate_name, candidates)
    target = ranked[0][0] if ranked else None

    if dry_run:
        return {"plate": plate, "gate": gate_name, "target": target,
                "ranked_candidates": ranked[:10], "dispatched": False}

    if target is None:
        state.log_activity(f"No available spot for {plate} at {gate_name} - lot full", level="error")
        return {"plate": plate, "gate": gate_name, "target": None, "dispatched": False}

    if not state.reserve_spot(target, plate):
        candidates = [c for c in state.available_spots(car_type=candidate_type) if c != target]
        ranked = rank_spots(gate_name, candidates)
        target = ranked[0][0] if ranked else None
        if target is None or not state.reserve_spot(target, plate):
            state.log_activity(f"Failed to reserve any spot for {plate}", level="error")
            return {"plate": plate, "gate": gate_name, "target": None, "dispatched": False}

    state.assign_spot(plate, target)
    await client.car_goto(plate, target)
    state.log_activity(f"Dispatched {plate} from {gate_name} to {target}")
    log.info("dispatched %s from %s to %s", plate, gate_name, target)
    return {"plate": plate, "gate": gate_name, "target": target, "dispatched": True}


async def assign_specific_spot(plate: str, gate_name: str, spot_name: str,
                               car_type: str = "Normal") -> dict[str, Any]:
    """Used by the gate portal: honour the user's explicit pick if it's still valid."""
    state.start_session(plate, gate=gate_name)
    if not state.reserve_spot(spot_name, plate):
        return {"plate": plate, "gate": gate_name, "target": spot_name, "dispatched": False,
                "reason": "spot no longer available"}
    state.assign_spot(plate, spot_name)
    await client.car_goto(plate, spot_name)
    state.log_activity(f"Check-in: {plate} chose {spot_name} at {gate_name}")
    return {"plate": plate, "gate": gate_name, "target": spot_name, "dispatched": True}


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def operator_dashboard(request: Request):
    if templates is None:
        raise HTTPException(500, "templates directory missing")
    return templates.TemplateResponse(request, "index.html", {
        "team_name": "KuruSushi-Park", "asset_version": str(int(time.time())),
    })


@app.get("/gate", response_class=HTMLResponse, include_in_schema=False)
async def gate_portal(request: Request, gate: Optional[str] = Query(None)):
    if templates is None:
        raise HTTPException(500, "templates directory missing")
    gates = state.entry_gates()
    selected = gate if gate in gates else (gates[0] if gates else None)
    return templates.TemplateResponse(request, "gate.html", {
        "gates": gates, "selected_gate": selected, "asset_version": str(int(time.time())),
    })


# --------------------------------------------------------------------------- #
# Read APIs
# --------------------------------------------------------------------------- #
@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "spots": len(state.spots),
        "barriers": len(state.barriers),
        "zones": len(state.zones),
        "fans": len(state.fans),
        "active_sessions": len(state.sessions),
        "last_sequence_id": state.last_sequence_id,
        "maintenance_queue": maintenance_queue.stats,
        "ws_clients": manager.count,
    }


@app.get("/api/state")
async def get_state() -> dict[str, Any]:
    return state.snapshot()


@app.get("/api/spots")
async def get_spots() -> dict[str, Any]:
    return {"spots": state.snapshot()["spots"], "occupancy": state.occupancy_counts()}


@app.get("/api/gates")
async def get_gates() -> dict[str, Any]:
    return {"gates": state.entry_gates()}


@app.get("/api/broken")
async def get_broken() -> dict[str, Any]:
    return {"components": state.broken_components(), "deferred_repairs": dict(state.deferred_repairs)}


# --------------------------------------------------------------------------- #
# Manual / operator controls
# --------------------------------------------------------------------------- #
@app.post("/api/manual/sync")
async def manual_sync() -> dict[str, Any]:
    counts = await sync_from_simulator()
    return {"ok": True, **counts}


class ManualArrivalIn(BaseModel):
    plate: str = Field(..., min_length=1, max_length=16)
    gate: str = Field(..., min_length=1, max_length=32)
    car_type: str = Field("Normal", max_length=16)
    dry_run: bool = False


@app.post("/api/manual/arrival")
async def manual_arrival(body: ManualArrivalIn) -> dict[str, Any]:
    if body.gate not in state.spots:
        raise HTTPException(404, f"unknown gate/spot {body.gate}")
    return await dispatch_entry(body.plate, body.gate, body.car_type, body.dry_run)


@app.post("/api/manual/barrier/{name}/open")
async def manual_barrier_open(name: str) -> dict[str, Any]:
    await client.barrier_open(name)
    state.update_barrier_state(name, "Open")
    state.log_activity(f"Operator opened barrier {name}")
    return {"ok": True, "name": name, "state": "Open"}


@app.post("/api/manual/barrier/{name}/close")
async def manual_barrier_close(name: str) -> dict[str, Any]:
    await client.barrier_close(name)
    state.update_barrier_state(name, "Closed")
    state.log_activity(f"Operator closed barrier {name}")
    return {"ok": True, "name": name, "state": "Closed"}


@app.post("/api/manual/repair/{name}")
async def manual_repair(name: str) -> dict[str, Any]:
    component_type = (
        "ParkingSpot" if name in state.spots
        else "BarrierGate" if name in state.barriers
        else "ExhaustFan" if name in state.fans
        else None
    )
    if component_type is None:
        raise HTTPException(404, f"unknown component {name}")
    await _queue_repair(component_type, name)
    return {"ok": True, "name": name, "type": component_type, "queued": True}


# --------------------------------------------------------------------------- #
# Gate portal API
# --------------------------------------------------------------------------- #
class CheckinIn(BaseModel):
    plate: str = Field(..., min_length=1, max_length=16)
    gate: str = Field(..., min_length=1, max_length=32)
    spot: str = Field(..., min_length=1, max_length=32)
    car_type: str = Field("Normal", max_length=16)


@app.post("/api/gate/checkin")
async def gate_checkin(body: CheckinIn) -> dict[str, Any]:
    if body.gate not in state.spots:
        raise HTTPException(404, f"unknown gate {body.gate}")
    if body.spot not in state.spots:
        raise HTTPException(404, f"unknown spot {body.spot}")
    result = await assign_specific_spot(body.plate, body.gate, body.spot, body.car_type)
    if not result["dispatched"]:
        raise HTTPException(409, result.get("reason", "spot unavailable"))
    return result


# --------------------------------------------------------------------------- #
# Live WebSocket feed (operator HUD + gate picker)
# --------------------------------------------------------------------------- #
@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await manager.connect(ws)
    try:
        await ws.send_json({"type": "hello", "server_time": time.time(), **state.snapshot()})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(ws)


# --------------------------------------------------------------------------- #
# Inbound simulator webhook
# --------------------------------------------------------------------------- #
@app.post("/webhooks/simulator")
async def simulator_webhook(request: Request) -> JSONResponse:
    payload = await request.json()
    signature = payload.get("Signature")
    if not verify_signature(payload, signature):
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    event_id = payload.get("EventId")
    if event_id and state.is_duplicate(event_id):
        return JSONResponse({"status": "duplicate", "event_id": event_id})

    sequence_id = payload.get("SequenceId")
    gap = state.observe_sequence(sequence_id)
    if gap:
        log.warning("webhook sequence gap detected: %d missing event(s) before SequenceId=%s", gap, sequence_id)

    if settings.webhook_debug:
        log.info("RAW WEBHOOK PAYLOAD: %s", payload)

    event_class = payload.get("EventClass", "")
    handler = _HANDLERS.get(event_class)
    if handler is None:
        log.info("unhandled EventClass=%s (EventId=%s)", event_class, event_id)
        return JSONResponse({"status": "ignored", "event_class": event_class})

    try:
        await handler(payload)
    except Exception:  # noqa: BLE001 - a bad event must never crash the receiver
        log.exception("handler failed for EventClass=%s EventId=%s", event_class, event_id)
        return JSONResponse({"status": "error", "event_class": event_class}, status_code=200)

    return JSONResponse({"status": "processed", "event_class": event_class})


# --------------------------------------------------------------------------- #
# Maintenance queue helper
# --------------------------------------------------------------------------- #
async def _queue_repair(component_type: str, name: str) -> None:
    if component_type == "ParkingSpot":
        action: Callable[[], Awaitable[None]] = lambda: client.spot_repair(name)
    elif component_type == "BarrierGate":
        action = lambda: client.barrier_repair(name)
    elif component_type == "ExhaustFan":
        action = lambda: client.fan_repair(name)
    else:
        return
    await maintenance_queue.submit(f"repair {component_type}:{name}", action, priority=10)


# --------------------------------------------------------------------------- #
# Event handlers
# --------------------------------------------------------------------------- #
async def _handle_car_spot_action(payload: dict[str, Any]) -> None:
    plate = payload["CarPlateNumber"]
    spot_name = payload["SpotName"]
    spot_type = payload.get("SpotType", "")
    direction = payload.get("Direction", "")

    if spot_type == "EntrySpot" and direction == "CarIn":
        car_type = payload.get("CarType", "Normal")
        await dispatch_entry(plate, spot_name, car_type)
        return

    if spot_type == "Park" and direction == "CarIn":
        state.mark_parked(plate, spot_name)
        state.log_activity(f"{plate} parked at {spot_name}")
        return

    if spot_type == "Park" and direction == "CarOut":
        state.mark_spot_vacant(spot_name)
        state.log_activity(f"{plate} vacated {spot_name}")
        ready = state.pop_ready_repair(spot_name)
        if ready:
            await _queue_repair(ready, spot_name)
            log.info("deferred repair for %s now queued after vacancy", spot_name)
        return

    if spot_type == "ExitSpot" and direction == "CarIn":
        session = state.get_session(plate)
        if session is None or session.charged:
            return
        cost = compute_parking_cost(session.created_at)
        session.expected_amount = cost
        await client.car_charge(plate, cost)
        state.mark_charged(plate)
        state.mark_at_exit(plate)
        state.log_activity(f"Charged {plate} {cost:.2f} at {spot_name}")
        log.info("charged %s %.2f at exit spot %s", plate, cost, spot_name)
        return

    if spot_type == "ExitSpot" and direction == "CarOut":
        state.complete_session(plate)
        state.log_activity(f"{plate} left the facility via {spot_name}")
        log.info("%s left the facility via %s", plate, spot_name)
        return


async def _handle_component_broken(payload: dict[str, Any]) -> None:
    component_type = payload["Type"]
    name = payload["Name"]
    state.set_component_broken(component_type, name)
    state.log_activity(f"{component_type} {name} broken (fine {payload.get('FineAmount')})", level="warn")
    log.warning("component broken: %s %s (fine %s)", component_type, name, payload.get("FineAmount"))

    if component_type == "ParkingSpot":
        spot = state.spots.get(name)
        if spot is not None and spot.occupant_plate is not None:
            state.queue_deferred_repair(name, component_type)
            log.info("repair for occupied spot %s deferred until vacated", name)
            return
    await _queue_repair(component_type, name)


async def _handle_component_fixed(payload: dict[str, Any]) -> None:
    component_type = payload["Type"]
    name = payload["Name"]
    state.set_component_fixed(component_type, name)
    state.log_activity(f"{component_type} {name} fixed")
    log.info("component fixed: %s %s", component_type, name)


async def _handle_carbon_monoxide_event(payload: dict[str, Any]) -> None:
    zone_name = payload["ZoneName"]
    co_level = float(payload.get("CarbonMonoxideLevel", 0.0))
    danger_level = payload.get("DangerLevel", "Safe")
    state.update_zone(zone_name, co_level, danger_level)
    state.log_activity(f"CO {danger_level} in {zone_name} ({co_level:.1f})",
                       level="warn" if danger_level in ("High", "Critical") else "info")
    log.warning("CO event in %s: level=%.2f danger=%s", zone_name, co_level, danger_level)

    if danger_level in ("High", "Critical"):
        for fan_name in state.fans_in_zone(zone_name):
            fan = state.fans[fan_name]
            if not fan.is_on and not fan.broken and not fan.under_maintenance:
                await client.fan_on(fan_name)
                fan.is_on = True
                state.log_activity(f"Exhaust fan {fan_name} switched on for {zone_name}")
                log.info("exhaust fan %s switched on to mitigate CO in %s", fan_name, zone_name)


async def _handle_gate_action(payload: dict[str, Any]) -> None:
    name = payload["Name"]
    action = payload.get("Action", "")
    state.update_barrier_state(name, action)

    barrier = state.barriers.get(name)
    if barrier is not None and action == "Closed" and not barrier.broken and not barrier.under_maintenance:
        # Unmanned lot: nothing should leave a barrier closed once it settles -
        # a car sitting behind it would otherwise strand silently with no
        # further webhook. Reopen it off the critical path.
        await maintenance_queue.submit(f"reopen barrier {name}", lambda: client.barrier_open(name), priority=5)


async def _handle_payment_made(payload: dict[str, Any]) -> None:
    plate = payload["CarPlateNumber"]
    amount = float(payload.get("Amount", 0.0))
    session = state.get_session(plate)
    if session is not None and session.expected_amount is not None:
        if abs(amount - session.expected_amount) > 0.01:
            state.log_activity(f"Payment mismatch for {plate}: expected {session.expected_amount:.2f}, "
                               f"got {amount:.2f}", level="warn")
            log.warning("payment mismatch for %s: expected %.2f, server reported %.2f - flagged as suspect",
                       plate, session.expected_amount, amount)
            return
    accepted = state.mark_paid(plate, amount)
    if not accepted:
        log.warning("unsolicited or duplicate payment_made for %s (amount %.2f) - ignored", plate, amount)


async def _handle_penalty(payload: dict[str, Any]) -> None:
    reason = payload.get("Reason", "")
    fine = float(payload.get("FineAmount", 0.0))
    component_type = payload.get("Type", "")
    component_name = payload.get("ComponentName", "")
    state.record_penalty(reason, fine, component_type, component_name)
    state.log_activity(f"PENALTY: {reason} (-{fine})", level="error")
    log.error("PENALTY: %s - fine %s (%s %s)", reason, fine, component_type, component_name)


async def _handle_test_webhook(payload: dict[str, Any]) -> None:
    state.log_activity("Test webhook received")
    log.info("test webhook received: %s", payload.get("EventId"))


_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {
    "car_spot_action": _handle_car_spot_action,
    "component_broken": _handle_component_broken,
    "component_fixed": _handle_component_fixed,
    "carbon_monoxide_event": _handle_carbon_monoxide_event,
    "gate_action": _handle_gate_action,
    "payment_made": _handle_payment_made,
    "penalty": _handle_penalty,
    "test_webhook": _handle_test_webhook,
}
