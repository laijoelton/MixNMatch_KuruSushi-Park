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
import logging
import math
import json
from datetime import datetime, timezone
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

from app import auth, dashboard_api, db, ml_agent, simlog, tariffs, zones
from app.client import client
from app.config import settings
from app.layout import announce_level, component_coordinates, load_layout, running_level
from app.queue_worker import maintenance_queue, schedule_lights
from app.routing import load_distance_table, rank_spots, set_coordinates
from app.seed import load_level
from app.signature import verify as verify_signature_recipes
from app.state import Barrier, BarrierPosition, SessionPhase, VehicleSession, SpotStatus, normalize_car_type, state
from app.policy import has, project_snapshot, project_events, redact
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
    raise NotImplementedError("TODO: reimplement act")


def _planned_of(payload: dict[str, Any]) -> float:
    """PlannedParkingDurationInMinutes from an event, as a number.

    Present on both entry and park events. The simulator bills this figure,
    not the wall-clock time we observe.
    """
    raise NotImplementedError("TODO: reimplement _planned_of")


def _round_minutes(minutes: float, mode: str = "round") -> float:
    raise NotImplementedError("TODO: reimplement _round_minutes")


def billing_multiplier(car_type: str, tariff: Optional[dict] = None) -> float:
    raise NotImplementedError("TODO: reimplement billing_multiplier")


def compute_charge(session) -> tuple[float, float, float]:
    raise NotImplementedError("TODO: reimplement compute_charge")


def verify_signature(payload: dict[str, Any], provided: Optional[str]) -> bool:
    raise NotImplementedError("TODO: reimplement verify_signature")


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
    raise NotImplementedError("TODO: reimplement _sync_if_no_live_bays")


# A goto sent while the entry barrier is still rising is dropped silently (the
# call still returns 201) and the car sits at the entry until it gives up. We
# wait for the barrier's own "Open" webhook, and if a dispatched car still has
# not left the entry after ENTRY_RETRY_S (normally it leaves in ~1 s) we send
# it once more. Both waits are simulated time, so they scale with game speed.
GATE_OPEN_WAIT_S = 5.0
ENTRY_RETRY_S = 8.0
_left_entry: set[str] = set()   # plates seen driving off their entry spot
_waiting_at: dict[str, str] = {}  # plate -> zone sensor it has reached (two-hop leg 2)
_holding_gate: dict[str, str] = {}  # plate -> zone entry gate opened for it, until it drives through
_entry_watchers: set[asyncio.Task] = set()
_dispatch_tasks: dict[str, asyncio.Task] = {}
_webhook_events_in_progress: set[str] = set()
_co_warned_zones: set[str] = set()  # zones with an unresolved PREDICTIVE_CO_WARNING broadcast


async def _wait_for_barrier_open(name: str) -> bool:
    """True once ``name`` reports Open (or needn't be waited for); False on timeout."""
    raise NotImplementedError("TODO: reimplement _wait_for_barrier_open")


def _barrier_for_sensor(sensor: str) -> Optional[str]:
    raise NotImplementedError("TODO: reimplement _barrier_for_sensor")


# --------------------------------------------------------------------------- #
# Zone gates: closed by default, opened per car, closed once it has passed.
# On Level 2 every car crosses ENTRY1 (and then ENTRY2/3 on its way down the
# road), so the gate to open is the *target zone's* entry gate - not the one
# nearest the sensor the car first tripped. The main gate is operator-only.
# --------------------------------------------------------------------------- #
_gates_closed_for_level = False


def _zone_entry_gate(zone: str, sensor: str) -> Optional[str]:
    raise NotImplementedError("TODO: reimplement _zone_entry_gate")


def _zone_gates(zone: str) -> tuple[Optional[str], Optional[str]]:
    """(entry gate, exit gate) of ``zone``. The exit gate is found through the
    zone's exit sensor, because ZONE1's exit gate (gate2) carries no zone tag."""
    raise NotImplementedError("TODO: reimplement _zone_gates")


