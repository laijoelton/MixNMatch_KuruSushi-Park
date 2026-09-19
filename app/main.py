"""FastAPI dispatcher + dual dashboard: inbound webhooks, outbound REST, live UI.

This service is the sole external controller of the closed
``ParkingSimulator-win-x64`` binary. It never polls; every mutation to local
state happens strictly in reaction to an inbound webhook (see ``app/state.py``
docstring). Two browser-facing surfaces are layered on top of that headless
core without changing its behaviour:

* ``/`` - the signed-in operator console (see ``app/dashboard_api.py``,
  ``app/auth.py``); ``/dashboard`` now redirects there.
* ``/gate`` - the public driver check-in kiosk.

Both are read/observe layers over the same ``ParkingState`` the webhook
handlers mutate, pushed to connected browsers over ``/ws/live``.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import auth, dashboard_api, db
from app.client import client
from app.config import settings
from app.layout import load_layout, running_level
from app.queue_worker import maintenance_queue
from app.routing import load_distance_table, rank_spots, ring
from app.seed import load_level
from app.signature import verify as verify_signature_recipes
from app.state import BarrierPosition, SessionPhase, state
from app.ws_manager import manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dispatcher.main")

ROOT_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT_DIR / "static"
TEMPLATES_DIR = ROOT_DIR / "templates"


async def act(description: str, coro_factory: Callable[[], Awaitable[Any]]) -> bool:
    """Send a command to the simulator, unless AUTOPILOT is off.

    With AUTOPILOT=false every intended action is logged and skipped, so
    dispatch decisions can be reviewed against a live simulator before the
    service is allowed to touch it.
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


def _planned_of(payload: dict[str, Any]) -> float:
    """PlannedParkingDurationInMinutes from an event, as a number.

    Present on both entry and park events. The simulator bills this figure,
    not the wall-clock time we observe.
    """
    try:
        return float(payload.get("PlannedParkingDurationInMinutes") or 0)
    except (TypeError, ValueError):
        return 0.0


def _round_minutes(minutes: float) -> float:
    """Apply the configured billing rounding to a measured duration."""
    if minutes <= 0:
        return 0.0
    mode = settings.billing_rounding
    if mode == "ceil":
        return float(math.ceil(minutes))
    if mode == "exact":
        return round(minutes, 2)
    # "round": nearest whole minute, but never bill zero for a real stay.
    return float(max(1, round(minutes)))


def billing_multiplier(car_type: str) -> float:
    """Vehicle-class multiplier applied on top of the per-minute parking rate.

    Distinct from ``electric_multiplier``, which bills the electricity LINE
    for a car whose ``car_type`` is "Electric" - this multiplies the PARKING
    line for the vehicle's physical class (Sedan/SUV/EV), so an EV pays both
    its class multiplier on parking and the electric surcharge on charging.
    The simulator's documented CarType values seen so far are "Normal" and
    "Electric" only; Sedan/SUV are anticipated but unverified against live
    traffic, so unmatched types fall back to 1.0x rather than guessing.
    """
    key = (car_type or "").strip().lower()
    if key == "sedan":
        return settings.class_multiplier_sedan
    if key == "suv":
        return settings.class_multiplier_suv
    if key == "ev":
        return settings.class_multiplier_ev
    return 1.0


def compute_charge(session) -> tuple[float, float, float]:
    """Return (parking_cost, charging_cost, minutes) for a session.

    The docs contradict themselves: one page says "Charging cost: 1 per each
    minute, multiply by 2 if electric", another says "parking cost = total
    minutes spent parking, multiplied by 2 if car is electric". The API takes
    parkingCost and chargingCost separately, and there is a
    Penalty_ChargeCarForNoElectricityUsed for billing electricity to a car
    that used none.

    Reading encoded here: an electric car pays minutes of parking plus minutes
    of electricity (2x total); anything else pays minutes with chargingCost=0.
    Set ELECTRIC_SPLIT_CHARGING=false to bill the 2x entirely as parking.
    VERIFY against a real car early.

    Billing runs from the moment the car occupied its spot, not from the entry
    sensor -- the drive in is not parking time.
    """
    # The simulator bills the duration the driver BOOKED, not the wall-clock
    # time we observe. Measured against 65 explicit corrections it sent us:
    # planned 3 -> wants 3.00 (26 cases), planned 4 -> wants 4.00 (24 cases).
    #
    # Our own measurement is in real seconds and does not line up: one
    # simulator-minute is roughly 24 real seconds here, so a 3-minute booking
    # looks like 1.2 real minutes and we were billing 1.00 for it.
    if settings.billing_basis == "planned" and session.planned_minutes:
        billable = float(session.planned_minutes)
    else:
        minutes = max(session.billable_minutes, 0.0)
        if session.parked_at is None:
            # Never observed parking and no booking either - estimate rather
            # than bill zero, which the simulator reads as "not charged".
            minutes = settings.unknown_car_minutes
        billable = _round_minutes(minutes)
    base = round(max(settings.minimum_charge, billable * settings.parking_rate_per_minute), 2)
    base = round(base * billing_multiplier(session.car_type), 2)
    if not session.is_electric:
        return base, 0.0, billable
    if settings.electric_split_charging:
        return base, round(base * (settings.electric_multiplier - 1.0), 2), billable
    return round(base * settings.electric_multiplier, 2), 0.0, billable


def verify_signature(payload: dict[str, Any], provided: Optional[str]) -> bool:
    """Delegate to the calibrating verifier in app/signature.py.

    The organizer's documented recipe does not reproduce the signatures
    printed in their own samples, so a fixed algorithm here rejects every real
    event. app/signature.py scores 36 candidate recipes against live traffic
    and, in observe mode, never rejects. Run
    python -m scripts.signature_report to find the winner, then pin it via
    WEBHOOK_SIGNATURE_RECIPE and set WEBHOOK_SIGNATURE_MODE=enforce.
    """
    result = verify_signature_recipes(payload)
    if result.enforced:
        return result.ok is not False
    return True


# The simulator's REST API answers as soon as the process starts, but
# list-parking-spots returns [] until a level is started in its window. A
# dispatcher launched in that gap (START.bat does exactly this) syncs zero
# bays and then treats every arrival as "lot full". A car event proves a
# level is running, so the first one we see without real bays triggers one
# sync - event-driven, not a poll. Seeded layouts don't count as real.
TRAFFIC_SYNC_COOLDOWN_S = 10.0
_live_bays_synced = False
_traffic_sync_lock = asyncio.Lock()
_last_traffic_sync = 0.0


