"""FastAPI dispatcher: inbound Grand Park Auto webhooks, outbound REST commands.

This service is the sole external controller of the closed
``ParkingSimulator-win-x64`` binary. It never polls; every mutation to local
state happens strictly in reaction to an inbound webhook (see ``app/state.py``
docstring), and every simulator-facing action is a direct REST call made from
inside one of the handlers below.

Two durability rules shape the request path:

1. **Persist before acknowledging.** Every webhook is written to SQLite
   (``app/db.py``) the moment it arrives, before any decision is made, so a
   crash mid-handler cannot lose the event.
2. **Never reject on an unproven signature.** See ``app/signature.py`` -- the
   documented recipe does not reproduce the organizer's own samples, so the
   verifier calibrates against live traffic instead of dropping events.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import db
from app.client import client
from app.config import settings
from app.routing import find_best_spot, has_distance_table, load_distance_table, ring
from app.seed import load_level
from app.signature import verify as verify_signature
from app.state import SessionPhase, state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dispatcher.main")


# --------------------------------------------------------------------------- #
# Command gate
# --------------------------------------------------------------------------- #
async def act(description: str, coro_factory: Callable[[], Awaitable[Any]]) -> bool:
    """Send a command to the simulator, unless AUTOPILOT is off.

    With ``AUTOPILOT=false`` every intended action is logged and skipped, so the
    decision logic can be reviewed against a live simulator before it is allowed
    to touch it.
    """
    if not settings.autopilot:
        log.info("[dry-run] %s", description)
        return False
    try:
        log.info("[act] %s", description)
        await coro_factory()
        return True
    except Exception as exc:  # noqa: BLE001 - a failed command must not kill the handler
        log.error("[act:FAILED] %s -> %s", description, exc)
        return False


# --------------------------------------------------------------------------- #
# Billing
# --------------------------------------------------------------------------- #
def compute_charge(session) -> tuple[float, float, float]:
    """Return ``(parking_cost, charging_cost, minutes)`` for a session.

    The documentation contradicts itself here. One page says "Charging cost: 1
    per each minute, multiply by 2 if electric"; another says "parking cost =
    total minutes spent parking, multiplied by 2 if car is electric". Yet the
    API accepts ``parkingCost`` and ``chargingCost`` separately, and there is a
    ``Penalty_ChargeCarForNoElectricityUsed`` for billing electricity to a car
    that used none.

    Reading encoded here: an electric car pays ``minutes`` of parking plus
    ``minutes`` of electricity (2x total); anything else pays ``minutes`` with
    ``chargingCost = 0``. Set ``ELECTRIC_SPLIT_CHARGING=false`` to bill the 2x
    entirely as ``parkingCost`` instead.

    VERIFY AGAINST A REAL CAR EARLY -- Penalty_CarChargedIncorrectParkingAmount
    compounds on every vehicle.
    """
    minutes = session.billable_minutes
    billable = max(minutes, 0.0)
    base = round(max(settings.minimum_charge, billable * settings.parking_rate_per_minute), 2)

    if not session.is_electric:
        return base, 0.0, billable

    if settings.electric_split_charging:
        surcharge = round(base * (settings.electric_multiplier - 1.0), 2)
        return base, surcharge, billable

    return round(base * settings.electric_multiplier, 2), 0.0, billable


# --------------------------------------------------------------------------- #
# Lifespan
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    """One-shot sync at startup. The list-* endpoints are documented as costly,
    so this runs here and on POST /resync -- never on a timer."""
    try:
        await client.login()
        spots = await client.list_parking_spots()
        barriers = await client.list_barriers()
        zones = await client.list_zones()
        try:
            fans = await client.list_exhaust_fans()
        except Exception:  # noqa: BLE001 - not every level has exhaust fans
            fans = []

        state.load_spots(spots)
        state.load_barriers(barriers)
        state.load_zones(zones)
        state.load_fans(fans)
        ring.rebuild(list(state.spots.keys()) + list(state.barriers.keys()))

        log.info(
            "startup sync complete: %d spots, %d barriers, %d zones, %d fans",
            len(state.spots), len(state.barriers), len(state.zones), len(state.fans),
        )
    except Exception as exc:  # noqa: BLE001
        # The listener must come up regardless; otherwise we drop events while
        # waiting for the simulator to start.
        log.error("startup sync failed (is the simulator running?): %s", exc)
        log.error("listener is up anyway - call POST /resync once the simulator is available")

        # Offline development: fall back to the level file so the dispatcher
        # has a park to reason about without the simulator running.
        if settings.seed_from_level:
            seeded = load_level(settings.seed_from_level)
            if seeded:
                state.load_spots(seeded["spots"])
                state.load_barriers(seeded["barriers"])
                state.load_zones(seeded["zones"])
                state.load_fans(seeded["fans"])
                ring.rebuild(list(state.spots.keys()) + list(state.barriers.keys()))
                log.warning(
                    "running on SEEDED layout from %s - not live simulator state",
                    settings.seed_from_level,
                )

    # Real driving distances from the C/C++ pathfinder, if it has produced them.
    if load_distance_table():
        log.info("routing: using precomputed driving distances (data/distances.json)")
    else:
        log.warning(
            "routing: no data/distances.json - falling back to the name-ordered "
            "synthetic ring, which is NOT physical distance"
        )

    log.info("autopilot=%s  signature_mode=%s", settings.autopilot, settings.webhook_signature_mode)
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(
    title="KuruSushi-Park Dispatcher",
    version="1.1.0",
    description="Supervisory dispatch layer over the Grand Park Auto simulator.",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Webhook intake
# --------------------------------------------------------------------------- #
@app.post("/webhooks/simulator")
async def simulator_webhook(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"status": "bad_request"}, status_code=400)

    if not isinstance(payload, dict) or "EventClass" not in payload:
        return JSONResponse({"status": "bad_request"}, status_code=400)

    result = verify_signature(payload)

    if result.enforced and result.ok is False:
        log.warning("rejected event with bad signature: %s", payload.get("EventId"))
        return JSONResponse({"status": "invalid_signature"}, status_code=401)

    # Persist first: EventId is the primary key, so redelivery is caught by the
    # database rather than by a bounded in-memory cache that can age out.
    is_new = db.record_event(payload, result.ok)
    event_id = payload.get("EventId")

    if not is_new:
        return JSONResponse({"status": "duplicate", "event_id": event_id})

    # Keep the in-memory dedupe cache aligned for the hot path.
    if event_id:
        state.is_duplicate(event_id)

    gap = state.observe_sequence(payload.get("SequenceId"))
    if gap:
        log.warning(
            "webhook sequence gap: %d missing event(s) before SequenceId=%s",
            gap, payload.get("SequenceId"),
        )
        db.record_sequence_gap(
            expected=int(payload.get("SequenceId", 0)) - gap,
            received=int(payload.get("SequenceId", 0)),
            missing=gap,
        )

    event_class = payload.get("EventClass", "")
    handler = _HANDLERS.get(event_class)
    if handler is None:
        log.info("unhandled EventClass=%s (EventId=%s)", event_class, event_id)
        db.mark_processed(event_id, "unhandled event class")
        return JSONResponse({"status": "ignored", "event_class": event_class})

    try:
        await handler(payload)
        db.mark_processed(event_id)
    except Exception as exc:  # noqa: BLE001 - a bad event must never crash the receiver
        log.exception("handler failed for EventClass=%s EventId=%s", event_class, event_id)
        db.mark_processed(event_id, str(exc))
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
        await _dispatch_arrival(payload, plate, spot_name)
        return

    if spot_type == "Park" and direction == "CarIn":
        state.mark_parked(plate, spot_name)
        log.info("%s parked at %s", plate, spot_name)
        return

    if spot_type == "Park" and direction == "CarOut":
        state.mark_left_spot(plate)
        state.mark_spot_vacant(spot_name)
        ready = state.pop_ready_repair(spot_name)
        if ready:
            await act(f"repair vacated spot {spot_name}", lambda: client.spot_repair(spot_name))
        return

    if spot_type == "ExitSpot" and direction == "CarIn":
        await _charge_at_exit(plate, spot_name)
        return

    if spot_type == "ExitSpot" and direction == "CarOut":
        _archive(plate)
        log.info("%s left the facility via %s", plate, spot_name)
        return


async def _dispatch_arrival(payload: dict[str, Any], plate: str, spot_name: str) -> None:
    car_type = payload.get("CarType", "Normal")
    state.start_session(plate, gate=spot_name, car_type=car_type)

    # Sweep promises made to cars that never turned up, otherwise the lot
    # reports itself full while standing empty.
    freed = state.expire_stale_reservations(settings.reservation_ttl_s)
    if freed:
        log.info("released %d stale reservation(s): %s", len(freed), ", ".join(freed[:5]))

    candidate_type = "Electric" if car_type.strip().lower() == "electric" else "Any"
    candidates = state.available_spots(car_type=candidate_type)
    target = find_best_spot(spot_name, candidates)

    if target is None or not state.reserve_spot(target, plate):
        # Retry once against a refreshed view in case of a race.
        candidates = [c for c in state.available_spots(car_type=candidate_type) if c != target]
        target = find_best_spot(spot_name, candidates)
        if target is None or not state.reserve_spot(target, plate):
            # Lot is full. Sending the car away beats leaving it to trigger
            # Penalty_CarLeftFromEntryBecauseNeglected.
            log.error("no available spot for %s (%s) - releasing from entry", plate, car_type)
            await act(f"car {plate} -> leavepark (lot full)", lambda: client.car_goto(plate, "leavepark"))
            state.complete_session(plate)
            return

    state.assign_spot(plate, target)
    await act(
        f"open {settings.entry_gate} for {plate}",
        lambda: client.barrier_open(settings.entry_gate),
    )
    dispatched = await act(
        f"car {plate} -> {target}", lambda: client.car_goto(plate, target)
    )

    if not dispatched:
        # The command was skipped (dry-run) or failed. Holding the reservation
        # would leak the spot, since the car will never arrive to claim it.
        state.release_reservation(target, plate)
        state.complete_session(plate)
        log.info("would dispatch %s from %s to %s (reservation released)", plate, spot_name, target)
        return

    log.info("dispatched %s from %s to %s", plate, spot_name, target)


async def _charge_at_exit(plate: str, spot_name: str) -> None:
    session = state.get_session(plate)
    if session is None:
        log.warning("%s reached exit with no session on record", plate)
        return

    # Charging the same session twice is its own penalty.
    if session.charged:
        log.info("%s already charged - skipping", plate)
        return

    state.mark_at_exit(plate)
    parking_cost, charging_cost, minutes = compute_charge(session)

    session.expected_parking = parking_cost
    session.expected_charging = charging_cost
    session.expected_amount = round(parking_cost + charging_cost, 2)
    session.exit_gate = spot_name

    log.info(
        "billing %s: %.2f min -> parking=%.2f charging=%.2f (type=%s)",
        plate, minutes, parking_cost, charging_cost, session.car_type,
    )
    await act(
        f"charge {plate} parking={parking_cost} charging={charging_cost}",
        lambda: client.car_charge(plate, parking_cost, charging_cost),
    )
    state.mark_charged(plate)


def _archive(plate: str) -> None:
    """Move a finished session from memory into SQLite."""
    session = state.complete_session(plate)
    if session is None:
        return
    db.record_session(
        {
            "plate": session.plate,
            "car_type": session.car_type,
            "spot": session.assigned_spot,
            "entry_gate": session.entry_gate,
            "exit_gate": session.exit_gate,
            "arrived_at": session.arrived_wall or None,
            "parked_at": session.parked_wall or None,
            "left_spot_at": session.left_spot_wall or None,
            "minutes": session.billable_minutes,
            "parking_cost": session.expected_parking,
            "charging_cost": session.expected_charging,
            "paid_amount": session.expected_amount if session.paid else None,
            "payment_ok": int(session.paid),
        }
    )


async def _handle_component_broken(payload: dict[str, Any]) -> None:
    component_type = payload["Type"]
    name = payload["Name"]
    state.set_component_broken(component_type, name)
    fine = payload.get("FineAmount")
    log.warning("component broken: %s %s (fine %s)", component_type, name, fine)
    db.record_component_event(name, component_type, "broken", _as_float(fine))

    if component_type in ("ParkingSpot", "Park"):
        spot = state.spots.get(name)
        if spot is not None and spot.occupant_plate is not None:
            # Repairing an occupied spot is Penalty_RepairAnOccupiedSpot.
            state.queue_deferred_repair(name, component_type)
            log.info("repair for occupied spot %s deferred until vacated", name)
            return
        await act(f"repair spot {name}", lambda: client.spot_repair(name))
    elif component_type == "BarrierGate":
        await act(f"repair gate {name}", lambda: client.barrier_repair(name))
    elif component_type == "ExhaustFan":
        await act(f"repair fan {name}", lambda: client.fan_repair(name))


async def _handle_component_fixed(payload: dict[str, Any]) -> None:
    component_type = payload["Type"]
    name = payload["Name"]
    state.set_component_fixed(component_type, name)
    log.info("component fixed: %s %s", component_type, name)
    db.record_component_event(name, component_type, "fixed", _as_float(payload.get("RepairCost")))


async def _handle_carbon_monoxide_event(payload: dict[str, Any]) -> None:
    zone_name = payload["ZoneName"]
    co_level = _as_float(payload.get("CarbonMonoxideLevel")) or 0.0
    danger_level = payload.get("DangerLevel", "Safe")
    state.update_zone(zone_name, co_level, danger_level)

    # Docs: fans reduce CO but consume electricity, so keep them off below 50.
    should_run = co_level >= settings.co_fan_on_threshold

    for fan_name in state.fans_in_zone(zone_name):
        fan = state.fans[fan_name]
        if fan.broken or fan.under_maintenance:
            continue
        if should_run and not fan.is_on:
            if await act(f"fan {fan_name} ON (zone {zone_name} CO={co_level:.1f})",
                         lambda n=fan_name: client.fan_on(n)):
                fan.is_on = True
        elif not should_run and fan.is_on:
            if await act(f"fan {fan_name} OFF (zone {zone_name} CO={co_level:.1f})",
                         lambda n=fan_name: client.fan_off(n)):
                fan.is_on = False


async def _handle_gate_action(payload: dict[str, Any]) -> None:
    state.update_barrier_state(payload["Name"], payload.get("Action", ""))


async def _handle_payment_made(payload: dict[str, Any]) -> None:
    """Validate the payment, then release the car.

    The docs warn that "some cars will tweak the system and send fake payment",
    so the reported ``Amount`` is compared against what we actually billed. A
    car is only sent to ``leavepark`` once that check passes -- releasing an
    underpaying car is Penalty_CarEscapedWithoutPaying.
    """
    plate = payload["CarPlateNumber"]
    amount = _as_float(payload.get("Amount")) or 0.0
    session = state.get_session(plate)
    expected = session.expected_amount if session else None

    valid = expected is not None and abs(amount - expected) <= 0.01

    if payload.get("EventId"):
        db.record_payment(
            event_id=payload["EventId"],
            plate=plate,
            amount=amount,
            expected=expected,
            valid=valid,
            reason=payload.get("Reason"),
            server_datetime=payload.get("ServerDateTime"),
        )

    if not valid:
        log.warning(
            "SUSPECT PAYMENT %s: reported %.2f, expected %s - holding at exit",
            plate, amount, "unknown" if expected is None else f"{expected:.2f}",
        )
        return

    if not state.mark_paid(plate, amount):
        log.warning("unsolicited or duplicate payment_made for %s (%.2f) - ignored", plate, amount)
        return

    log.info("payment accepted for %s (%.2f) - releasing", plate, amount)
    await act(f"car {plate} -> leavepark", lambda: client.car_goto(plate, "leavepark"))


async def _handle_penalty(payload: dict[str, Any]) -> None:
    log.error(
        "PENALTY: %s - fine %s (%s %s)",
        payload.get("Reason"), payload.get("FineAmount"),
        payload.get("Type"), payload.get("ComponentName"),
    )
    if payload.get("EventId"):
        db.record_penalty(
            event_id=payload["EventId"],
            reason=payload.get("Reason"),
            fine_amount=_as_float(payload.get("FineAmount")) or 0.0,
            type_=payload.get("Type"),
            component_name=payload.get("ComponentName"),
            server_datetime=payload.get("ServerDateTime"),
        )


async def _handle_test_webhook(payload: dict[str, Any]) -> None:
    log.info("test webhook received: %s", payload.get("EventId"))


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


# --------------------------------------------------------------------------- #
# Operator / dashboard endpoints
# --------------------------------------------------------------------------- #
@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "autopilot": settings.autopilot,
        "signature_mode": settings.webhook_signature_mode,
        "signature_pinned": settings.webhook_signature_recipe or None,
        "spots": len(state.spots),
        "free_spots": len(state.available_spots()),
        "barriers": len(state.barriers),
        "zones": len(state.zones),
        "fans": len(state.fans),
        "active_sessions": len(state.sessions),
        "last_sequence_id": state.last_sequence_id,
        **db.counters(),
    }


@app.get("/events")
async def events(limit: int = 50, event_class: Optional[str] = None) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    if event_class:
        return db.query(
            "SELECT * FROM events WHERE event_class = ? ORDER BY sequence_id DESC LIMIT ?",
            (event_class, limit),
        )
    return db.query("SELECT * FROM events ORDER BY sequence_id DESC LIMIT ?", (limit,))


@app.get("/sessions")
async def sessions(limit: int = 100) -> list[dict[str, Any]]:
    return db.query(
        "SELECT * FROM sessions ORDER BY completed_at DESC LIMIT ?", (max(1, min(limit, 500)),)
    )


@app.get("/cars")
async def cars() -> list[dict[str, Any]]:
    return [
        {
            "plate": s.plate,
            "car_type": s.car_type,
            "phase": s.phase.value,
            "spot": s.assigned_spot,
            "minutes": round(s.billable_minutes, 2),
            "expected_amount": s.expected_amount,
            "charged": s.charged,
            "paid": s.paid,
        }
        for s in state.sessions.values()
    ]


@app.get("/components")
async def components() -> dict[str, Any]:
    return {
        "spots": [
            {
                "name": s.name, "zone": s.zone_parent, "type": s.parking_for_car_type,
                "status": s.status.value, "broken": s.broken,
                "maintenance": s.under_maintenance, "occupant": s.occupant_plate,
            }
            for s in state.spots.values()
        ],
        "barriers": [
            {"name": b.name, "zone": b.zone_parent, "state": b.state.value, "broken": b.broken}
            for b in state.barriers.values()
        ],
        "fans": [
            {"name": f.name, "zone": f.zone_parent, "on": f.is_on, "broken": f.broken}
            for f in state.fans.values()
        ],
        "zones": [
            {"name": z.name, "co": z.gas_co_level, "danger": z.danger_level}
            for z in state.zones.values()
        ],
    }


@app.get("/penalties")
async def penalties() -> list[dict[str, Any]]:
    return db.query("SELECT * FROM penalties ORDER BY server_datetime DESC LIMIT 200")


@app.get("/signature-report")
async def signature_report() -> dict[str, Any]:
    return {"attempts": db.signature_attempts(), "candidates": db.signature_trials()}


@app.post("/resync")
async def resync() -> dict[str, Any]:
    """Manual re-sync after a crash. Documented as costly -- never on a timer."""
    await client.login()
    spots = await client.list_parking_spots()
    barriers = await client.list_barriers()
    zones = await client.list_zones()
    try:
        fans = await client.list_exhaust_fans()
    except Exception:  # noqa: BLE001
        fans = []

    state.load_spots(spots)
    state.load_barriers(barriers)
    state.load_zones(zones)
    state.load_fans(fans)
    ring.rebuild(list(state.spots.keys()) + list(state.barriers.keys()))

    return {
        "ok": True,
        "spots": len(state.spots),
        "barriers": len(state.barriers),
        "zones": len(state.zones),
        "fans": len(state.fans),
    }
