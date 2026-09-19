"""FastAPI dispatcher: inbound Grand Park Auto webhooks, outbound REST commands.

This service is the sole external controller of the closed
``ParkingSimulator-win-x64`` binary. It never polls; every mutation to local
state happens strictly in reaction to an inbound webhook (see ``app/state.py``
docstring), and every simulator-facing action is a direct REST call made from
inside one of the handlers below.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.client import client
from app.config import settings
from app.routing import find_best_spot, ring
from app.state import state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dispatcher.main")


def compute_parking_cost(entered_monotonic: float) -> float:
    minutes = max(0.0, (time.monotonic() - entered_monotonic) / 60.0)
    return round(max(settings.minimum_charge, minutes * settings.parking_rate_per_minute), 2)


def verify_signature(payload: dict[str, Any], provided: Optional[str]) -> bool:
    """Recompute the webhook signature per the organizer's documented recipe:
    sort field names alphabetically (excluding ``Signature``), join the
    corresponding values with ``|``, hash the result, compare to ``provided``.

    If ``WEBHOOK_SECRET`` is configured the hash is HMAC-keyed; otherwise a
    plain digest of the joined string is used, matching the documented
    example which shows no separate signing key.
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await client.login()
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

    log.info("startup sync complete: %d spots, %d barriers, %d zones, %d fans",
             len(state.spots), len(state.barriers), len(state.zones), len(state.fans))
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(
    title="KuruSushi-Park Dispatcher",
    version="1.0.0",
    description="Supervisory dispatch layer over the Grand Park Auto simulator.",
    lifespan=lifespan,
)


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
    }


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
# Event handlers
# --------------------------------------------------------------------------- #
async def _handle_car_spot_action(payload: dict[str, Any]) -> None:
    plate = payload["CarPlateNumber"]
    spot_name = payload["SpotName"]
    spot_type = payload.get("SpotType", "")
    direction = payload.get("Direction", "")

    if spot_type == "EntrySpot" and direction == "CarIn":
        state.start_session(plate, gate=spot_name)
        car_type = payload.get("CarType", "Normal")
        candidate_type = "Electric" if car_type.lower() == "electric" else "Any"
        candidates = state.available_spots(car_type=candidate_type)
        target = find_best_spot(spot_name, candidates)
        if target is None:
            log.error("no available spot for %s at %s - lot full", plate, spot_name)
            return
        if not state.reserve_spot(target, plate):
            candidates = [c for c in state.available_spots(car_type=candidate_type) if c != target]
            target = find_best_spot(spot_name, candidates)
            if target is None or not state.reserve_spot(target, plate):
                log.error("failed to reserve any spot for %s", plate)
                return
        state.assign_spot(plate, target)
        await client.car_goto(plate, target)
        log.info("dispatched %s from %s to %s", plate, spot_name, target)
        return

    if spot_type == "Park" and direction == "CarIn":
        state.mark_parked(plate, spot_name)
        return

    if spot_type == "Park" and direction == "CarOut":
        state.mark_spot_vacant(spot_name)
        ready = state.pop_ready_repair(spot_name)
        if ready:
            await client.spot_repair(spot_name)
            log.info("deferred repair now applied to vacated spot %s", spot_name)
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
        log.info("charged %s %.2f at exit spot %s", plate, cost, spot_name)
        return

    if spot_type == "ExitSpot" and direction == "CarOut":
        state.complete_session(plate)
        log.info("%s left the facility via %s", plate, spot_name)
        return


async def _handle_component_broken(payload: dict[str, Any]) -> None:
    component_type = payload["Type"]
    name = payload["Name"]
    state.set_component_broken(component_type, name)
    log.warning("component broken: %s %s (fine %s)", component_type, name, payload.get("FineAmount"))

    if component_type == "ParkingSpot":
        spot = state.spots.get(name)
        if spot is not None and spot.occupant_plate is not None:
            state.queue_deferred_repair(name, component_type)
            log.info("repair for occupied spot %s deferred until vacated", name)
            return
        await client.spot_repair(name)
    elif component_type == "BarrierGate":
        await client.barrier_repair(name)
    elif component_type == "ExhaustFan":
        await client.fan_repair(name)


async def _handle_component_fixed(payload: dict[str, Any]) -> None:
    component_type = payload["Type"]
    name = payload["Name"]
    state.set_component_fixed(component_type, name)
    log.info("component fixed: %s %s", component_type, name)


async def _handle_carbon_monoxide_event(payload: dict[str, Any]) -> None:
    zone_name = payload["ZoneName"]
    co_level = float(payload.get("CarbonMonoxideLevel", 0.0))
    danger_level = payload.get("DangerLevel", "Safe")
    state.update_zone(zone_name, co_level, danger_level)
    log.warning("CO event in %s: level=%.2f danger=%s", zone_name, co_level, danger_level)

    if danger_level in ("High", "Critical"):
        for fan_name in state.fans_in_zone(zone_name):
            fan = state.fans[fan_name]
            if not fan.is_on and not fan.broken and not fan.under_maintenance:
                await client.fan_on(fan_name)
                fan.is_on = True
                log.info("exhaust fan %s switched on to mitigate CO in %s", fan_name, zone_name)


async def _handle_gate_action(payload: dict[str, Any]) -> None:
    name = payload["Name"]
    action = payload.get("Action", "")
    state.update_barrier_state(name, action)


async def _handle_payment_made(payload: dict[str, Any]) -> None:
    plate = payload["CarPlateNumber"]
    amount = float(payload.get("Amount", 0.0))
    session = state.get_session(plate)
    if session is not None and session.expected_amount is not None:
        if abs(amount - session.expected_amount) > 0.01:
            log.warning("payment mismatch for %s: expected %.2f, server reported %.2f - flagged as suspect",
                       plate, session.expected_amount, amount)
            return
    accepted = state.mark_paid(plate, amount)
    if not accepted:
        log.warning("unsolicited or duplicate payment_made for %s (amount %.2f) - ignored", plate, amount)


async def _handle_penalty(payload: dict[str, Any]) -> None:
    log.error("PENALTY: %s - fine %s (%s %s)", payload.get("Reason"), payload.get("FineAmount"),
              payload.get("Type"), payload.get("ComponentName"))


async def _handle_test_webhook(payload: dict[str, Any]) -> None:
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