async def _sync_if_no_live_bays() -> None:
    global _last_traffic_sync
    if _live_bays_synced:
        return
    async with _traffic_sync_lock:  # a burst of arrivals triggers one sync, not one each
        if _live_bays_synced or time.monotonic() - _last_traffic_sync < TRAFFIC_SYNC_COOLDOWN_S:
            return
        _last_traffic_sync = time.monotonic()
        try:
            counts = await sync_from_simulator()
            log.warning("traffic arrived with no live bays - synced: %d spots, %d barriers",
                        counts["spots"], counts["barriers"])
        except Exception:  # noqa: BLE001 - the arrival is still handled below
            log.exception("traffic-triggered sync failed")


# A goto sent while the entry barrier is still rising is dropped silently (the
# call still returns 201) and the car sits at the entry until it gives up. We
# wait for the barrier's own "Open" webhook, and if a dispatched car still has
# not left the entry after ENTRY_RETRY_S (normally it leaves in ~1 s) we send
# it once more. Both waits are simulated time, so they scale with game speed.
GATE_OPEN_WAIT_S = 5.0
ENTRY_RETRY_S = 8.0
_left_entry: set[str] = set()   # plates seen driving off their entry spot
_entry_watchers: set[asyncio.Task] = set()


async def _wait_for_barrier_open(name: str) -> bool:
    """True once ``name`` reports Open (or needn't be waited for); False on timeout."""
    barrier = state.barriers.get(name)
    if barrier is None or barrier.broken or barrier.under_maintenance or not settings.autopilot:
        return True
    deadline = time.monotonic() + GATE_OPEN_WAIT_S / max(0.1, settings.game_speed)
    while state.barriers[name].state != BarrierPosition.OPEN:
        if time.monotonic() >= deadline:
            log.warning("barrier %s still %s after waiting - dispatching anyway",
                        name, state.barriers[name].state.value)
            return False
        await asyncio.sleep(0.05)
    return True


async def _resend_if_still_at_entry(plate: str, spot: str) -> None:
    await asyncio.sleep(ENTRY_RETRY_S / max(0.1, settings.game_speed))
    session = state.get_session(plate)
    if (plate in _left_entry or session is None or session.phase != SessionPhase.ASSIGNED
            or session.assigned_spot != spot):
        return
    log.warning("%s never left %s - resending goto %s", plate, session.entry_gate, spot)
    state.log_activity(f"{plate} did not leave {session.entry_gate} - sending it to {spot} again", level="warn")
    await act(f"car {plate} -> {spot} (resend)", lambda: client.car_goto(plate, spot))


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
    global _live_bays_synced
    if any(s.get("purpose") == "Park" for s in spots):
        _live_bays_synced = True
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
                was = barrier.state.value
                await client.barrier_open(barrier.name)
                # "Opening", not "Open": the arm takes time to rise, and a car
                # sent through it meanwhile is silently dropped by the simulator.
                # Its gate_action webhook reports when it is really open.
                state.update_barrier_state(barrier.name, "Opening")
                state.log_activity(f"Auto-opening barrier {barrier.name} (was {was})")
                log.info("auto-opened barrier %s to clear the entry path", barrier.name)
            except Exception:  # noqa: BLE001 - one stuck barrier must not block the others
                log.exception("failed to auto-open barrier %s", barrier.name)


async def _wear_check_loop() -> None:
    """Preventive maintenance: repair a component nearing its wear threshold
    during an idle lull (maintenance queue empty), rather than waiting for it
    to actually break and earn a penalty.
    """
    while True:
        try:
            if len(maintenance_queue) == 0:
                for row in state.wear_snapshot():
                    if row["broken"] or row["under_maintenance"]:
                        continue
                    over_cycles = row["cycle_count"] >= settings.wear_cycle_threshold
                    over_runtime = row["runtime_seconds"] >= settings.wear_runtime_threshold_s
                    if not (over_cycles or over_runtime):
                        continue
                    name, component_type = row["name"], row["type"]
                    if component_type == "Light":
                        # Lights have no repair endpoint / broken state - just
                        # reset the counters so the dashboard stops flagging it.
                        db.mark_component_repaired(name)
                        db.record_component_event(name, component_type, "repair_triggered_proactive")
                        state.log_activity(f"Preventive reset of light {name} wear counters")
                        continue
                    db.set_meta(f"pending_proactive_repair:{name}", "1")
                    db.record_component_event(name, component_type, "repair_triggered_proactive")
                    await _queue_repair(component_type, name)
                    state.log_activity(
                        f"Preventive maintenance: {component_type} {name} queued for repair "
                        f"(cycles={row['cycle_count']}, runtime={row['runtime_seconds']:.0f}s)")
                    db.record_audit_log(None, "system", "preventive_repair",
                                        f"{component_type}:{name} cycles={row['cycle_count']} "
                                        f"runtime={row['runtime_seconds']:.0f}s")
        except Exception:  # noqa: BLE001 - the sweep must never die
            log.exception("wear-check sweep failed")
        await asyncio.sleep(settings.environment_loop_interval_s)


def _latest_server_hour() -> Optional[int]:
    """Hour-of-day from the most recent webhook's ServerDateTime, if any."""
    rows = db.query(
        "SELECT server_datetime FROM events WHERE server_datetime IS NOT NULL "
        "ORDER BY received_at DESC LIMIT 1"
    )
    if not rows or not rows[0]["server_datetime"]:
        return None
    raw = rows[0]["server_datetime"]
    match = re.search(r"[T ](\d{1,2}):", str(raw))
    if not match:
        return None
    try:
        return int(match.group(1)) % 24
    except ValueError:
        return None


_lights_are_day: Optional[bool] = None