def _all_zones() -> list[str]:
    raise NotImplementedError("TODO: reimplement _all_zones")


def _zone_of_gate(gate: str) -> Optional[str]:
    raise NotImplementedError("TODO: reimplement _zone_of_gate")


def _entry_gate_for_session(session) -> Optional[str]:
    raise NotImplementedError("TODO: reimplement _entry_gate_for_session")


def _gate_usable(name: Optional[str]) -> bool:
    raise NotImplementedError("TODO: reimplement _gate_usable")


def _gates_in_use() -> set[str]:
    """Gates a car still has to drive through: its zone entry gate until it
    parks, and its exit gate from payment until it has left the facility."""
    raise NotImplementedError("TODO: reimplement _gates_in_use")


async def _close_idle_gates(*, include_main: bool = False) -> None:
    raise NotImplementedError("TODO: reimplement _close_idle_gates")



# --------------------------------------------------------------------------- #
# Zone maintenance (4.26). When a zone's entry or exit gate needs repair the
# zone closes to new cars (dispatch skips it, so the ratio routes cars to the
# other zones). Its entry gate is repaired once no car is still driving in;
# its exit gate once the zone has emptied - parked cars leave at the end of
# their booked stay. A broken gate is repaired at once. The zone reopens only
# when both gates are fixed, so the entry stays shut until then.
# --------------------------------------------------------------------------- #
_maintenance_rotation: list[str] = []   # zones in the order maintenance started them


def _start_zone_maintenance(zone: str, trigger: str) -> None:
    raise NotImplementedError("TODO: reimplement _start_zone_maintenance")


def _zone_inbound_clear(zone: str, entry: Optional[str]) -> bool:
    raise NotImplementedError("TODO: reimplement _zone_inbound_clear")



async def _advance_zone_maintenance() -> None:
    raise NotImplementedError("TODO: reimplement _advance_zone_maintenance")



# --------------------------------------------------------------------------- #
# Balanced gate repairs (4.28). Exactly one gate may be in repair at a time,
# breakdowns included. When the slot is free: a broken gate first, then the
# next step of an open zone maintenance, then - preventively - the most-worn
# idle gate once it has GATE_REPAIR_MIN_OPENS opens since its last repair.
# Ranking by wear staggers the repairs instead of letting gates come due
# together. The main gate is never repaired preventively (operator-only).
# --------------------------------------------------------------------------- #
_repair_seen_at: dict[str, float] = {}   # gate -> when we first saw it under repair


def _gate_repair_slot_holder() -> Optional[str]:
    """The gate holding the one repair slot. A repair still unfinished after
    GATE_REPAIR_STUCK_S is declared stuck and no longer holds it (4.35). The
    clock starts when we first see the gate under repair: a level can load with
    a gate already half-repaired, and never finish it."""
    raise NotImplementedError("TODO: reimplement _gate_repair_slot_holder")


def _mark_repair_stuck(name: str) -> None:
    raise NotImplementedError("TODO: reimplement _mark_repair_stuck")


async def _schedule_gate_repairs() -> None:
    raise NotImplementedError("TODO: reimplement _schedule_gate_repairs")


async def _close_idle_gates_later(delay_s: Optional[float] = None) -> None:
    raise NotImplementedError("TODO: reimplement _close_idle_gates_later")


async def _close_gates_for_level_start() -> None:
    """Every gate, main gate included, starts closed - once per running level,
    so a later manual sync never shuts the main gate on the operator."""
    raise NotImplementedError("TODO: reimplement _close_gates_for_level_start")


# The simulator needs a moment after "Load Game" before list-* return the new
# bays. Loading is real time, not simulated time, so this does not scale with
# game speed; the retries stop at the first sync that sees bays.
LEVEL_SYNC_RETRY_S = 1.0
LEVEL_SYNC_ATTEMPTS = 15


