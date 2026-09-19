"""FastAPI dispatcher + dual dashboard: inbound webhooks, outbound REST, live UI.

This service is the sole external controller of the closed
``ParkingSimulator-win-x64`` binary. It never polls; every mutation to local
state happens strictly in reaction to an inbound webhook (see ``app/state.py``
docstring). Two browser-facing surfaces are layered on top of that headless
core without changing its behaviour:

* ``/`` - an operator HUD (live telemetry, digital twin, manual controls).
* ``/gate`` - a mobile-first cinema-style bay picker for walk-in check-in.
* ``/dashboard`` - split-screen operator canvas (real simulator geometry,
  animated dispatch paths) next to a phone-frame driver GPS mockup.

All three are pure read/observe layers over the same ``ParkingState`` the
webhook handlers mutate, pushed to connected browsers over ``/ws/live`` (and,
for the split dashboard, the identical feed mirrored at ``/ws/telemetry``).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import auth, db
from app.auth import SESSION_COOKIE, ROLE_ADMIN, SessionInfo
from app.client import client
from app.config import settings
from app.layout import load_layout
from app.queue_worker import maintenance_queue
from app.routing import load_distance_table, rank_spots, ring
from app.seed import load_level
from app.signature import verify as verify_signature_recipes
from app.state import BarrierPosition, state
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
    minutes = max(session.billable_minutes, 0.0)
    if session.parked_at is None:
        # Never observed parking, so there is nothing to measure. Estimate
        # rather than bill zero, which the simulator reads as "not charged".
        minutes = settings.unknown_car_minutes

    # Turn the measured duration into a billable figure. Live evidence: a
    # fractional charge is never paid and the car escapes, so the simulator
    # wants whole minutes. It also draws planned durations as whole minutes,
    # so a measured 1.02 is a 1-minute stay - rounding to nearest, not up.
    billable = _round_minutes(minutes)
    base = round(max(settings.minimum_charge, billable * settings.parking_rate_per_minute), 2)
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

    log.info("autopilot=%s  signature_mode=%s", settings.autopilot, settings.webhook_signature_mode)

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
    state.start_session(plate, gate=gate_name, car_type=car_type)

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
        # Lot (or this car's zone/type) is genuinely full. Leaving the car
        # idling at the entry earns Penalty_CarLeftFromEntryBecauseNeglected
        # once the driver gives up - send it straight back out instead.
        state.log_activity(f"No available spot for {plate} at {gate_name} - lot full, sending to leavepark",
                           level="error")
        await act(f"car {plate} -> leavepark (lot full)", lambda: client.car_goto(plate, "leavepark"))
        state.complete_session(plate)
        return {"plate": plate, "gate": gate_name, "target": None, "dispatched": False, "reason": "lot_full"}

    if not state.reserve_spot(target, plate):
        candidates = [c for c in state.available_spots(car_type=candidate_type) if c != target]
        ranked = rank_spots(gate_name, candidates)
        target = ranked[0][0] if ranked else None
        if target is None or not state.reserve_spot(target, plate):
            state.log_activity(f"Failed to reserve any spot for {plate} - sending to leavepark", level="error")
            await act(f"car {plate} -> leavepark (no reservable spot)", lambda: client.car_goto(plate, "leavepark"))
            state.complete_session(plate)
            return {"plate": plate, "gate": gate_name, "target": None, "dispatched": False, "reason": "lot_full"}

    state.assign_spot(plate, target)
    await act(f"open {gate_name} barrier for {plate}",
              lambda: client.barrier_open(settings.entry_gate))
    sent = await act(f"car {plate} -> {target}", lambda: client.car_goto(plate, target))

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
        return {"plate": plate, "gate": gate_name, "target": spot_name, "dispatched": False,
                "reason": "autopilot disabled"}
    state.log_activity(f"Check-in: {plate} chose {spot_name} at {gate_name}")
    return {"plate": plate, "gate": gate_name, "target": spot_name, "dispatched": True}


# --------------------------------------------------------------------------- #
# Auth pages
# --------------------------------------------------------------------------- #
# Only the staff surfaces below (operator/admin dashboards, manual control
# endpoints) sit behind login. The public driver portal (/gate) and its APIs
# are deliberately left open - a walk-in driver has no staff account.
def _require_page_user(request: Request) -> SessionInfo:
    """Like auth.require_staff, but for HTML pages: redirect to /login
    instead of a bare 401 JSON body a browser tab can't do anything with."""
    info = auth.sessions.get(request.cookies.get(SESSION_COOKIE))
    if info is None:
        raise _LoginRedirect(str(request.url.path))
    return info