async def _environment_loop() -> None:
    """Day/night light control, driven by the simulator's own ServerDateTime.

    There is no dedicated day/night webhook field, so this reads the hour out
    of the most recent event's ServerDateTime (falls back to doing nothing
    until at least one event has arrived). "Light group" is assumed to be the
    zone name, since list-lights exposes no other grouping - VERIFY against a
    live level; if the simulator uses a different group id this silently
    no-ops (the client call 404s and act() logs+swallows it).
    """
    global _lights_are_day
    while True:
        try:
            hour = _latest_server_hour()
            if hour is not None:
                is_day = settings.day_start_hour <= hour < settings.night_start_hour
                if is_day != _lights_are_day:
                    for zone in state.zone_names():
                        lights = state.lights_in_zone(zone) or [zone]
                        if is_day:
                            if await act(f"lights OFF for {zone} (hour={hour}, day)",
                                        lambda z=zone: client.light_group_off(z)):
                                for light_name in lights:
                                    cycles, runtime = state.set_light_on(light_name, False)
                                    db.sync_component_wear(light_name, "Light", cycles, runtime)
                                    db.record_component_event(light_name, "Light", "off")
                        else:
                            if await act(f"lights ON for {zone} (hour={hour}, night)",
                                        lambda z=zone: client.light_group_on(z)):
                                for light_name in lights:
                                    cycles, runtime = state.set_light_on(light_name, True)
                                    db.sync_component_wear(light_name, "Light", cycles, runtime)
                                    db.record_component_event(light_name, "Light", "on")
                    state.log_activity(f"Environmental control: lights set for "
                                       f"{'day' if is_day else 'night'} (hour={hour})")
                    _lights_are_day = is_day
        except Exception:  # noqa: BLE001 - the environment loop must never die
            log.exception("environment loop tick failed")
        await asyncio.sleep(settings.environment_loop_interval_s)


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
    try:
        await client.login()
        counts = await sync_from_simulator()
        log.info("startup sync complete: %d spots, %d barriers, %d zones, %d fans",
                 counts["spots"], counts["barriers"], counts["zones"], counts["fans"])
    except Exception as exc:  # noqa: BLE001
        # The listener must come up regardless, or we drop events while waiting
        # for the simulator to start.
        log.error("startup sync failed (is the simulator running?): %s", exc)
        log.error("listener is up anyway - POST /api/manual/sync once it is available")
        if settings.seed_from_level:
            seeded = load_level(settings.seed_from_level)
            if seeded:
                state.load_spots(seeded["spots"])
                state.load_barriers(seeded["barriers"])
                state.load_zones(seeded["zones"])
                state.load_fans(seeded["fans"])
                ring.rebuild(list(state.spots.keys()) + list(state.barriers.keys()))
                log.warning("running on SEEDED layout from %s - NOT live simulator state",
                            settings.seed_from_level)

    if load_distance_table():
        log.info("routing: using precomputed driving distances (data/distances.json)")
    else:
        log.warning("routing: no data/distances.json - falling back to the name-ordered "
                    "synthetic ring, which is NOT physical distance")

    log.info("autopilot=%s  signature_mode=%s  game_speed=%sx",
             settings.autopilot, settings.webhook_signature_mode, settings.game_speed)

    maintenance_queue.start()
    broadcaster = asyncio.create_task(_broadcast_loop(), name="dispatcher-broadcaster")
    wear_checker = asyncio.create_task(_wear_check_loop(), name="dispatcher-wear-check")
    environment = asyncio.create_task(_environment_loop(), name="dispatcher-environment")
    background_tasks = (broadcaster, wear_checker, environment)
    try:
        yield
    finally:
        for task in background_tasks:
            task.cancel()
        for task in background_tasks:
            try:
                await task
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

# Dashboard: sign-in/roles for every route, plus the console's own pages and APIs.
auth.install(app)
app.include_router(dashboard_api.router)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR)) if TEMPLATES_DIR.exists() else None


# --------------------------------------------------------------------------- #
# Dispatch core (shared by the real webhook path and the manual/dry-run path)
# --------------------------------------------------------------------------- #
async def dispatch_entry(plate: str, gate_name: str, car_type: str = "Normal",
                         dry_run: bool = False, planned_minutes: float = 0.0) -> dict[str, Any]:
    state.start_session(plate, gate=gate_name, car_type=car_type,
                        planned_minutes=planned_minutes)

    # Release promises made to cars that never turned up, otherwise the lot
    # reports itself full while standing empty.
    freed = state.expire_stale_reservations(settings.reservation_ttl_s)
    if freed:
        log.info("released %d stale reservation(s): %s", len(freed), ", ".join(freed[:5]))

    candidate_type = "Electric" if car_type.lower() == "electric" else "Any"
    candidates = state.available_spots(car_type=candidate_type)
    ranked = rank_spots(gate_name, candidates)
    target = ranked[0][0] if ranked else None

    if dry_run:
        return {"plate": plate, "gate": gate_name, "target": target,
                "ranked_candidates": ranked[:10], "dispatched": False}

    if target is None:
        # Level 1: "new cars can leave if park has no free spots". Leaving the
        # car at the entry only earns CarLeftFromEntryBecauseNeglected later.
        # Not when we know no bays at all - that is a sync problem, not a full lot.
        if not _live_bays_synced:
            state.log_activity(f"No bays known yet - {plate} waits at {gate_name}", level="error")
            return {"plate": plate, "gate": gate_name, "target": None, "dispatched": False}
        state.log_activity(f"Lot full - turning {plate} away at {gate_name}", level="warn")
        if await act(f"car {plate} -> leavepark (lot full)", lambda: client.car_goto(plate, "leavepark")):
            state.complete_session(plate)
        return {"plate": plate, "gate": gate_name, "target": None, "dispatched": False, "turned_away": True}

    if not state.reserve_spot(target, plate):
        candidates = [c for c in state.available_spots(car_type=candidate_type) if c != target]
        ranked = rank_spots(gate_name, candidates)
        target = ranked[0][0] if ranked else None
        if target is None or not state.reserve_spot(target, plate):
            state.log_activity(f"Failed to reserve any spot for {plate}", level="error")
            return {"plate": plate, "gate": gate_name, "target": None, "dispatched": False}

    state.assign_spot(plate, target)

    # Only touch the barrier if it is not already open. _ensure_barriers_open()
    # and the gate_action reopen keep it open in normal running, so opening it
    # per car was a wasted HTTP round trip on the entry critical path -- which
    # is exactly where arrivals queue up at higher game speeds. It also burned
    # gate open/close cycles, and gates need repair after a fixed number of
    # them.
    barrier = state.barriers.get(settings.entry_gate)
    if barrier is not None and barrier.state not in (BarrierPosition.OPEN, BarrierPosition.OPENING) \
            and not barrier.broken and not barrier.under_maintenance:
        if await act(f"open {settings.entry_gate} barrier for {plate}",
                     lambda: client.barrier_open(settings.entry_gate)):
            state.update_barrier_state(settings.entry_gate, "Opening")
    await _wait_for_barrier_open(settings.entry_gate)

    _left_entry.discard(plate)  # plates recycle; this is a new visit
    sent = await act(f"car {plate} -> {target}", lambda: client.car_goto(plate, target))
    if sent:
        task = asyncio.create_task(_resend_if_still_at_entry(plate, target))
        _entry_watchers.add(task)  # keep a reference so it isn't garbage-collected mid-wait
        task.add_done_callback(_entry_watchers.discard)

    if not sent:
        # Command skipped (dry-run) or failed. Release the spot - the car will
        # never arrive to claim it, and holding it leaks capacity. Keep the
        # session though: the car really is at the entry, and discarding it
        # would make every later event for this plate look like an unknown car.
        state.release_reservation(target, plate)
        log.info("would dispatch %s from %s to %s (reservation released)", plate, gate_name, target)
        return {"plate": plate, "gate": gate_name, "target": target, "dispatched": False}

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
    sent = await act(f"car {plate} -> {spot_name} (gate portal pick)",
                     lambda: client.car_goto(plate, spot_name))
    if not sent:
        state.release_reservation(spot_name, plate)
        state.complete_session(plate)
        # Distinguish "we chose not to send" from "we tried and it failed" --
        # a driver at the gate portal being told "autopilot disabled" when the
        # simulator is simply unreachable sends them looking in the wrong place.
        reason = ("autopilot disabled - no command sent" if not settings.autopilot
                  else "simulator did not accept the command")
        return {"plate": plate, "gate": gate_name, "target": spot_name, "dispatched": False,
                "reason": reason}
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