async def _on_level_loaded(level: str) -> None:
    """The simulator console printed ``Load Game./settings/<level>.json``.

    Everything live from the previous level is gone in the simulator, so drop
    it here too: unfinished sessions (they would otherwise sit "charged at the
    exit" forever and keep exit gates in use), operator and ghost holds, and
    the gate/bay model. Then sync once - the docs' "once per level loading" -
    which closes every gate, main gate included.
    """
    raise NotImplementedError("TODO: reimplement _on_level_loaded")


def _spawn(coro) -> asyncio.Task:
    raise NotImplementedError("TODO: reimplement _spawn")


def _start_dispatch_retry(plate: str, spot: str, sensor: Optional[str] = None) -> None:
    raise NotImplementedError("TODO: reimplement _start_dispatch_retry")


def _zone_sensor(gate: Optional[str]) -> Optional[str]:
    """The entry sensor standing in front of ``gate`` (ENTRY3 for gate5)."""
    raise NotImplementedError("TODO: reimplement _zone_sensor")


async def _open_gate_and_wait(gate_name: str, why: str) -> None:
    """Open ``gate_name`` and wait for its Open report. The simulator plans a
    route when the goto arrives and treats a closed or rising gate as a wall:
    "Paths found: 0" at an entrance, "No valid escape spot found" at an exit."""
    raise NotImplementedError("TODO: reimplement _open_gate_and_wait")


# A car sent to its zone's sensor normally pulls off ENTRY1 within ~3 s (seen
# 1.1-2.9 s live). Resend the hop this many times before concluding the
# simulator refused it and falling back to opening the zone gate at ENTRY1.
HOP_ATTEMPTS = 5


async def _resend_if_still_at_entry(plate: str, spot: str, *, initial: bool = False,
                                    sensor: Optional[str] = None) -> None:
    """Get a car from the entry sensor it is waiting at (``sensor``, default
    its first one) to ``spot``, resending while it has not driven off.

    The simulator only accepts a goto from a car waiting at an entry or exit
    sensor. If the zone's gate is not in front of this sensor (a ZONE3 car at
    ENTRY1), the car is first sent to the zone's own sensor with the gate still
    closed; ``_handle_car_spot_action`` resumes it from there.
    """
    raise NotImplementedError("TODO: reimplement _resend_if_still_at_entry")


def _persist(plate: str) -> None:
    raise NotImplementedError("TODO: reimplement _persist")


def restore_sessions() -> None:
    raise NotImplementedError("TODO: reimplement restore_sessions")


def restore_operational_observability() -> None:
    """Restore durable counters that otherwise reset and hide problems on restart."""
    raise NotImplementedError("TODO: reimplement restore_operational_observability")


async def reap_orphans() -> None:
    raise NotImplementedError("TODO: reimplement reap_orphans")


async def sync_from_simulator() -> dict[str, int]:
    """One-shot list-* refresh. Called at startup and on manual operator request only -
    never on a timer, per the organizer's no-polling rule."""
    raise NotImplementedError("TODO: reimplement sync_from_simulator")


async def _ensure_barriers_open() -> None:
    # Entry gates are opened only by a dispatch. Exit gates remain under payment control.
    raise NotImplementedError("TODO: reimplement _ensure_barriers_open")


async def check_wear() -> None:
    raise NotImplementedError("TODO: reimplement check_wear")


async def _wear_check_loop() -> None:
    raise NotImplementedError("TODO: reimplement _wear_check_loop")


_last_motion: dict[str, float] = {}   # zone -> monotonic time a car was last seen moving there
_light_timer: Optional[asyncio.Task] = None   # switches held zones off when their hold ends


def _moving_zones() -> set[str]:
    """Zones with a car on the move: driving to a bay there, or out of its
    bay there and not yet gone. Parked cars and empty zones do not count."""
    raise NotImplementedError("TODO: reimplement _moving_zones")


async def _refresh_lights(server_datetime: Optional[str] = None) -> None:
    """Night lighting by movement (4.29): the time of day comes from the
    webhooks' ServerDateTime. At night a zone is lit only while a car moves in
    it, plus LIGHT_HOLD_S so lights do not flicker between cars. By day, off."""
    raise NotImplementedError("TODO: reimplement _refresh_lights")