class _LoginRedirect(Exception):
    def __init__(self, next_path: str) -> None:
        self.next_path = next_path


@app.exception_handler(_LoginRedirect)
async def _login_redirect_handler(request: Request, exc: _LoginRedirect) -> RedirectResponse:
    return RedirectResponse(f"/login?next={exc.next_path}", status_code=303)


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, next: str = "/", error: Optional[str] = None):
    if templates is None:
        raise HTTPException(500, "templates directory missing")
    existing = auth.sessions.get(request.cookies.get(SESSION_COOKIE))
    if existing is not None:
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "next": next, "error": error, "asset_version": str(int(time.time())),
    })


@app.post("/login", include_in_schema=False)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...),
                       next: str = Form("/")):
    role = auth.authenticate(username.strip(), password)
    if role is None:
        return RedirectResponse(f"/login?next={next}&error=1", status_code=303)
    session = auth.sessions.create(username.strip(), role)
    state.log_activity(f"{username} ({role}) logged in")
    response = RedirectResponse(next or "/", status_code=303)
    response.set_cookie(SESSION_COOKIE, session.token, httponly=True, samesite="lax",
                        max_age=int(settings.session_ttl_s))
    return response


@app.post("/logout", include_in_schema=False)
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    info = auth.sessions.get(token)
    auth.sessions.destroy(token)
    if info is not None:
        state.log_activity(f"{info.username} ({info.role}) logged out")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/api/me")