@app.get("/dashboard", include_in_schema=False)
async def split_dashboard() -> RedirectResponse:
    """Superseded by the operator console at ``/``; kept so old links still work."""
    return RedirectResponse("/", status_code=302)


# --------------------------------------------------------------------------- #
# Read APIs
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
        "maintenance_queue": maintenance_queue.stats,
        "ws_clients": manager.count,
        **db.counters(),
    }


@app.get("/api/history")
async def get_history(limit: int = 100) -> list[dict[str, Any]]:
    """Completed parking sessions, for the dashboard history table."""
    return db.query("SELECT * FROM sessions ORDER BY completed_at DESC LIMIT ?",
                    (max(1, min(limit, 500)),))


@app.get("/api/events")
async def get_events(limit: int = 50, event_class: Optional[str] = None) -> list[dict[str, Any]]:
    """Raw webhook log, newest first."""
    limit = max(1, min(limit, 500))
    if event_class:
        return db.query(
            "SELECT * FROM events WHERE event_class = ? ORDER BY sequence_id DESC LIMIT ?",
            (event_class, limit))
    return db.query("SELECT * FROM events ORDER BY sequence_id DESC LIMIT ?", (limit,))


@app.get("/api/payments")
async def get_payments(limit: int = 100) -> list[dict[str, Any]]:
    return db.query("SELECT * FROM payments ORDER BY server_datetime DESC LIMIT ?",
                    (max(1, min(limit, 500)),))


@app.get("/api/signature-report")
async def get_signature_report() -> dict[str, Any]:
    """Which signature recipe the live simulator is actually using."""
    return {"attempts": db.signature_attempts(), "candidates": db.signature_trials()}


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


@app.get("/api/layout")
async def get_layout() -> dict[str, Any]:
    """Real simulator pixel-space geometry for the split-dashboard canvas.

    Geometry only, sourced from the level file (see app/layout.py) - never
    status. The caller merges this once against the live /api/state or
    /ws/telemetry feed for occupancy/broken/etc.
    """
    return load_layout(running_level() or "lvl1")


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
async def manual_barrier_open(name: str, user: dict = Depends(auth.require_staff)) -> dict[str, Any]:
    await client.barrier_open(name)
    state.update_barrier_state(name, "Open")
    cycles = state.record_barrier_cycle(name)
    db.upsert_component_wear(name, "BarrierGate", cycle_delta=0)  # ensure row exists
    db.sync_component_wear(name, "BarrierGate", cycles, 0.0)
    state.log_activity(f"Operator opened barrier {name}")
    db.record_audit_log(user["username"], user["role"], "barrier_open", name)
    return {"ok": True, "name": name, "state": "Open"}


@app.post("/api/manual/barrier/{name}/close")
async def manual_barrier_close(name: str, user: dict = Depends(auth.require_staff)) -> dict[str, Any]:
    await client.barrier_close(name)
    state.update_barrier_state(name, "Closed")
    cycles = state.record_barrier_cycle(name)
    db.sync_component_wear(name, "BarrierGate", cycles, 0.0)
    state.log_activity(f"Operator closed barrier {name}")
    db.record_audit_log(user["username"], user["role"], "barrier_close", name)
    return {"ok": True, "name": name, "state": "Closed"}


@app.post("/api/manual/repair/{name}")
async def manual_repair(name: str, user: dict = Depends(auth.require_role("maintenance"))) -> dict[str, Any]:
    component_type = (
        "ParkingSpot" if name in state.spots
        else "BarrierGate" if name in state.barriers
        else "ExhaustFan" if name in state.fans
        else "Light" if name in state.lights
        else None
    )
    if component_type is None:
        raise HTTPException(404, f"unknown component {name}")
    if component_type == "Light":
        db.mark_component_repaired(name)  # lights have no broken/fixed webhook - reset immediately
    else:
        await _queue_repair(component_type, name)  # counters reset when component_fixed arrives
    db.record_audit_log(user["username"], user["role"], "manual_repair", f"{component_type}:{name}")
    return {"ok": True, "name": name, "type": component_type, "queued": True}


@app.post("/api/manual/fan/{name}/on")
async def manual_fan_on(name: str, user: dict = Depends(auth.require_role("maintenance"))) -> dict[str, Any]:
    if name not in state.fans:
        raise HTTPException(404, f"unknown fan {name}")
    await client.fan_on(name)
    cycles, runtime = state.set_fan_on(name, True)
    db.sync_component_wear(name, "ExhaustFan", cycles, runtime)
    state.log_activity(f"Operator switched on fan {name}")
    db.record_audit_log(user["username"], user["role"], "fan_on", name)
    return {"ok": True, "name": name, "is_on": True}