async def _relight_after(delay_s: float, server_datetime: str) -> None:
    raise NotImplementedError("TODO: reimplement _relight_after")


async def _environment_loop() -> None:
    raise NotImplementedError("TODO: reimplement _environment_loop")


async def _broadcast_loop() -> None:
    raise NotImplementedError("TODO: reimplement _broadcast_loop")


@asynccontextmanager
async def lifespan(app: FastAPI):
    restore_operational_observability()
    restore_sessions()
    client.on_reconnect = sync_from_simulator
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
                state.load_lights(seeded["lights"])
                log.warning("running on SEEDED layout from %s - NOT live simulator state",
                            settings.seed_from_level)

    if load_distance_table():
        log.info("routing: using precomputed driving distances")
    else:
        log.warning("routing: data/distances.json missing - falling back to "
                    "straight-line distance. Run scripts.export_graph then "
                    "scripts.build_distances for true driving distance.")
    set_coordinates(component_coordinates(running_level() or "lvl2"))

    log.info("autopilot=%s  signature_mode=%s  game_speed=%sx",
             settings.autopilot, settings.webhook_signature_mode, settings.game_speed)

    maintenance_queue.start()
    broadcaster = asyncio.create_task(_broadcast_loop(), name="dispatcher-broadcaster")
    wear_checker = asyncio.create_task(_wear_check_loop(), name="dispatcher-wear-check")
    environment = asyncio.create_task(_environment_loop(), name="dispatcher-environment")
    predictive = asyncio.create_task(
        ml_agent.run_predictive_loop(wear_snapshot=state.wear_snapshot, queue_repair=_queue_repair,
                                      broadcast=manager.broadcast),
        name="dispatcher-ml-predictive",
    )
    background_tasks = (broadcaster, wear_checker, environment, predictive)
    if settings.simulator_log:
        background_tasks += (asyncio.create_task(simlog.follow(settings.simulator_log, _on_level_loaded),
                                                 name="dispatcher-level-watch"),)
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
        for task in list(_entry_watchers):
            task.cancel()
        await asyncio.gather(*list(_entry_watchers), return_exceptions=True)
        client.on_reconnect = None
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
def _retire_previous_visit(plate: str) -> None:
    """A plate at the entrance whose session already parked, reached an exit or
    was billed is a *new* visit - the simulator recycles plates from a fixed
    list. Reusing the old session made the new car inherit ``charged``/``paid``
    (no invoice at the exit, so it escaped) and its old bay (so the entrance
    short-circuit never dispatched it). A car still driving to its bay keeps
    its session: it trips ENTRY2/ENTRY3 on the way down the road.
    """
    raise NotImplementedError("TODO: reimplement _retire_previous_visit")


async def dispatch_entry(plate: str, gate_name: str, car_type: str = "Normal",
                         dry_run: bool = False, planned_minutes: float = 0.0,
                         target_spot: Optional[str] = None) -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement dispatch_entry")


async def assign_specific_spot(plate: str, gate_name: str, spot_name: str,
                               car_type: str = "Normal") -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement assign_specific_spot")


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def operator_dashboard(request: Request):
    raise NotImplementedError("TODO: reimplement operator_dashboard")


@app.get("/gate", response_class=HTMLResponse, include_in_schema=False)
async def gate_portal(request: Request, gate: Optional[str] = Query(None)):
    raise NotImplementedError("TODO: reimplement gate_portal")


@app.get("/dashboard", include_in_schema=False)
async def split_dashboard() -> RedirectResponse:
    """Superseded by the operator console at ``/``; kept so old links still work."""
    raise NotImplementedError("TODO: reimplement split_dashboard")


# --------------------------------------------------------------------------- #
# Read APIs
# --------------------------------------------------------------------------- #
@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement healthz")


@app.get("/api/history")
async def get_history(request: Request, limit: int = 100) -> list[dict[str, Any]]:
    """Completed parking sessions, for the dashboard history table."""
    raise NotImplementedError("TODO: reimplement get_history")