async def api_me(user: SessionInfo = Depends(auth.require_staff)) -> dict[str, Any]:
    return {"username": user.username, "role": user.role}


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def operator_dashboard(request: Request, user: SessionInfo = Depends(_require_page_user)):
    if templates is None:
        raise HTTPException(500, "templates directory missing")
    return templates.TemplateResponse(request, "index.html", {
        "team_name": "KuruSushi-Park", "asset_version": str(int(time.time())), "user": user,
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


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def split_dashboard(request: Request, user: SessionInfo = Depends(_require_page_user)):
    if templates is None:
        raise HTTPException(500, "templates directory missing")
    gates = state.entry_gates() or [settings.entry_gate]
    return templates.TemplateResponse(request, "dashboard.html", {
        "team_name": "KuruSushi-Park", "gates": gates,
        "default_gate": gates[0] if gates else settings.entry_gate,
        "asset_version": str(int(time.time())), "user": user,
    })


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_dashboard(request: Request):
    if templates is None:
        raise HTTPException(500, "templates directory missing")
    info = auth.sessions.get(request.cookies.get(SESSION_COOKIE))
    if info is None:
        raise _LoginRedirect("/admin")
    if info.role != ROLE_ADMIN:
        raise HTTPException(403, "admin role required")
    return templates.TemplateResponse(request, "admin.html", {
        "team_name": "KuruSushi-Park", "asset_version": str(int(time.time())), "user": info,
    })


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
async def get_history(limit: int = 100, plate: Optional[str] = None,
                      user: SessionInfo = Depends(auth.require_staff)) -> list[dict[str, Any]]:
    """Completed parking sessions, for the dashboard history table.
    Optional ``plate`` does a case-insensitive substring search, so staff can
    look up "what happened to car X" without scanning the whole log."""
    limit = max(1, min(limit, 500))
    if plate:
        return db.query(
            "SELECT * FROM sessions WHERE plate LIKE ? ORDER BY completed_at DESC LIMIT ?",
            (f"%{plate.strip()}%", limit))
    return db.query("SELECT * FROM sessions ORDER BY completed_at DESC LIMIT ?", (limit,))


@app.get("/api/zones")
async def get_zones(user: SessionInfo = Depends(auth.require_staff)) -> dict[str, Any]:
    """Occupied/free spots by zone plus that zone's gate status - so staff
    can see at a glance whether a zone (or the whole lot) is full."""
    return {"zones": state.zone_occupancy()}


@app.get("/api/admin/sessions")
async def get_admin_sessions(user: SessionInfo = Depends(auth.require_admin)) -> dict[str, Any]:
    """Who is currently logged in - Admin oversight, not available to Operators."""
    return {"sessions": [
        {"username": s.username, "role": s.role, "expires_at": s.expires_at}
        for s in auth.sessions.active_sessions()
    ]}


@app.get("/api/events")
async def get_events(limit: int = 50, event_class: Optional[str] = None,
                     user: SessionInfo = Depends(auth.require_staff)) -> list[dict[str, Any]]:
    """Raw webhook log, newest first."""
    limit = max(1, min(limit, 500))
    if event_class:
        return db.query(
            "SELECT * FROM events WHERE event_class = ? ORDER BY sequence_id DESC LIMIT ?",
            (event_class, limit))
    return db.query("SELECT * FROM events ORDER BY sequence_id DESC LIMIT ?", (limit,))


@app.get("/api/payments")
async def get_payments(limit: int = 100, user: SessionInfo = Depends(auth.require_staff)) -> list[dict[str, Any]]:
    return db.query("SELECT * FROM payments ORDER BY server_datetime DESC LIMIT ?",
                    (max(1, min(limit, 500)),))


@app.get("/api/signature-report")
async def get_signature_report(user: SessionInfo = Depends(auth.require_staff)) -> dict[str, Any]:
    """Which signature recipe the live simulator is actually using."""
    return {"attempts": db.signature_attempts(), "candidates": db.signature_trials()}


@app.get("/api/state")
async def get_state(user: SessionInfo = Depends(auth.require_staff)) -> dict[str, Any]:
    return state.snapshot()


@app.get("/api/spots")
async def get_spots(user: SessionInfo = Depends(auth.require_staff)) -> dict[str, Any]:
    return {"spots": state.snapshot()["spots"], "occupancy": state.occupancy_counts()}


@app.get("/api/gates")
async def get_gates(user: SessionInfo = Depends(auth.require_staff)) -> dict[str, Any]:
    return {"gates": state.entry_gates()}


@app.get("/api/broken")
async def get_broken(user: SessionInfo = Depends(auth.require_staff)) -> dict[str, Any]:
    return {"components": state.broken_components(), "deferred_repairs": dict(state.deferred_repairs)}


@app.get("/api/layout")
async def get_layout(user: SessionInfo = Depends(auth.require_staff)) -> dict[str, Any]:
    """Real simulator pixel-space geometry for the split-dashboard canvas.

    Geometry only, sourced from the level file (see app/layout.py) - never
    status. The caller merges this once against the live /api/state or
    /ws/telemetry feed for occupancy/broken/etc.
    """
    return load_layout(settings.seed_from_level or "lvl1")


# --------------------------------------------------------------------------- #
# Manual / operator controls
# --------------------------------------------------------------------------- #
@app.post("/api/manual/sync")
async def manual_sync(user: SessionInfo = Depends(auth.require_staff)) -> dict[str, Any]:
    counts = await sync_from_simulator()
    return {"ok": True, **counts}


class ManualArrivalIn(BaseModel):
    plate: str = Field(..., min_length=1, max_length=16)
    gate: str = Field(..., min_length=1, max_length=32)
    car_type: str = Field("Normal", max_length=16)
    dry_run: bool = False


@app.post("/api/manual/arrival")
async def manual_arrival(body: ManualArrivalIn, user: SessionInfo = Depends(auth.require_staff)) -> dict[str, Any]:
    if body.gate not in state.spots:
        raise HTTPException(404, f"unknown gate/spot {body.gate}")
    return await dispatch_entry(body.plate, body.gate, body.car_type, body.dry_run)


@app.post("/api/manual/barrier/{name}/open")
async def manual_barrier_open(name: str, user: SessionInfo = Depends(auth.require_staff)) -> dict[str, Any]:
    await client.barrier_open(name)
    state.update_barrier_state(name, "Open")
    state.log_activity(f"{user.username} opened barrier {name}")
    return {"ok": True, "name": name, "state": "Open"}


@app.post("/api/manual/barrier/{name}/close")
async def manual_barrier_close(name: str, user: SessionInfo = Depends(auth.require_staff)) -> dict[str, Any]:
    await client.barrier_close(name)
    state.update_barrier_state(name, "Closed")
    state.log_activity(f"{user.username} closed barrier {name}")
    return {"ok": True, "name": name, "state": "Closed"}


@app.post("/api/manual/repair/{name}")
async def manual_repair(name: str, user: SessionInfo = Depends(auth.require_staff)) -> dict[str, Any]:
    component_type = (
        "ParkingSpot" if name in state.spots
        else "BarrierGate" if name in state.barriers
        else "ExhaustFan" if name in state.fans
        else None
    )
    if component_type is None:
        raise HTTPException(404, f"unknown component {name}")
    await _queue_repair(component_type, name)
    state.log_activity(f"{user.username} queued repair for {name}")
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

    if spot_type == "EntrySpot" and direction == "CarIn":
        car_type = payload.get("CarType", "Normal")
        await dispatch_entry(plate, spot_name, car_type)
        return

    if spot_type == "Park" and direction == "CarIn":
        if state.get_session(plate) is None:
            # Not dispatched by us -- a car already in the lot when we started,
            # or one that survived a restart. Adopt it so it still gets billed.
            state.start_session(plate, gate="(adopted)",
                                car_type=payload.get("CarType", "Normal"))
            log.info("adopted untracked car %s parking at %s", plate, spot_name)
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
        if session is None:
            # Unknown car at the exit. We cannot know how long it really
            # stayed, but charging an estimate beats letting it leave unpaid:
            # billing zero reads to the simulator as not charging at all.
            session = state.start_session(plate, gate="(adopted)",
                                          car_type=payload.get("CarType", "Normal"))
            log.warning("unknown car %s at exit %s - charging an estimate", plate, spot_name)
            state.log_activity(f"Unknown car {plate} at exit - charging estimate", level="warn")
        if session.charged:
            return
        state.mark_at_exit(plate)
        session.exit_gate = spot_name
        # Charging inline here is too early - see _charge_at_exit, which logs
        # its own billing line once it actually computes an amount.
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
    await asyncio.sleep(settings.exit_charge_delay_s)

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
        log.info("billing %s: %.0f min -> parking=%.2f charging=%.2f (type=%s)%s",
                 plate, minutes, parking_cost, charging_cost, session.car_type, suffix)
        await act(f"charge {plate} parking={parking_cost} charging={charging_cost}{suffix}",
                  lambda: client.car_charge(plate, parking_cost, charging_cost))
        state.log_activity(f"Charged {plate} {session.expected_amount:.2f} at {spot_name}{suffix}")

        # Poll for payment rather than sleeping the whole window, so a car that
        # pays quickly is released quickly.
        waited = 0.0
        while waited < settings.payment_wait_s:
            await asyncio.sleep(1.0)
            waited += 1.0
            current = state.get_session(plate)
            if current is None or current.paid:
                return

        log.warning("%s has not paid %.2f after %.0fs - recharging",
                    plate, session.expected_amount, settings.payment_wait_s)

    log.error("%s never paid after %d attempts - it will likely escape",
              plate, settings.charge_max_attempts)
    state.log_activity(f"{plate} never paid after {settings.charge_max_attempts} attempts",
                       level="error")


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
                state.log_activity(f"Exhaust fan {fan_name} switched on for {zone_name}")
        elif not should_run and fan.is_on:
            if await act(f"fan {fan_name} OFF (zone {zone_name} CO={co_level:.1f})",
                         lambda n=fan_name: client.fan_off(n)):
                fan.is_on = False
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