@app.post("/api/manual/fan/{name}/off")
async def manual_fan_off(name: str, user: dict = Depends(auth.require_role("maintenance"))) -> dict[str, Any]:
    if name not in state.fans:
        raise HTTPException(404, f"unknown fan {name}")
    await client.fan_off(name)
    cycles, runtime = state.set_fan_on(name, False)
    db.sync_component_wear(name, "ExhaustFan", cycles, runtime)
    state.log_activity(f"Operator switched off fan {name}")
    db.record_audit_log(user["username"], user["role"], "fan_off", name)
    return {"ok": True, "name": name, "is_on": False}


@app.post("/api/manual/light/{name}/on")
async def manual_light_on(name: str, user: dict = Depends(auth.require_staff)) -> dict[str, Any]:
    await client.light_on(name)
    cycles, runtime = state.set_light_on(name, True)
    db.sync_component_wear(name, "Light", cycles, runtime)
    state.log_activity(f"Operator switched on light {name}")
    db.record_audit_log(user["username"], user["role"], "light_on", name)
    return {"ok": True, "name": name, "is_on": True}


@app.post("/api/manual/light/{name}/off")
async def manual_light_off(name: str, user: dict = Depends(auth.require_staff)) -> dict[str, Any]:
    await client.light_off(name)
    cycles, runtime = state.set_light_on(name, False)
    db.sync_component_wear(name, "Light", cycles, runtime)
    state.log_activity(f"Operator switched off light {name}")
    db.record_audit_log(user["username"], user["role"], "light_off", name)
    return {"ok": True, "name": name, "is_on": False}


@app.post("/api/ghost-cars/{ghost_id}/override")
async def ghost_car_override(ghost_id: int,
                              user: dict = Depends(auth.require_role("operator"))) -> dict[str, Any]:
    """Authenticated operator override: resolves the ghost-car alert, charges
    the recorded fallback amount, and only then releases the car."""
    row = db.resolve_ghost_car(ghost_id, user["username"])
    if row is None:
        raise HTTPException(404, "no such open ghost-car event, or it is already resolved")

    plate = row["plate"]
    fallback = float(row["fallback_charge"] or 0.0)
    session = state.get_session(plate)
    if session is None:
        session = state.start_session(plate, gate="(ghost-override)")
    session.expected_parking = fallback
    session.expected_charging = 0.0
    session.expected_amount = round(fallback, 2)
    state.mark_charged(plate)

    await act(f"charge {plate} parking={fallback} (ghost-car override by {user['username']})",
              lambda: client.car_charge(plate, fallback, 0.0))
    state.mark_paid(plate, session.expected_amount)
    await act(f"car {plate} -> leavepark (ghost-car override)", lambda: client.car_goto(plate, "leavepark"))

    state.log_activity(f"Ghost car {plate} released by operator override ({user['username']})", level="warn")
    db.record_audit_log(user["username"], user["role"], "ghost_car_override",
                        f"ghost_id={ghost_id} plate={plate} charge={fallback}")
    return {"ok": True, "ghost_id": ghost_id, "plate": plate, "charged": fallback}


@app.get("/api/ghost-cars")
async def list_ghost_cars(resolved: Optional[bool] = None,
                          user: dict = Depends(auth.require_staff)) -> list[dict[str, Any]]:
    if resolved is None:
        return db.query("SELECT * FROM ghost_car_events ORDER BY occurred_at DESC LIMIT 200")
    return db.query("SELECT * FROM ghost_car_events WHERE resolved = ? ORDER BY occurred_at DESC LIMIT 200",
                    (int(resolved),))


# --------------------------------------------------------------------------- #
# Penalties (staff)
# --------------------------------------------------------------------------- #
@app.get("/api/penalties")
async def get_penalties(limit: int = 200, code: Optional[str] = None,
                        user: dict = Depends(auth.require_staff)) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    if code:
        return db.query(
            "SELECT * FROM penalties WHERE reason LIKE ? ORDER BY server_datetime DESC LIMIT ?",
            (f"%{code}%", limit))
    return db.query("SELECT * FROM penalties ORDER BY server_datetime DESC LIMIT ?", (limit,))


# --------------------------------------------------------------------------- #
# Level 2 dynamic reporting
# --------------------------------------------------------------------------- #
@app.get("/api/reports/daily")
async def daily_report(user: dict = Depends(auth.require_staff)) -> dict[str, Any]:
    """Aggregated operational + (role-gated) financial metrics.

    Operational metrics are visible to any signed-in staff member; the
    revenue section is only computed for financial_auditor/admin.
    """
    # component_wear/spots have no zone column of their own to join against in
    # SQL, so throughput is reported per spot (the dashboard groups by zone
    # client-side using the same /api/layout geometry it already has).
    throughput_by_zone = db.query(
        """SELECT COALESCE(spot, 'unknown') AS spot, COUNT(*) AS sessions
           FROM sessions GROUP BY spot ORDER BY sessions DESC LIMIT 50"""
    )

    light_off_events = db.query(
        "SELECT COUNT(*) AS n FROM component_events WHERE type = 'Light' AND event = 'off'"
    )[0]["n"]
    energy_conserved_wh_estimate = round(
        light_off_events * settings.light_watts_estimate * (settings.environment_loop_interval_s / 3600.0), 2
    )

    co_mitigation_events = db.query(
        "SELECT COUNT(*) AS n FROM component_events WHERE type = 'ExhaustFan' "
    )[0]["n"]

    preventive = db.query(
        "SELECT COUNT(*) AS n FROM component_events WHERE event = 'repair_triggered_proactive'"
    )[0]["n"]
    reactive = db.query(
        "SELECT COUNT(*) AS n FROM component_events WHERE event = 'broken'"
    )[0]["n"]

    out: dict[str, Any] = {
        "throughput_by_spot": throughput_by_zone,
        "energy_conserved_wh_estimate": energy_conserved_wh_estimate,
        "energy_conserved_note": "Estimate: light-off actions x assumed wattage x tick interval - not a real meter.",
        "co_mitigation_events": co_mitigation_events,
        "preventive_repairs": preventive,
        "unexpected_breakdowns": reactive,
        "ghost_car_events_open": db.query(
            "SELECT COUNT(*) AS n FROM ghost_car_events WHERE resolved = 0")[0]["n"],
    }

    if user["role"] in ("financial_auditor", "admin"):
        paid = db.query("SELECT COALESCE(SUM(paid_amount), 0) AS r FROM sessions WHERE payment_ok = 1")[0]["r"]
        fines = db.query("SELECT COALESCE(SUM(fine_amount), 0) AS f FROM penalties")[0]["f"]
        out["revenue"] = {
            "paid_total": round(paid, 2),
            "penalty_total": round(fines, 2),
            "net_revenue": round(paid - fines, 2),
        }

    return out


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