@app.get("/api/events")
async def get_events(request: Request, limit: int = 50, event_class: Optional[str] = None,
                     page: int = 1) -> list[dict[str, Any]]:
    raise NotImplementedError("TODO: reimplement get_events")


@app.get("/api/payments")
async def get_payments(limit: int = 100) -> list[dict[str, Any]]:
    raise NotImplementedError("TODO: reimplement get_payments")


@app.get("/api/signature-report")
async def get_signature_report() -> dict[str, Any]:
    """Which signature recipe the live simulator is actually using."""
    raise NotImplementedError("TODO: reimplement get_signature_report")


@app.get("/api/state")
async def get_state(request: Request) -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement get_state")


@app.get("/api/spots")
async def get_spots() -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement get_spots")


@app.get("/api/gates")
async def get_gates() -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement get_gates")


@app.get("/api/broken")
async def get_broken() -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement get_broken")


@app.get("/api/layout")
async def get_layout() -> dict[str, Any]:
    """Real simulator pixel-space geometry for the split-dashboard canvas.

    Geometry only, sourced from the level file (see app/layout.py) - never
    status. The caller merges this once against the live /api/state or
    /ws/telemetry feed for occupancy/broken/etc.
    """
    raise NotImplementedError("TODO: reimplement get_layout")


# --------------------------------------------------------------------------- #
# Manual / operator controls
# --------------------------------------------------------------------------- #
@app.post("/api/manual/sync")
async def manual_sync() -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement manual_sync")


class ManualArrivalIn(BaseModel):
    plate: str = Field(..., min_length=1, max_length=16)
    gate: str = Field(..., min_length=1, max_length=32)
    car_type: str = Field("Normal", max_length=16)
    dry_run: bool = False


@app.post("/api/manual/arrival")
async def manual_arrival(body: ManualArrivalIn) -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement manual_arrival")


def _staff_takes_gate(name: str) -> Barrier:
    """Staff commands beat the automation (4.33). Only a gate that is broken
    or actually being repaired refuses: operating it is penalised. A repair
    that is merely queued is cancelled - the rotation comes back to it later."""
    raise NotImplementedError("TODO: reimplement _staff_takes_gate")


def _set_staff_mode(barrier: Barrier, *, held_open: bool, held_closed: bool) -> None:
    raise NotImplementedError("TODO: reimplement _set_staff_mode")


@app.post("/api/barriers/{name}/open")
@app.post("/api/manual/barrier/{name}/open")
async def manual_barrier_open(name: str, user: dict = Depends(auth.require_capability("ops:control_gates"))) -> dict[str, Any]:
    """Hold the gate open until staff press Hold closed or Automatic. Cars
    held at it (a ghost car, a suspect payment) are let out."""
    raise NotImplementedError("TODO: reimplement manual_barrier_open")


@app.post("/api/barriers/{name}/close")
@app.post("/api/manual/barrier/{name}/close")
async def manual_barrier_close(name: str, user: dict = Depends(auth.require_capability("ops:control_gates"))) -> dict[str, Any]:
    """Hold the gate closed until staff press Open or Automatic."""
    raise NotImplementedError("TODO: reimplement manual_barrier_close")


@app.post("/api/barriers/{name}/auto")
@app.post("/api/manual/barrier/{name}/auto")
async def manual_barrier_auto(name: str, user: dict = Depends(auth.require_capability("ops:control_gates"))) -> dict[str, Any]:
    """Hand the gate back to the automation."""
    raise NotImplementedError("TODO: reimplement manual_barrier_auto")


@app.post("/api/manual/repair/{name}")
async def manual_repair(name: str, user: dict = Depends(auth.require_maintenance)) -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement manual_repair")


@app.post("/api/manual/fan/{name}/on")
async def manual_fan_on(name: str, user: dict = Depends(auth.require_maintenance)) -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement manual_fan_on")