class DispatchIn(BaseModel):
    car_plate: str = Field(..., min_length=1, max_length=16)
    target_spot_id: str = Field(..., min_length=1, max_length=32)
    gate: Optional[str] = Field(None, max_length=32)
    car_type: str = Field("Normal", max_length=16)


@app.post("/api/dispatch")
async def api_dispatch(body: DispatchIn) -> dict[str, Any]:
    """Split-dashboard entry point: driver picks a bay in the phone GPS view.

    Thin wrapper over the same reserve-then-goto path /api/gate/checkin uses
    (``assign_specific_spot``), so it is idempotency- and AUTOPILOT-safe.
    """
    known_gates = state.entry_gates()
    gate = body.gate or (known_gates[0] if known_gates else settings.entry_gate)
    if body.target_spot_id not in state.spots:
        raise HTTPException(404, f"unknown spot {body.target_spot_id}")
    result = await assign_specific_spot(body.car_plate, gate, body.target_spot_id, body.car_type)
    if not result["dispatched"]:
        raise HTTPException(409, result.get("reason", "spot unavailable"))
    return result


# --------------------------------------------------------------------------- #
# Live WebSocket feed (operator HUD + gate picker + split dashboard)
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


@app.websocket("/ws/telemetry")
async def ws_telemetry(ws: WebSocket):
    """Identical feed to /ws/live, named for the split dashboard's operator
    canvas + phone GPS view so both stay trivially in sync with each other
    and with the main HUD - they all share one ConnectionManager broadcast."""
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
        reason = "no recipe matched the provided signature" if signature else "signature missing"
        db.record_unsigned_webhook(payload.get("EventId"), reason, payload)
        log.warning("webhook rejected (enforce mode): %s (EventId=%s)", reason, payload.get("EventId"))
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    # Persist first: EventId is the table's primary key, so a redelivery is
    # rejected by the database rather than by a bounded in-memory cache that
    # can age out. A crash mid-handler cannot lose the event.
    sig = verify_signature_recipes(payload)
    is_new = db.record_event(payload, sig.ok)
    event_id = payload.get("EventId")
    if not is_new:
        return JSONResponse({"status": "duplicate", "event_id": event_id})

    if event_id:
        state.is_duplicate(event_id)  # keep the hot-path cache aligned

    sequence_id = payload.get("SequenceId")
    gap = state.observe_sequence(sequence_id)
    if gap:
        log.warning("webhook sequence gap detected: %d missing event(s) before SequenceId=%s", gap, sequence_id)
        try:
            db.record_sequence_gap(int(sequence_id) - gap, int(sequence_id), gap)
        except (TypeError, ValueError):
            pass

    if settings.webhook_debug:
        log.info("RAW WEBHOOK PAYLOAD: %s", payload)

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

    await _sync_if_no_live_bays()

    if spot_type == "EntrySpot" and direction == "CarOut":
        _left_entry.add(plate)  # it took the goto - no resend needed
        return

    if spot_type == "EntrySpot" and direction == "CarIn":
        car_type = payload.get("CarType", "Normal")
        await dispatch_entry(plate, spot_name, car_type,
                             planned_minutes=_planned_of(payload))
        return

    if spot_type == "Park" and direction == "CarIn":
        if state.get_session(plate) is None:
            # Not dispatched by us -- a car already in the lot when we started,
            # or one that survived a restart. Adopt it so it still gets billed.
            state.start_session(plate, gate="(adopted)",
                                car_type=payload.get("CarType", "Normal"),
                                planned_minutes=_planned_of(payload))
            log.info("adopted untracked car %s parking at %s", plate, spot_name)
        state.set_planned_minutes(plate, _planned_of(payload))
        state.mark_parked(plate, spot_name)
        state.log_activity(f"{plate} parked at {spot_name}")
        return

    if spot_type == "Park" and direction == "CarOut":
        state.mark_left_spot(plate)
        state.mark_spot_vacant(spot_name)
        state.log_activity(f"{plate} vacated {spot_name}")
        ready = state.pop_ready_repair(spot_name)
        if ready:
            await _queue_repair(ready, spot_name)
            log.info("deferred repair for %s now queued after vacancy", spot_name)
        return

    if spot_type == "ExitSpot" and direction == "CarIn":
        session = state.get_session(plate)
        if session is None or session.parked_at is None:
            # Ghost car: arrived at the exit barrier with no matching entry
            # session (no VehicleSession, or one with no recorded parked_at -
            # e.g. it was already in the lot when we started and we never saw
            # it park). Unlike a car we tracked but measured imprecisely, we
            # have literally no record of it ever entering - do not estimate
            # and quietly charge it. Hold it at the barrier and raise an alert
            # for a human instead; see _handle_ghost_car.
            await _handle_ghost_car(plate, spot_name, payload)
            return
        if session.charged:
            return
        state.mark_at_exit(plate)
        session.exit_gate = spot_name
        # Charging inline here is too early - see _charge_at_exit, which logs
        # the amount once it has actually computed and sent it.
        state.mark_charged(plate)
        asyncio.create_task(_charge_at_exit(plate, spot_name))
        return

    if spot_type == "ExitSpot" and direction == "CarOut":
        _archive(plate)
        state.log_activity(f"{plate} left the facility via {spot_name}")
        log.info("%s left the facility via %s", plate, spot_name)
        return


def _archive(plate: str) -> None:
    """Move a finished session out of memory and into SQLite for the dashboard."""
    session = state.complete_session(plate)
    if session is None:
        return
    db.record_session({
        "plate": session.plate,
        "car_type": session.car_type,
        "spot": session.assigned_spot,
        "entry_gate": session.entry_gate,
        "exit_gate": session.exit_gate,
        "arrived_at": session.arrived_wall or None,
        "parked_at": session.parked_wall or None,
        "left_spot_at": session.left_spot_wall or None,
        "minutes": session.billable_minutes,
        "planned_minutes": session.planned_minutes,
        "parking_cost": session.expected_parking,
        "charging_cost": session.expected_charging,
        "paid_amount": session.expected_amount if session.paid else None,
        "payment_ok": int(session.paid),
    })


async def _charge_at_exit(plate: str, spot_name: str) -> None:
    """Charge a car once it is actually waiting at the exit, and keep trying.

    The ExitSpot CarIn sensor fires when the car ENTERS the exit area, not when
    it has settled and is waiting for an invoice. Charging on the event itself
    is rejected by the simulator with "Car (X) is not waiting at the exit" --
    but the REST call still returns 201, so the failure is invisible from our
    side. The car then waits ~5 minutes for an invoice that never arrives and
    escapes, costing both CarShouldBeChargedAtExit and CarEscapedWithoutPaying.

    So: let the car settle, charge, then watch for payment_made and charge
    again if nothing comes. The 5-minute driver patience gives us room for
    several attempts.
    """
    # Scale by the simulator's clock. Our waits are wall-clock but the driver's
    # five minutes of patience is simulated time, so at 5x speed that budget is
    # gone in 60 real seconds while an unscaled 2s delay stays 2s -- thirty
    # times more of the window than at 1x.
    speed = max(0.1, settings.game_speed)
    settle = settings.exit_charge_delay_s / speed
    payment_window = settings.payment_wait_s / speed

    await asyncio.sleep(settle)

    for attempt in range(1, max(1, settings.charge_max_attempts) + 1):
        session = state.get_session(plate)
        if session is None:
            return  # archived - the car already left
        if session.paid:
            return

        parking_cost, charging_cost, minutes = compute_charge(session)
        session.expected_parking = parking_cost
        session.expected_charging = charging_cost
        session.expected_amount = round(parking_cost + charging_cost, 2)

        suffix = "" if attempt == 1 else f" (attempt {attempt})"
        basis = ("planned" if settings.billing_basis == "planned" and session.planned_minutes
                 else "measured")
        log.info("billing %s: %.0f min (%s) -> parking=%.2f charging=%.2f (type=%s)%s",
                 plate, minutes, basis, parking_cost, charging_cost, session.car_type, suffix)
        await act(f"charge {plate} parking={parking_cost} charging={charging_cost}{suffix}",
                  lambda: client.car_charge(plate, parking_cost, charging_cost))
        state.log_activity(f"Charged {plate} {session.expected_amount:.2f} at {spot_name}{suffix}")

        # Poll for payment rather than sleeping the whole window, so a car that
        # pays quickly is released quickly. A correction from the simulator
        # (see _handle_penalty) also lands here as a changed expected_amount.
        waited = 0.0
        tick = min(1.0, max(0.1, 1.0 / speed))
        charged_amount = session.expected_amount
        while waited < payment_window:
            await asyncio.sleep(tick)
            waited += tick
            current = state.get_session(plate)
            if current is None or current.paid:
                return
            if current.expected_amount != charged_amount:
                # The simulator corrected us and _handle_penalty already
                # re-sent the right figure. Resume waiting on that, rather
                # than resending the amount it just rejected.
                charged_amount = current.expected_amount
                waited = 0.0

        log.warning("%s has not paid %.2f after %.0fs - recharging",
                    plate, session.expected_amount, payment_window)

    log.error("%s never paid after %d attempts - it will likely escape",
              plate, settings.charge_max_attempts)
    state.log_activity(f"{plate} never paid after {settings.charge_max_attempts} attempts",
                       level="error")


async def _handle_ghost_car(plate: str, exit_spot: str, payload: dict[str, Any]) -> None:
    """A car with zero registration reached an exit barrier.

    Charging silently on our usual estimate (unknown_car_minutes) is what the
    dispatcher already does for a car it tracked but lost precise timing for.
    This is a stricter case - there is no session at all, so the exit barrier
    is left alone (we never call barrier_open for it here) and the car waits
    for an authenticated operator to review and release it via
    POST /api/ghost-cars/{id}/override.
    """
    fallback = db.median_parking_cost()
    if fallback is None:
        fallback = round(settings.unknown_car_minutes * settings.parking_rate_per_minute, 2)
    ghost_id = db.record_ghost_car(plate, exit_spot, fallback)
    state.log_activity(
        f"UNREGISTERED_VEHICLE_EXIT: {plate} at {exit_spot} - held for operator review "
        f"(fallback charge {fallback:.2f})", level="error")
    log.error("UNREGISTERED_VEHICLE_EXIT: %s at %s has no prior entry session - holding at barrier "
              "(ghost_id=%d, fallback=%.2f)", plate, exit_spot, ghost_id, fallback)
    try:
        await manager.broadcast({
            "type": "alert", "alert_type": "UNREGISTERED_VEHICLE_EXIT",
            "plate": plate, "gate": exit_spot, "ghost_id": ghost_id,
            "fallback_charge": fallback, "server_time": time.time(),
        })
    except Exception:  # noqa: BLE001 - a broadcast failure must not lose the alert
        log.exception("failed to broadcast ghost-car alert for %s", plate)