@app.post("/api/manual/fan/{name}/off")
async def manual_fan_off(name: str, user: dict = Depends(auth.require_maintenance)) -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement manual_fan_off")


@app.post("/api/manual/light/{name}/on")
async def manual_light_on(name: str, user: dict = Depends(auth.require_staff)) -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement manual_light_on")


@app.post("/api/manual/light/{name}/off")
async def manual_light_off(name: str, user: dict = Depends(auth.require_staff)) -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement manual_light_off")


@app.post("/api/ghost-cars/{ghost_id}/override")
async def ghost_car_override(ghost_id: int,
                              user: dict = Depends(auth.require_capability("ops:control_gates"))) -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement ghost_car_override")


class GhostOverrideIn(BaseModel):
    ghost_id: int


@app.post("/api/ghost-car/override")
async def ghost_override_alias(body: GhostOverrideIn, user: dict = Depends(auth.require_capability("ops:control_gates"))):
    raise NotImplementedError("TODO: reimplement ghost_override_alias")


class GhostReleaseIn(BaseModel):
    confirm_unpaid: bool = False


@app.post("/api/ghost-cars/{ghost_id}/release")
async def ghost_car_release(ghost_id: int, body: GhostReleaseIn,
                            user: dict = Depends(auth.require_capability("ops:control_gates"))) -> dict[str, Any]:
    """Staff let a held ghost car leave (4.25). A car that has not paid yet is
    only released with ``confirm_unpaid``: it would leave unpaid, and the
    simulator fines CarEscapedWithoutPaying."""
    raise NotImplementedError("TODO: reimplement ghost_car_release")


@app.get("/api/ghost-cars")
async def list_ghost_cars(resolved: Optional[bool] = None,
                          user: dict = Depends(auth.require_staff)) -> list[dict[str, Any]]:
    raise NotImplementedError("TODO: reimplement list_ghost_cars")


# --------------------------------------------------------------------------- #
# Penalties (staff)
# --------------------------------------------------------------------------- #
@app.get("/api/penalties")
async def get_penalties(limit: int = 200, code: Optional[str] = None,
                        user: dict = Depends(auth.require_staff)) -> list[dict[str, Any]]:
    raise NotImplementedError("TODO: reimplement get_penalties")


# --------------------------------------------------------------------------- #
# Level 2 dynamic reporting
# --------------------------------------------------------------------------- #
@app.get("/api/reports/daily")
async def daily_report(user: dict = Depends(auth.require_staff)) -> dict[str, Any]:
    """Aggregated operational + (role-gated) financial metrics.

    Operational metrics are visible to any signed-in staff member; the
    revenue section is computed only with fin:view.
    """
    # Durable zone names preserve historical throughput across layout changes.
    raise NotImplementedError("TODO: reimplement daily_report")


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
    raise NotImplementedError("TODO: reimplement gate_checkin")


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
    raise NotImplementedError("TODO: reimplement api_dispatch")


# --------------------------------------------------------------------------- #
# Live WebSocket feed (operator HUD + gate picker + split dashboard)
# --------------------------------------------------------------------------- #
@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    raise NotImplementedError("TODO: reimplement ws_live")


@app.websocket("/ws/telemetry")
async def ws_telemetry(ws: WebSocket):
    """Identical feed to /ws/live, named for the split dashboard's operator
    canvas + phone GPS view so both stay trivially in sync with each other
    and with the main HUD - they all share one ConnectionManager broadcast."""
    raise NotImplementedError("TODO: reimplement ws_telemetry")


# --------------------------------------------------------------------------- #
# Inbound simulator webhook
# --------------------------------------------------------------------------- #
@app.post("/webhooks/simulator")
async def simulator_webhook(request: Request) -> JSONResponse:
    raise NotImplementedError("TODO: reimplement simulator_webhook")


# --------------------------------------------------------------------------- #
# Maintenance queue helper
# --------------------------------------------------------------------------- #
async def _queue_repair(component_type: str, name: str, *, via_zone: bool = False) -> None:
    raise NotImplementedError("TODO: reimplement _queue_repair")


# --------------------------------------------------------------------------- #
# Event handlers
# --------------------------------------------------------------------------- #
async def _handle_car_spot_action(payload: dict[str, Any]) -> None:
    raise NotImplementedError("TODO: reimplement _handle_car_spot_action")


def _valid_exit(session) -> bool:
    raise NotImplementedError("TODO: reimplement _valid_exit")


def _archive(plate: str) -> None:
    """Move a finished session out of memory and into SQLite for the dashboard."""
    raise NotImplementedError("TODO: reimplement _archive")


def _record_neglect_and_discard(plate: str, reason: str) -> None:
    """Persist an unserved arrival without counting it as completed throughput."""
    raise NotImplementedError("TODO: reimplement _record_neglect_and_discard")


async def _charge_at_exit(plate: str, spot_name: str) -> None:
    raise NotImplementedError("TODO: reimplement _charge_at_exit")


async def _hold_exit(plate: str, sensor: str) -> None:
    raise NotImplementedError("TODO: reimplement _hold_exit")


async def _handle_ghost_car(plate: str, exit_spot: str, payload: dict[str, Any]) -> None:
    """A car that was never seen at an entrance reached an exit (4.25).

    Either it is unknown here, or it appeared straight into a bay (an
    "adopted" session). It is billed automatically with the ML-estimated fee
    (``POST /car/{plate}/charge`` is the simulator's "ask car for payment"),
    its exit barrier is held closed, and a payment does NOT release it: staff
    release it from the dashboard banner (POST /api/ghost-cars/{id}/release).
    """
    raise NotImplementedError("TODO: reimplement _handle_ghost_car")


async def _invoice_ghost(plate: str) -> None:
    raise NotImplementedError("TODO: reimplement _invoice_ghost")


def _ghost_status(plate: str) -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement _ghost_status")


async def _broadcast_ghost(ghost_id: int, plate: str, gate: Optional[str], *, resolved: bool = False,
                           **ml_detail: Any) -> None:
    raise NotImplementedError("TODO: reimplement _broadcast_ghost")

async def _handle_component_broken(payload: dict[str, Any]) -> None:
    raise NotImplementedError("TODO: reimplement _handle_component_broken")


async def _handle_component_fixed(payload: dict[str, Any]) -> None:
    raise NotImplementedError("TODO: reimplement _handle_component_fixed")


async def _handle_carbon_monoxide_event(payload: dict[str, Any]) -> None:
    raise NotImplementedError("TODO: reimplement _handle_carbon_monoxide_event")


async def _handle_gate_action(payload: dict[str, Any]) -> None:
    raise NotImplementedError("TODO: reimplement _handle_gate_action")


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
    raise NotImplementedError("TODO: reimplement _find_session_by_loose_plate")


async def _apply_charge_correction(payload: dict[str, Any], sent: float, should_be: float) -> None:
    # Corrections are evidence for review, never a second charge or a changed invoice.
    raise NotImplementedError("TODO: reimplement _apply_charge_correction")


async def _handle_payment_made(payload: dict[str, Any]) -> None:
    """Validate the payment, then release the car.

    The docs warn that "some cars will tweak the system and send fake
    payment", so the reported Amount is checked against what we actually
    billed. The car is only sent to leavepark once that passes - releasing an
    underpaying car is Penalty_CarEscapedWithoutPaying.
    """
    raise NotImplementedError("TODO: reimplement _handle_payment_made")


_releases_in_progress: set[str] = set()


async def _release_paid(session) -> None:
    raise NotImplementedError("TODO: reimplement _release_paid")


async def _send_paid_release(session) -> None:
    raise NotImplementedError("TODO: reimplement _send_paid_release")



async def _handle_penalty(payload: dict[str, Any]) -> None:
    raise NotImplementedError("TODO: reimplement _handle_penalty")


async def _handle_test_webhook(payload: dict[str, Any]) -> None:
    raise NotImplementedError("TODO: reimplement _handle_test_webhook")


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