async def _handle_component_broken(payload: dict[str, Any]) -> None:
    component_type = payload["Type"]
    name = payload["Name"]
    state.set_component_broken(component_type, name)
    db.set_meta(f"pending_proactive_repair:{name}", "0")  # it broke on its own - reactive
    try:
        fine_amount = float(payload.get("FineAmount") or 0.0)
    except (TypeError, ValueError):
        fine_amount = None
    db.record_component_event(name, component_type, "broken", amount=fine_amount)
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
    db.mark_component_repaired(name)
    was_proactive = db.get_meta(f"pending_proactive_repair:{name}") == "1"
    db.set_meta(f"pending_proactive_repair:{name}", "0")
    db.record_component_event(name, component_type,
                              "fixed_proactive" if was_proactive else "fixed_reactive")
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

    # Hysteresis: switch ON at/above co_fan_on_threshold, but only switch OFF
    # once the level drops below the lower co_fan_off_threshold. A single
    # shared threshold made the fan flap on/off on every reading that
    # hovered around it - two distinct edges stop that.
    for fan_name in state.fans_in_zone(zone_name):
        fan = state.fans[fan_name]
        if fan.broken or fan.under_maintenance:
            continue
        if not fan.is_on and co_level >= settings.co_fan_on_threshold:
            if await act(f"fan {fan_name} ON (zone {zone_name} CO={co_level:.1f})",
                         lambda n=fan_name: client.fan_on(n)):
                cycles, runtime = state.set_fan_on(fan_name, True)
                db.sync_component_wear(fan_name, "ExhaustFan", cycles, runtime)
                db.record_component_event(fan_name, "ExhaustFan", "on", amount=co_level)
                state.log_activity(f"Exhaust fan {fan_name} switched on for {zone_name}")
        elif fan.is_on and co_level < settings.co_fan_off_threshold:
            if await act(f"fan {fan_name} OFF (zone {zone_name} CO={co_level:.1f})",
                         lambda n=fan_name: client.fan_off(n)):
                cycles, runtime = state.set_fan_on(fan_name, False)
                db.sync_component_wear(fan_name, "ExhaustFan", cycles, runtime)
                db.record_component_event(fan_name, "ExhaustFan", "off", amount=co_level)
                state.log_activity(f"Exhaust fan {fan_name} switched off for {zone_name}")


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


# "Car is being charged wrongly with amount: (2.00). Car type is (Normal) so
# charge should be: (4.00)"  -- the simulator hands us the correct figure.
_WRONG_CHARGE_RE = re.compile(
    r"charged wrongly with amount:\s*\(?([\d.]+)\)?.*?"
    r"should be:\s*\(?([\d.]+)\)?",
    re.IGNORECASE | re.DOTALL,
)


def _find_session_by_loose_plate(raw: str):
    """Match a plate ignoring spacing.

    Penalty payloads name the car as "CRL592" while every other event uses
    "CRL 592", so an exact dictionary lookup misses.
    """
    if not raw:
        return None, None
    squashed = raw.replace(" ", "").upper().removeprefix("CAR")
    for plate, session in list(state.sessions.items()):
        if plate.replace(" ", "").upper() == squashed:
            return plate, session
    return None, None


async def _apply_charge_correction(payload: dict[str, Any], sent: float, should_be: float) -> None:
    """Re-charge a car with the amount the simulator says it actually owes.

    Our own measurement of parked time disagrees with the simulator's for some
    cars, and we cannot see why from this side -- identical measured durations
    are sometimes accepted and sometimes rejected. Rather than keep resending a
    figure that was just refused (which earns another penalty every retry), we
    take the corrected amount from the penalty itself and charge that.

    The car is still waiting at the exit, so this converts a repeating penalty
    into a single one followed by a successful payment.
    """
    plate, session = _find_session_by_loose_plate(payload.get("ComponentName", ""))
    if session is None:
        log.warning("charge correction for %s but no live session", payload.get("ComponentName"))
        return

    delta = should_be - sent
    log.warning("charge correction for %s: sent %.2f, simulator wants %.2f (delta %+.2f)",
                plate, sent, should_be, delta)

    # Electricity is only ever billed to an electric car; put the correction on
    # the parking line so we do not invent a charge for unused electricity.
    session.expected_parking = round(should_be - (session.expected_charging or 0.0), 2)
    session.expected_amount = round(should_be, 2)

    await act(f"re-charge {plate} parking={session.expected_parking} "
              f"charging={session.expected_charging or 0.0} (simulator correction)",
              lambda: client.car_charge(plate, session.expected_parking,
                                        session.expected_charging or 0.0))
    state.log_activity(f"Corrected charge for {plate} to {should_be:.2f}", level="warn")


async def _handle_payment_made(payload: dict[str, Any]) -> None:
    """Validate the payment, then release the car.

    The docs warn that "some cars will tweak the system and send fake
    payment", so the reported Amount is checked against what we actually
    billed. The car is only sent to leavepark once that passes - releasing an
    underpaying car is Penalty_CarEscapedWithoutPaying.
    """
    plate = payload["CarPlateNumber"]
    amount = float(payload.get("Amount", 0.0))
    session = state.get_session(plate)
    expected = session.expected_amount if session is not None else None
    valid = expected is not None and abs(amount - expected) <= 0.01

    if payload.get("EventId"):
        db.record_payment(
            event_id=payload["EventId"], plate=plate, amount=amount, expected=expected,
            valid=valid, reason=payload.get("Reason"),
            server_datetime=payload.get("ServerDateTime"),
        )

    if not valid:
        shown = "unknown" if expected is None else f"{expected:.2f}"
        state.log_activity(f"SUSPECT PAYMENT {plate}: expected {shown}, got {amount:.2f}", level="warn")
        log.warning("SUSPECT PAYMENT %s: reported %.2f, expected %s - holding at exit",
                    plate, amount, shown)
        return

    if not state.mark_paid(plate, amount):
        log.warning("unsolicited or duplicate payment_made for %s (amount %.2f) - ignored", plate, amount)
        return

    state.log_activity(f"Payment accepted for {plate} ({amount:.2f}) - releasing")
    log.info("payment accepted for %s (%.2f) - releasing", plate, amount)
    await act(f"car {plate} -> leavepark", lambda: client.car_goto(plate, "leavepark"))


async def _handle_penalty(payload: dict[str, Any]) -> None:
    reason = payload.get("Reason", "")
    fine = float(payload.get("FineAmount", 0.0))
    component_type = payload.get("Type", "")
    component_name = payload.get("ComponentName", "")
    state.record_penalty(reason, fine, component_type, component_name)
    if payload.get("EventId"):
        db.record_penalty(
            event_id=payload["EventId"], reason=reason, fine_amount=fine,
            type_=component_type, component_name=component_name,
            server_datetime=payload.get("ServerDateTime"),
        )
    state.log_activity(f"PENALTY: {reason} (-{fine})", level="error")
    log.error("PENALTY: %s - fine %s (%s %s)", reason, fine, component_type, component_name)

    # A wrong-amount penalty carries the correct figure. Use it rather than
    # letting the retry loop resend the amount that was just refused.
    match = _WRONG_CHARGE_RE.search(reason or "")
    if match:
        try:
            sent, should_be = float(match.group(1)), float(match.group(2))
        except ValueError:
            return
        db.set_meta("last_charge_delta", round(should_be - sent, 2))
        await _apply_charge_correction(payload, sent, should_be)


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
