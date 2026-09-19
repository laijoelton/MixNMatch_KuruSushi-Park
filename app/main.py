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
from app.layout import announce_level, load_layout, running_level
from app.queue_worker import maintenance_queue, schedule_lights
from app.routing import load_distance_table, rank_spots, ring
from app.seed import load_level
from app.signature import verify as verify_signature_recipes
from app.state import BarrierPosition, SessionPhase, VehicleSession, SpotStatus, normalize_car_type, state
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
        value = float(payload.get("PlannedParkingDurationInMinutes") or 0)
        return value if math.isfinite(value) and value > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _round_minutes(minutes: float, mode: str = "round") -> float:
    if minutes <= 0:
        return 0.0
    if mode == "ceil":
        return float(math.ceil(minutes))
    if mode == "exact":
        return round(minutes, 2)
    return float(max(1, math.floor(minutes + 0.5)))


def billing_multiplier(car_type: str, tariff: Optional[dict] = None) -> float:
    tariff = tariff or tariffs.effective()
    key = normalize_car_type(car_type)
    return float(tariff.get(f"class_multiplier_{key}", 1.0))


def compute_charge(session) -> tuple[float, float, float]:
    tariff = tariffs.effective()
    if tariff["billing_basis"] == "planned" and session.planned_minutes > 0:
        billable = session.billable_minutes
    else:
        minutes = session.measured_minutes * settings.game_speed
        if session.parked_at is None:
            minutes = settings.unknown_car_minutes
        billable = _round_minutes(minutes, tariff["billing_rounding"])
    base = round(max(tariff["minimum_charge"], billable * tariff["parking_rate_per_minute"])
                 * billing_multiplier(session.car_type, tariff), 2)
    if not session.is_electric:
        return base, 0.0, billable
    if tariff["electric_split_charging"]:
        return base, round(base * max(0, tariff["electric_multiplier"] - 1), 2), billable
    return round(base * tariff["electric_multiplier"], 2), 0.0, billable


def verify_signature(payload: dict[str, Any], provided: Optional[str]) -> bool:
    return verify_signature_recipes(payload).ok


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
        if _live_bays_synced or time.monotonic() - _last_traffic_sync < TRAFFIC_SYNC_COOLDOWN_S / max(settings.game_speed, 0.1):
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
_waiting_at: dict[str, str] = {}  # plate -> zone sensor it has reached (two-hop leg 2)
_holding_gate: dict[str, str] = {}  # plate -> zone entry gate opened for it, until it drives through
_entry_watchers: set[asyncio.Task] = set()
_dispatch_tasks: dict[str, asyncio.Task] = {}
_webhook_events_in_progress: set[str] = set()


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


def _barrier_for_sensor(sensor: str) -> Optional[str]:
    if sensor in state.barriers:
        return sensor
    layout = load_layout(running_level() or "lvl1")
    position = layout.get("spots", {}).get(sensor)
    gates = {n: g for n, g in layout.get("gates", {}).items() if n in state.barriers}
    if position and gates:
        return min(gates, key=lambda n: (gates[n]["x"] - position["x"]) ** 2
                   + (gates[n]["y"] - position["y"]) ** 2)
    if sensor.upper().startswith("ENTRY") and settings.entry_gate in state.barriers:
        return settings.entry_gate
    return next(iter(state.barriers)) if len(state.barriers) == 1 else None


# --------------------------------------------------------------------------- #
# Zone gates: closed by default, opened per car, closed once it has passed.
# On Level 2 every car crosses ENTRY1 (and then ENTRY2/3 on its way down the
# road), so the gate to open is the *target zone's* entry gate - not the one
# nearest the sensor the car first tripped. The main gate is operator-only.
# --------------------------------------------------------------------------- #
_gates_closed_for_level = False


def _zone_entry_gate(zone: str, sensor: str) -> Optional[str]:
    layout = load_layout(running_level() or "lvl1")
    positions = {n: (g["x"], g["y"]) for n, g in layout.get("gates", {}).items()}
    entries = [(s["x"], s["y"]) for s in layout.get("spots", {}).values() if s.get("purpose") == "EntrySpot"]
    gate = zones.entry_gate_for_zone(zone, state.barriers.values(), positions, entries,
                                     exclude={settings.main_gate})
    return gate or _barrier_for_sensor(sensor)


def _zone_gates(zone: str) -> tuple[Optional[str], Optional[str]]:
    """(entry gate, exit gate) of ``zone``. The exit gate is found through the
    zone's exit sensor, because ZONE1's exit gate (gate2) carries no zone tag."""
    layout = load_layout(running_level() or "lvl1")
    positions = {n: (g["x"], g["y"]) for n, g in layout.get("gates", {}).items()}
    entries = [(s["x"], s["y"]) for s in layout.get("spots", {}).values() if s.get("purpose") == "EntrySpot"]
    entry = zones.entry_gate_for_zone(zone, state.barriers.values(), positions, entries,
                                      exclude={settings.main_gate})
    # Geometry only: _barrier_for_sensor's "the only barrier" fallback would
    # misread any lone gate as a zone's exit gate.
    known = {n: xy for n, xy in positions.items() if n in state.barriers and n != settings.main_gate}
    exit_gate = None
    for s in layout.get("spots", {}).values():
        if s.get("purpose") == "ExitSpot" and s.get("zone") == zone and known:
            exit_gate = min(known, key=lambda n: (known[n][0] - s["x"]) ** 2 + (known[n][1] - s["y"]) ** 2)
            break
    return entry, exit_gate


def _zone_of_gate(gate: str) -> Optional[str]:
    if gate == settings.main_gate:
        return None
    layout = load_layout(running_level() or "lvl1")
    for zone in sorted({s.get("zone") for s in layout.get("spots", {}).values() if s.get("zone")}):
        if gate in _zone_gates(zone):
            return zone
    return None


def _entry_gate_for_session(session) -> Optional[str]:
    spot = state.spots.get(session.assigned_spot or "")
    return _zone_entry_gate(spot.zone_parent if spot else "", session.entry_gate)


def _gate_usable(name: Optional[str]) -> bool:
    barrier = state.barriers.get(name or "")
    return barrier is None or not (barrier.operator_override or barrier.held_vehicles or barrier.broken
                                   or barrier.under_maintenance or barrier.name in state.pending_repairs)


def _gates_in_use() -> set[str]:
    """Gates a car still has to drive through: its zone entry gate until it
    parks, and its exit gate from payment until it has left the facility."""
    used: set[Optional[str]] = set(_holding_gate.values())
    for session in list(state.sessions.values()):
        if session.paid and session.exit_gate:
            used.add(_barrier_for_sensor(session.exit_gate))
    return {name for name in used if name}


async def _close_idle_gates(*, include_main: bool = False) -> None:
    in_use = _gates_in_use()
    for name, barrier in list(state.barriers.items()):
        if (name in in_use or (name == settings.main_gate and not include_main)
                or barrier.state not in (BarrierPosition.OPEN, BarrierPosition.OPENING)
                or barrier.broken or barrier.under_maintenance or name in state.pending_repairs):
            continue
        if await act(f"close idle gate {name}", lambda n=name: client.barrier_close(n)):
            state.update_barrier_state(name, "Closing")
            db.sync_component_wear(name, "BarrierGate", state.record_barrier_cycle(name), 0)



# --------------------------------------------------------------------------- #
# Zone maintenance (4.26). When a zone's entry or exit gate needs repair the
# zone closes to new cars (dispatch skips it, so the ratio routes cars to the
# other zones). Its entry gate is repaired once no car is still driving in;
# its exit gate once the zone has emptied - parked cars leave at the end of
# their booked stay. A broken gate is repaired at once. The zone reopens only
# when both gates are fixed, so the entry stays shut until then.
# --------------------------------------------------------------------------- #
def _start_zone_maintenance(zone: str, trigger: str) -> None:
    if zone in state.zone_maintenance:
        return
    entry, exit_gate = _zone_gates(zone)
    state.zone_maintenance[zone] = {"entry": entry, "exit": exit_gate, "trigger": trigger,
                                    "todo": {g for g in (entry, exit_gate) if g}}
    state.log_activity(f"{zone} closed for maintenance: {trigger} needs repair. New cars go to other "
                       f"zones; {exit_gate or 'the exit gate'} is repaired once the zone is empty",
                       level="warn", capability="logs:view_maint")


def _zone_inbound_clear(zone: str, entry: Optional[str]) -> bool:
    if entry and entry in _holding_gate.values():
        return False
    for session in list(state.sessions.values()):
        spot = state.spots.get(session.assigned_spot or "")
        if session.phase == SessionPhase.ASSIGNED and session.parked_at is None and spot and spot.zone_parent == zone:
            return False
    return True


def _zone_empty(zone: str) -> bool:
    if any(s.zone_parent == zone and s.status in (SpotStatus.OCCUPIED, SpotStatus.RESERVED)
           for s in state.spots.values()):
        return False
    for session in list(state.sessions.values()):
        spot = state.spots.get(session.assigned_spot or "")
        if session.zone == zone or (spot and spot.zone_parent == zone):
            return False   # parked there, or still on its way out
    return True


async def _advance_zone_maintenance() -> None:
    for zone, info in list(state.zone_maintenance.items()):
        entry, exit_gate = info["entry"], info["exit"]
        if entry in info["todo"] and _zone_inbound_clear(zone, entry):
            await _queue_repair("BarrierGate", entry, via_zone=True)
        if exit_gate in info["todo"] and _zone_empty(zone):
            await _queue_repair("BarrierGate", exit_gate, via_zone=True)



# --------------------------------------------------------------------------- #
# Balanced gate repairs (4.28). Exactly one gate may be in repair at a time,
# breakdowns included. When the slot is free: a broken gate first, then the
# next step of an open zone maintenance, then - preventively - the most-worn
# idle gate once it has GATE_REPAIR_MIN_OPENS opens since its last repair.
# Ranking by wear staggers the repairs instead of letting gates come due
# together. The main gate is never repaired preventively (operator-only).
# --------------------------------------------------------------------------- #
def _gate_repair_slot_holder() -> Optional[str]:
    for name, barrier in state.barriers.items():
        if barrier.under_maintenance or name in state.pending_repairs:
            return name
    return None


async def _schedule_gate_repairs() -> None:
    if _gate_repair_slot_holder():
        return
    for barrier in sorted((b for b in state.barriers.values() if b.broken),
                          key=lambda b: (-b.opens_since_repair, b.name)):
        await _queue_repair("BarrierGate", barrier.name)
        if _gate_repair_slot_holder():
            return
    await _advance_zone_maintenance()
    if _gate_repair_slot_holder() or state.zone_maintenance:
        return   # one zone at a time; its next gate goes when it is ready
    worn = sorted((b for b in state.barriers.values()
                   if b.name != settings.main_gate and not b.broken and not b.under_maintenance
                   and b.opens_since_repair >= settings.gate_repair_min_opens),
                  key=lambda b: (-b.opens_since_repair, b.name))
    if worn:
        state.log_activity(f"Preventive maintenance: {worn[0].name} is the most worn gate "
                           f"({worn[0].opens_since_repair} opens since repair)", capability="logs:view_maint")
        await _queue_repair("BarrierGate", worn[0].name)


async def _close_idle_gates_later(delay_s: Optional[float] = None) -> None:
    delay_s = settings.gate_close_delay_s if delay_s is None else delay_s
    await asyncio.sleep(delay_s / max(settings.game_speed, 0.1))
    await _close_idle_gates()


async def _close_gates_for_level_start() -> None:
    """Every gate, main gate included, starts closed - once per running level,
    so a later manual sync never shuts the main gate on the operator."""
    global _gates_closed_for_level
    if _gates_closed_for_level:
        return
    _gates_closed_for_level = True
    await _close_idle_gates(include_main=True)
    state.log_activity("Level start: all gates closed; open the main gate to admit cars")


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
    global _live_bays_synced, _gates_closed_for_level
    for task in list(_entry_watchers):
        task.cancel()
    _dispatch_tasks.clear()
    _left_entry.clear()
    _waiting_at.clear()
    _holding_gate.clear()
    _last_motion.clear()
    db.reset_live_level()
    dropped = state.reset_for_new_level()
    announce_level(level)
    _live_bays_synced = False
    _gates_closed_for_level = False
    state.log_activity(f"Simulator loaded {level}: previous level cleared "
                       f"({dropped} unfinished vehicle{'s' if dropped != 1 else ''} dropped, gate holds released)")
    for _ in range(LEVEL_SYNC_ATTEMPTS):
        await asyncio.sleep(LEVEL_SYNC_RETRY_S)
        try:
            await sync_from_simulator()
        except Exception as exc:  # noqa: BLE001 - the simulator may still be loading
            log.warning("sync after %s load failed: %s", level, exc)
        if _live_bays_synced:
            return
    state.log_activity(f"{level} loaded but no bays were returned yet; the first car will sync it", level="warn")


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _entry_watchers.add(task)
    task.add_done_callback(_entry_watchers.discard)
    return task


def _start_dispatch_retry(plate: str, spot: str, sensor: Optional[str] = None) -> None:
    existing = _dispatch_tasks.get(plate)
    if existing and not existing.done():
        if sensor is None:
            return
        existing.cancel()   # the car moved on to its zone sensor: that leg is over
    task = _spawn(_resend_if_still_at_entry(plate, spot, initial=True, sensor=sensor))
    _dispatch_tasks[plate] = task
    def finished(completed):
        if _dispatch_tasks.get(plate) is completed:
            _dispatch_tasks.pop(plate, None)
    task.add_done_callback(finished)


def _zone_sensor(gate: Optional[str]) -> Optional[str]:
    """The entry sensor standing in front of ``gate`` (ENTRY3 for gate5)."""
    if not gate:
        return None
    layout = load_layout(running_level() or "lvl1")
    for name, spot in layout.get("spots", {}).items():
        if spot.get("purpose") == "EntrySpot" and _barrier_for_sensor(name) == gate:
            return name
    return None


async def _open_gate_and_wait(gate_name: str, why: str) -> None:
    """Open ``gate_name`` and wait for its Open report. The simulator plans a
    route when the goto arrives and treats a closed or rising gate as a wall:
    "Paths found: 0" at an entrance, "No valid escape spot found" at an exit."""
    barrier = state.barriers.get(gate_name)
    if barrier is None:
        return
    if barrier.state not in (BarrierPosition.OPEN, BarrierPosition.OPENING):
        if await act(f"open {gate_name} {why}", lambda: client.barrier_open(gate_name)):
            state.update_barrier_state(gate_name, "Opening")
            db.sync_component_wear(gate_name, "BarrierGate", state.record_barrier_cycle(gate_name), 0)
    await _wait_for_barrier_open(gate_name)


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
    session = state.get_session(plate)
    if session is None:
        return
    identity = session.session_id
    here = sensor or session.entry_gate
    gate_name = _entry_gate_for_session(session)
    via = _zone_sensor(gate_name) if gate_name in state.barriers else None
    hop = bool(settings.zone_gate_at_sensor and via and via != here and _barrier_for_sensor(here) != gate_name)
    plan = ["hop"] * HOP_ATTEMPTS + ["direct"] * settings.entry_max_attempts if hop \
        else ["direct"] * settings.entry_max_attempts
    delay_s = max(0.4, 1.0 / max(settings.game_speed, 1.0))
    for attempt, how in enumerate(plan):
        if attempt or not initial:
            await asyncio.sleep(delay_s)
        session = state.get_session(plate)
        if (session is None or session.session_id != identity or session.entry_departed
                or plate in _left_entry or session.phase != SessionPhase.ASSIGNED
                or session.assigned_spot != spot or _waiting_at.get(plate, session.entry_gate) != here):
            return   # gone, or now waiting at another sensor that has its own leg
        target = state.spots.get(spot)
        if (target is None or target.status != SpotStatus.RESERVED or target.occupant_plate != plate
                or target.broken or target.under_maintenance or spot in state.pending_repairs):
            state.log_activity(f"Dispatch paused for {plate}: bay {spot} is no longer safely reserved", level="warn")
            return
        if not _gate_usable(gate_name):
            return
        if how == "hop":
            session.staged_via = via
            await act(f"car {plate} -> {via} on the way to {spot} (attempt {attempt + 1})",
                      lambda: client.car_goto(plate, via))
            continue
        if session.staged_via and session.staged_via != here:
            session.staged_via = None
            state.log_activity(f"{plate} would not drive to {via}; opening {gate_name} from {here} instead", level="warn")
        if gate_name:
            _holding_gate[plate] = gate_name
        await _open_gate_and_wait(gate_name, f"for {plate}")
        await act(f"car {plate} -> {spot} (attempt {attempt + 1})", lambda: client.car_goto(plate, spot))
    state.log_activity(f"{plate} still at {here} after {len(plan)} attempts", level="warn")


def _persist(plate: str) -> None:
    session = state.get_session(plate)
    if session:
        db.save_active_session(session)


def restore_sessions() -> None:
    now = time.monotonic()
    def monotonic_stamp(raw):
        if not raw:
            return None
        return now - max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(raw)).total_seconds())
    for row in db.query("SELECT * FROM active_sessions"):
        if row["plate"] in state.sessions:
            continue
        data = json.loads(row["payload"])
        data["phase"] = SessionPhase(data["phase"])
        data["created_at"] = monotonic_stamp(data["arrived_wall"]) or now
        data["parked_at"] = monotonic_stamp(data["parked_wall"])
        data["left_spot_at"] = monotonic_stamp(data["left_spot_wall"])
        data["charge_attempted"] = bool(row["charge_attempted"])
        session = VehicleSession(**data)
        state.sessions[session.plate] = session
    for session in state.sessions.values():
        if session.assigned_spot:
            state.active_dispatches[session.plate] = session.assigned_spot
            spot = state.spots.get(session.assigned_spot)
            # A live occupancy count is authoritative even if its vehicle identity
            # is unknown. Downgrading it to RESERVED lets expiry free a full bay.
            if (spot and spot.dispatchable and session.left_spot_at is None
                    and spot.occupant_plate in (None, session.plate)):
                spot.occupant_plate = session.plate
                spot.status = SpotStatus.OCCUPIED if session.parked_at else SpotStatus.RESERVED
                spot.reserved_at = session.created_at if not session.parked_at else None
    state.neglected_vehicles.clear()
    state.neglected_vehicles.extend(db.query("SELECT * FROM neglected_vehicles ORDER BY occurred_at DESC LIMIT 100"))


def restore_operational_observability() -> None:
    """Restore durable counters that otherwise reset and hide problems on restart."""
    sequence = db.query("SELECT COALESCE(MAX(sequence_id), 0) AS n FROM events")[0]["n"]
    try:
        state.last_sequence_id = max(state.last_sequence_id, int(sequence or 0))
    except (TypeError, ValueError):
        pass
    counters = db.counters()
    state.penalty_count = int(counters["penalties"] or 0)
    state.total_fines = float(counters["total_fines"] or 0.0)


async def reap_orphans() -> None:
    now = time.monotonic()
    expired = False
    for plate, session in list(state.sessions.items()):
        if (session.left_spot_at is not None and not session.exit_confirmed
                and now - session.left_spot_at > settings.orphan_timeout_s / settings.game_speed):
            state.log_activity(f"Orphan session archived: {plate} left a bay but no final exit was observed", level="warn")
            _archive(plate)
        elif session.parked_at is None and not session.exit_confirmed and now - session.created_at > settings.reservation_ttl_s / settings.game_speed:
            reason = "Left entrance without reaching a bay" if session.entry_departed else "No bay arrival before reservation expired"
            db.record_neglect(session, reason)
            state.neglected_vehicles.appendleft({"plate": plate, "gate": session.entry_gate, "reason": reason})
            state.log_activity(f"Neglected vehicle {plate}: {reason}", level="warn")
            if session.assigned_spot:
                state.release_reservation(session.assigned_spot, plate)
            _archive(plate)
            expired = True
    if expired:
        await _close_idle_gates()   # nobody is coming through that zone gate any more


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

    lights = await client.list_lights()
    state.spots.clear()
    state.load_spots(spots)
    state.barriers.clear()
    state.load_barriers(barriers)
    state.zones.clear()
    state.load_zones(zones)
    state.fans.clear()
    state.load_fans(fans)
    state.lights.clear()
    state.load_lights(lights)
    # A live empty sensor supersedes an old parked record after a missed SpotLeft.
    for session in state.sessions.values():
        spot = state.spots.get(session.assigned_spot)
        if (spot and spot.status == SpotStatus.AVAILABLE and session.parked_at is not None
                and session.left_spot_at is None):
            state.mark_left_spot(session.plate)
            _persist(session.plate)
    restore_sessions()
    for row in db.component_wear_rows():
        component = state.barriers.get(row["name"]) or state.fans.get(row["name"]) or state.lights.get(row["name"]) or state.spots.get(row["name"])
        if component:
            component.cycle_count = max(component.cycle_count, row["cycle_count"])
            if hasattr(component, "runtime_seconds"):
                component.runtime_seconds = max(component.runtime_seconds, row["runtime_seconds"])
    for row in state.wear_snapshot():
        if not row["under_maintenance"]:
            db.set_meta(f"pending_proactive_repair:{row['name']}", "0")
    for barrier in state.barriers.values():
        barrier.operator_override = db.get_meta(f"gate_override:{barrier.name}") == "1"
        barrier.held_vehicles = set(json.loads(db.get_meta(f"gate_holds:{barrier.name}", "[]")))
    for row in db.query("SELECT plate, gate FROM ghost_car_events WHERE resolved = 0"):
        gate = _barrier_for_sensor(row["gate"] or "")
        if gate:
            state.barriers[gate].held_vehicles.add(row["plate"])
    ring.rebuild(list(state.spots.keys()) + list(state.barriers.keys()))
    global _live_bays_synced
    if any(s.get("purpose") == "Park" for s in spots):
        _live_bays_synced = True
    counts = {"spots": len(state.spots), "barriers": len(state.barriers),
              "zones": len(state.zones), "fans": len(state.fans)}
    state.log_activity(f"Manual sync: {counts['spots']} spots, {counts['barriers']} barriers, "
                       f"{counts['zones']} zones, {counts['fans']} fans")
    if _live_bays_synced:
        await _close_gates_for_level_start()
    await _ensure_barriers_open()
    return counts


async def _ensure_barriers_open() -> None:
    # Entry gates are opened only by a dispatch. Exit gates remain under payment control.
    for session in list(state.sessions.values()):
        if session.phase == SessionPhase.ASSIGNED and not session.entry_departed:
            _start_dispatch_retry(session.plate, session.assigned_spot)
        elif session.exit_confirmed and not session.charge_attempted and not session.paid:
            _spawn(_charge_at_exit(session.plate, session.exit_gate or ""))
        elif session.paid and not session.released:
            await _release_paid(session)


async def check_wear() -> None:
    await reap_orphans()
    for row in state.wear_snapshot():
        db.sync_component_wear(row["name"], row["type"], row["cycle_count"], row["runtime_seconds"])
        if row["under_maintenance"] or row["type"] in ("Light", "BarrierGate"):
            continue   # gates: _schedule_gate_repairs (4.28)
        if row["broken"]:
            await _queue_repair(row["type"], row["name"])
            continue
        if row["type"] == "ParkingSpot":
            if row["cycle_count"] < settings.spot_preventive_parks:
                continue
        elif row["cycle_count"] < 0.85 * settings.wear_cycle_threshold and row["runtime_seconds"] < 0.85 * settings.wear_runtime_threshold_s:
            continue
        name, component_type = row["name"], row["type"]
        if db.get_meta(f"pending_proactive_repair:{name}") == "1":
            continue
        if settings.autopilot:
            db.set_meta(f"pending_proactive_repair:{name}", "1")
            db.record_component_event(name, component_type, "repair_triggered_proactive")
        await _queue_repair(component_type, name)
        state.log_activity(f"Preventive maintenance: {component_type} {name} reached 85% wear",
                           capability="logs:view_maint")
    await _schedule_gate_repairs()


async def _wear_check_loop() -> None:
    while True:
        try:
            await check_wear()
        except Exception:
            log.exception("wear-check sweep failed")
        await asyncio.sleep(settings.environment_loop_interval_s / max(settings.game_speed, 0.1))


_last_motion: dict[str, float] = {}   # zone -> monotonic time a car was last seen moving there
_light_timer: Optional[asyncio.Task] = None   # switches held zones off when their hold ends


def _moving_zones() -> set[str]:
    """Zones with a car on the move: driving to a bay there, or out of its
    bay there and not yet gone. Parked cars and empty zones do not count."""
    zones_moving: set[str] = set()
    for session in list(state.sessions.values()):
        spot = state.spots.get(session.assigned_spot or "")
        zone = session.zone or (spot.zone_parent if spot else "")
        driving_in = session.phase == SessionPhase.ASSIGNED and session.parked_at is None and session.reached_zone
        leaving = session.left_spot_at is not None
        if zone and (driving_in or leaving):
            zones_moving.add(zone)
    return zones_moving


async def _refresh_lights(server_datetime: Optional[str] = None) -> None:
    """Night lighting by movement (4.29): the time of day comes from the
    webhooks' ServerDateTime. At night a zone is lit only while a car moves in
    it, plus LIGHT_HOLD_S so lights do not flicker between cars. By day, off."""
    if server_datetime is None:
        rows = db.query("SELECT server_datetime FROM events WHERE server_datetime IS NOT NULL "
                        "ORDER BY received_at DESC LIMIT 1")
        if not rows:
            return
        server_datetime = rows[0]["server_datetime"]
    now = time.monotonic()
    for zone in _moving_zones():
        _last_motion[zone] = now
    hold = settings.light_hold_s / max(settings.game_speed, 0.1)
    moving = _moving_zones()
    held = {zone for zone, seen in _last_motion.items() if zone not in moving and now - seen < hold}
    await schedule_lights(server_datetime, state, client, act, lit_zones=moving | held)
    # A held zone must go dark when its hold ends even if no webhook arrives
    # then; waiting for the next event or the 15 s loop left zones lit ~25 s (4.30).
    global _light_timer
    if held and (_light_timer is None or _light_timer.done()):
        wait = min(_last_motion[zone] + hold for zone in held) - now
        _light_timer = _spawn(_relight_after(max(wait, 0.0) + 0.01, server_datetime))


async def _relight_after(delay_s: float, server_datetime: str) -> None:
    await asyncio.sleep(delay_s)
    await _refresh_lights(server_datetime)


async def _environment_loop() -> None:
    while True:
        try:
            await _refresh_lights()
        except Exception:
            log.exception("environment loop tick failed")
        await asyncio.sleep(settings.environment_loop_interval_s / max(settings.game_speed, 0.1))


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
    predictive = asyncio.create_task(
        ml_agent.run_predictive_loop(wear_snapshot=state.wear_snapshot, queue_repair=_queue_repair),
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
    session = state.get_session(plate)
    if session is None or not (session.parked_at is not None or session.exit_confirmed
                               or session.charge_attempted or session.paid):
        return
    if session.assigned_spot:
        state.release_reservation(session.assigned_spot, plate)
    state.log_activity(f"{plate} is back at the entrance: previous visit closed, new visit started")
    _archive(plate)


async def dispatch_entry(plate: str, gate_name: str, car_type: str = "Normal",
                         dry_run: bool = False, planned_minutes: float = 0.0,
                         target_spot: Optional[str] = None) -> dict[str, Any]:
    if not dry_run:
        _retire_previous_visit(plate)
    assigned = state.active_dispatches.get(plate)
    if assigned:
        log.info("idempotent dispatch short-circuit: %s already assigned %s", plate, assigned)
        return {"plate": plate, "gate": gate_name, "target": assigned, "dispatched": True, "duplicate": True}
    session = None
    if not dry_run:
        session = state.start_session(plate, gate=gate_name, car_type=car_type,
                                      planned_minutes=planned_minutes)
        _persist(plate)
    await reap_orphans()
    kind = normalize_car_type(car_type)
    candidate_type = "Electric" if kind == "ev" else "Accessible" if kind == "accessible" else "Any"
    candidates = state.available_spots(candidate_type)
    if kind == "accessible":
        accessible = [n for n in candidates if state.spots[n].is_accessible]
        candidates = accessible or candidates
    # A zone closed for gate maintenance takes no new cars (4.26); the car
    # waits rather than being turned away if every suitable zone is closed.
    open_candidates = [n for n in candidates if state.spots[n].zone_parent not in state.zone_maintenance]
    if candidates and not open_candidates:
        return {"plate": plate, "gate": gate_name, "target": None, "dispatched": False,
                "reason": "Zone closed for maintenance"}
    candidates = open_candidates
    # A zone whose entry gate is held, broken or under repair cannot take the car.
    zone_gate = {z: _zone_entry_gate(z, gate_name) for z in {state.spots[n].zone_parent for n in candidates}}
    reachable = [n for n in candidates if _gate_usable(zone_gate[state.spots[n].zone_parent])]
    if candidates and not reachable:
        held = any(state.barriers[g].operator_override for g in zone_gate.values() if g in state.barriers)
        return {"plate": plate, "gate": gate_name, "target": None, "dispatched": False,
                "reason": "Held closed by operator" if held else "Barrier unavailable"}
    # Balance load: the zone with the lowest (occupied + reserved + broken +
    # under repair) / bays ratio, then the nearest suitable bay inside it.
    ratios = zones.zone_ratios(state.spots.values(), state.pending_repairs)
    zone = zones.pick_zone({state.spots[n].zone_parent for n in reachable}, ratios)
    ranked = rank_spots(gate_name, [n for n in reachable if state.spots[n].zone_parent == zone])
    target = target_spot if target_spot in reachable else (ranked[0][0] if ranked and not target_spot else None)
    result = {"plate": plate, "gate": gate_name, "target": target, "dispatched": False}
    if dry_run:
        return {**result, "ranked_candidates": ranked[:10]}
    if target is None:
        if not target_spot and _live_bays_synced:
            if await act(f"car {plate} -> leavepark (lot full)", lambda: client.car_goto(plate, "leavepark")):
                _record_neglect_and_discard(plate, "Turned away because no compatible bay was available")
            return {**result, "turned_away": True}
        return {**result, "reason": "spot unavailable"}
    if not settings.autopilot:
        await act(f"car {plate} -> {target}", lambda: client.car_goto(plate, target))
        return {**result, "reason": "autopilot disabled - no command sent"}
    if not state.reserve_spot(target, plate):
        return {**result, "reason": "spot no longer available"}
    if session is None:
        session = state.start_session(plate, gate=gate_name, car_type=car_type,
                                      planned_minutes=planned_minutes)
    state.assign_spot(plate, target)
    if _barrier_for_sensor(gate_name) == _entry_gate_for_session(session):
        session.reached_zone = True   # e.g. a ZONE1 car: ENTRY1 is its zone's own sensor
    _left_entry.discard(plate)
    _waiting_at.pop(plate, None)
    _holding_gate.pop(plate, None)
    _persist(plate)
    _start_dispatch_retry(plate, target)
    zone = state.spots[target].zone_parent
    note = f" ({zone} was {ratios.get(zone, 0):.0%} full)" if zone else ""
    state.log_activity(f"Dispatched {plate} from {gate_name} to {target}{note}")
    return {**result, "dispatched": True}


async def assign_specific_spot(plate: str, gate_name: str, spot_name: str,
                               car_type: str = "Normal") -> dict[str, Any]:
    return await dispatch_entry(plate, gate_name, car_type, target_spot=spot_name)


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
    counters = db.counters()
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
        "events": counters["events"],
        "unprocessed_events": counters["unprocessed_events"],
    }


@app.get("/api/history")
async def get_history(request: Request, limit: int = 100) -> list[dict[str, Any]]:
    """Completed parking sessions, for the dashboard history table."""
    return redact(db.query("SELECT * FROM sessions ORDER BY completed_at DESC LIMIT ?",
                    (max(1, min(limit, 500)),)), has(request.state.user, "fin:view"))


@app.get("/api/events")
async def get_events(request: Request, limit: int = 50, event_class: Optional[str] = None,
                     page: int = 1) -> list[dict[str, Any]]:
    from app.policy import EVENT_CAPABILITY, capabilities
    caps = capabilities(request.state.user)
    classes = [c for c, need in EVENT_CAPABILITY.items() if need in caps and (not event_class or c == event_class)]
    if not classes:
        return []
    limit = max(1, min(limit, 500))
    placeholders = ",".join("?" for _ in classes)
    rows = db.query(f"SELECT * FROM events WHERE event_class IN ({placeholders}) ORDER BY sequence_id DESC LIMIT ? OFFSET ?",
                    tuple(classes) + (limit, (max(1, page) - 1) * limit))
    return project_events(rows, request.state.user)


@app.get("/api/payments")
async def get_payments(limit: int = 100) -> list[dict[str, Any]]:
    return db.query("SELECT * FROM payments ORDER BY server_datetime DESC LIMIT ?",
                    (max(1, min(limit, 500)),))


@app.get("/api/signature-report")
async def get_signature_report() -> dict[str, Any]:
    """Which signature recipe the live simulator is actually using."""
    return {"attempts": db.signature_attempts(), "candidates": db.signature_trials()}


@app.get("/api/state")
async def get_state(request: Request) -> dict[str, Any]:
    return project_snapshot(state.snapshot(), request.state.user)


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


@app.post("/api/barriers/{name}/open")
@app.post("/api/manual/barrier/{name}/open")
async def manual_barrier_open(name: str, user: dict = Depends(auth.require_capability("ops:control_gates"))) -> dict[str, Any]:
    barrier = state.barriers.get(name)
    if barrier is None:
        raise HTTPException(404, "unknown barrier")
    if barrier.broken or barrier.under_maintenance or barrier.held_vehicles or name in state.pending_repairs:
        raise HTTPException(409, "Barrier unavailable or vehicle awaiting clearance")
    before = barrier.operator_override
    barrier.operator_override = False
    db.set_meta(f"gate_override:{name}", "0")
    sent = False
    if barrier.state != BarrierPosition.OPEN:
        sent = await act(f"operator opens {name}", lambda: client.barrier_open(name))
        if sent:
            state.update_barrier_state(name, "Opening")
            db.sync_component_wear(name, "BarrierGate", state.record_barrier_cycle(name), 0)
    auth.record_audit(user["username"], "POST", f"/api/barriers/{name}/open", 200, name,
                      {"before": {"operator_override": before}, "after": {"operator_override": False}})
    await _ensure_barriers_open()
    for session in list(state.sessions.values()):
        if session.paid and not session.released:
            await _release_paid(session)
    return {"ok": True, "name": name, "sent": sent, "operator_override": False}


@app.post("/api/barriers/{name}/close")
@app.post("/api/manual/barrier/{name}/close")
async def manual_barrier_close(name: str, user: dict = Depends(auth.require_capability("ops:control_gates"))) -> dict[str, Any]:
    barrier = state.barriers.get(name)
    if barrier is None:
        raise HTTPException(404, "unknown barrier")
    if barrier.broken or barrier.under_maintenance or name in state.pending_repairs:
        raise HTTPException(409, "Barrier unavailable")
    before = barrier.operator_override
    barrier.operator_override = True
    db.set_meta(f"gate_override:{name}", "1")
    sent = False
    if barrier.state not in (BarrierPosition.CLOSED, BarrierPosition.CLOSING):
        sent = await act(f"operator holds {name} closed", lambda: client.barrier_close(name))
        if sent:
            state.update_barrier_state(name, "Closing")
            db.sync_component_wear(name, "BarrierGate", state.record_barrier_cycle(name), 0)
    auth.record_audit(user["username"], "POST", f"/api/barriers/{name}/close", 200, name,
                      {"before": {"operator_override": before}, "after": {"operator_override": True}})
    return {"ok": True, "name": name, "sent": sent, "operator_override": True}


@app.post("/api/manual/repair/{name}")
async def manual_repair(name: str, user: dict = Depends(auth.require_maintenance)) -> dict[str, Any]:
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
        raise HTTPException(409, "Simulator lights have no repair command")
    else:
        await _queue_repair(component_type, name)  # counters reset when component_fixed arrives
    db.record_audit_log(user["username"], user["role"], "manual_repair", f"{component_type}:{name}")
    return {"ok": True, "name": name, "type": component_type, "queued": True}


@app.post("/api/manual/fan/{name}/on")
async def manual_fan_on(name: str, user: dict = Depends(auth.require_maintenance)) -> dict[str, Any]:
    if name not in state.fans:
        raise HTTPException(404, f"unknown fan {name}")
    if state.fans[name].broken or state.fans[name].under_maintenance or name in state.pending_repairs:
        raise HTTPException(409, "Fan unavailable")
    if not await act("manual fan on " + name, lambda: client.fan_on(name)):
        return {"ok": False, "sent": False, "name": name}
    cycles, runtime = state.set_fan_on(name, True)
    db.sync_component_wear(name, "ExhaustFan", cycles, runtime)
    state.log_activity(f"Operator switched on fan {name}")
    db.record_audit_log(user["username"], user["role"], "fan_on", name)
    return {"ok": True, "name": name, "is_on": True}


@app.post("/api/manual/fan/{name}/off")
async def manual_fan_off(name: str, user: dict = Depends(auth.require_maintenance)) -> dict[str, Any]:
    if name not in state.fans:
        raise HTTPException(404, f"unknown fan {name}")
    if state.fans[name].broken or state.fans[name].under_maintenance or name in state.pending_repairs:
        raise HTTPException(409, "Fan unavailable")
    if not await act("manual fan off " + name, lambda: client.fan_off(name)):
        return {"ok": False, "sent": False, "name": name}
    cycles, runtime = state.set_fan_on(name, False)
    db.sync_component_wear(name, "ExhaustFan", cycles, runtime)
    state.log_activity(f"Operator switched off fan {name}")
    db.record_audit_log(user["username"], user["role"], "fan_off", name)
    return {"ok": True, "name": name, "is_on": False}


@app.post("/api/manual/light/{name}/on")
async def manual_light_on(name: str, user: dict = Depends(auth.require_staff)) -> dict[str, Any]:
    light = state.lights.get(name)
    if light is None:
        raise HTTPException(404, "Unknown light")
    if light.broken or light.under_maintenance:
        raise HTTPException(409, "Light unavailable")
    if not await act("manual light on " + name, lambda: client.light_on(name)):
        return {"ok": False, "sent": False, "name": name}
    cycles, runtime = state.set_light_on(name, True)
    db.sync_component_wear(name, "Light", cycles, runtime)
    state.log_activity(f"Operator switched on light {name}")
    db.record_audit_log(user["username"], user["role"], "light_on", name)
    return {"ok": True, "name": name, "is_on": True}


@app.post("/api/manual/light/{name}/off")
async def manual_light_off(name: str, user: dict = Depends(auth.require_staff)) -> dict[str, Any]:
    light = state.lights.get(name)
    if light is None:
        raise HTTPException(404, "Unknown light")
    if light.broken or light.under_maintenance:
        raise HTTPException(409, "Light unavailable")
    if not await act("manual light off " + name, lambda: client.light_off(name)):
        return {"ok": False, "sent": False, "name": name}
    cycles, runtime = state.set_light_on(name, False)
    db.sync_component_wear(name, "Light", cycles, runtime)
    state.log_activity(f"Operator switched off light {name}")
    db.record_audit_log(user["username"], user["role"], "light_off", name)
    return {"ok": True, "name": name, "is_on": False}


@app.post("/api/ghost-cars/{ghost_id}/override")
async def ghost_car_override(ghost_id: int,
                              user: dict = Depends(auth.require_capability("ops:control_gates"))) -> dict[str, Any]:
    rows = db.query("SELECT * FROM ghost_car_events WHERE id = ? AND resolved = 0", (ghost_id,))
    if not rows:
        raise HTTPException(404, "no open ghost-car event")
    row = rows[0]
    if not settings.autopilot:
        await act(f"ghost override {row['plate']}", lambda: client.car_goto(row["plate"], "leavepark"))
        return {"ok": False, "sent": False, "ghost_id": ghost_id}
    plate = row["plate"]
    session = state.get_session(plate) or state.start_session(plate, gate="(ghost-override)")
    session.exit_gate = row["gate"]
    session.exit_confirmed = True
    session.expected_parking = float(row["fallback_charge"])
    session.expected_charging = 0.0
    session.expected_amount = session.expected_parking
    _persist(plate)
    if session.charge_attempted or not db.claim_charge(session):
        raise HTTPException(409, "Charge already attempted; awaiting payment or investigation")
    session.charge_attempted = True
    state.mark_charged(plate)
    _persist(plate)
    sent = await act(f"ghost invoice {plate}", lambda: client.car_charge(plate, session.expected_parking, 0.0))
    # Staff authorizes the fallback invoice, never fabricates payment.
    db.resolve_ghost_car(ghost_id, user["username"])
    auth.record_audit(user["username"], "POST", "/api/ghost-car/override", 200, plate,
                      {"before": {"resolved": False}, "after": {"resolved": True, "invoice_attempted": True}})
    return {"ok": sent, "ghost_id": ghost_id, "plate": plate, "awaiting_payment": True}


class GhostOverrideIn(BaseModel):
    ghost_id: int


@app.post("/api/ghost-car/override")
async def ghost_override_alias(body: GhostOverrideIn, user: dict = Depends(auth.require_capability("ops:control_gates"))):
    return await ghost_car_override(body.ghost_id, user)


class GhostReleaseIn(BaseModel):
    confirm_unpaid: bool = False


@app.post("/api/ghost-cars/{ghost_id}/release")
async def ghost_car_release(ghost_id: int, body: GhostReleaseIn,
                            user: dict = Depends(auth.require_capability("ops:control_gates"))) -> dict[str, Any]:
    """Staff let a held ghost car leave (4.25). A car that has not paid yet is
    only released with ``confirm_unpaid``: it would leave unpaid, and the
    simulator fines CarEscapedWithoutPaying."""
    rows = db.query("SELECT * FROM ghost_car_events WHERE id = ? AND resolved = 0", (ghost_id,))
    if not rows:
        raise HTTPException(404, "no open ghost-car event")
    plate, gate = rows[0]["plate"], rows[0]["gate"]
    session = state.get_session(plate)
    paid = bool(session and session.paid)
    if not paid and not body.confirm_unpaid:
        raise HTTPException(409, "Not paid yet. Releasing now lets the car leave unpaid "
                                 "(CarEscapedWithoutPaying) - confirm to release anyway.")
    if session is None:
        session = state.start_session(plate, gate="(ghost)")
        session.exit_gate, session.exit_confirmed, session.ghost_id = gate, True, ghost_id
    session.release_authorized = True
    _persist(plate)
    await _send_paid_release(session)
    db.resolve_ghost_car(ghost_id, user["username"])
    auth.record_audit(user["username"], "POST", f"/api/ghost-cars/{ghost_id}/release", 200, plate,
                      {"before": {"held": True, "paid": paid}, "after": {"released": session.released}})
    state.log_activity(f"Ghost car {plate} released by {user['username']}" + ("" if paid else " (unpaid)"),
                       level="info" if paid else "warn")
    await _broadcast_ghost(ghost_id, plate, gate, resolved=True)
    return {"ok": True, "ghost_id": ghost_id, "plate": plate, "paid": paid, "released": session.released}


@app.get("/api/ghost-cars")
async def list_ghost_cars(resolved: Optional[bool] = None,
                          user: dict = Depends(auth.require_staff)) -> list[dict[str, Any]]:
    where = " WHERE resolved = ?" if resolved is not None else ""
    rows = db.query("SELECT * FROM ghost_car_events" + where + " ORDER BY occurred_at DESC LIMIT 200",
                    (int(resolved),) if resolved is not None else ())
    for row in rows:
        row.update(_ghost_status(row["plate"]))   # status for the banner; amounts stay fin:view only
    return redact(rows, has(user, "fin:view"))


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
    revenue section is computed only with fin:view.
    """
    # Durable zone names preserve historical throughput across layout changes.
    throughput_by_zone = db.query(
        """SELECT COALESCE(zone, 'unknown') AS zone, COUNT(*) AS sessions
           FROM sessions WHERE date(completed_at) = date('now') GROUP BY zone ORDER BY sessions DESC LIMIT 100"""
    )

    light_off_events = db.query(
        "SELECT COUNT(*) AS n FROM component_events WHERE type = 'Light' AND event = 'off' AND date(occurred_at) = date('now')"
    )[0]["n"]
    energy_conserved_wh_estimate = round(
        light_off_events * settings.light_watts_estimate * (settings.environment_loop_interval_s / 3600.0), 2
    )

    co_mitigation_events = db.query(
        "SELECT COUNT(*) AS n FROM component_events WHERE type = 'ExhaustFan' AND event = 'on' AND date(occurred_at) = date('now') "
    )[0]["n"]

    preventive = db.query(
        "SELECT COUNT(*) AS n FROM component_events WHERE event = 'fixed_proactive' AND date(occurred_at) = date('now')"
    )[0]["n"]
    reactive = db.query(
        "SELECT COUNT(*) AS n FROM component_events WHERE event = 'broken' AND date(occurred_at) = date('now')"
    )[0]["n"]

    out: dict[str, Any] = {
        "date_basis": "UTC receipt date",
        "report_date": datetime.now(timezone.utc).date().isoformat(),
        "throughput_by_zone": throughput_by_zone,
        "energy_conserved_wh_estimate": energy_conserved_wh_estimate,
        "energy_conserved_note": "Estimate: light-off actions x assumed wattage x tick interval - not a real meter.",
        "co_mitigation_events": co_mitigation_events,
        "preventive_repairs": preventive,
        "unexpected_breakdowns": reactive,
        "ghost_car_events_open": db.query(
            "SELECT COUNT(*) AS n FROM ghost_car_events WHERE resolved = 0")[0]["n"],
    }

    if has(user, "fin:view"):
        paid = db.query("""SELECT COALESCE(SUM(p.amount), 0) AS r
            FROM payments p LEFT JOIN events e ON e.event_id = p.event_id
            WHERE p.valid = 1 AND date(COALESCE(e.received_at, p.server_datetime)) = date('now')""")[0]["r"]
        fines = db.query("""SELECT COALESCE(SUM(p.fine_amount), 0) AS f
            FROM penalties p LEFT JOIN events e ON e.event_id = p.event_id
            WHERE date(COALESCE(e.received_at, p.server_datetime)) = date('now')""")[0]["f"]
        repair_costs = db.query("""SELECT COALESCE(SUM(amount), 0) AS r
            FROM component_events
            WHERE event IN ('fixed_proactive', 'fixed_reactive')
              AND date(occurred_at) = date('now')""")[0]["r"]
        out["revenue"] = {
            "paid_total": round(paid, 2),
            "penalty_total": round(fines, 2),
            "repair_cost_total": round(repair_costs, 2),
            "net_revenue": round(paid - fines - repair_costs, 2),
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
        await ws.send_json(project_snapshot({"type": "hello", "server_time": time.time(), **state.snapshot()}, ws.state.user))
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
        await ws.send_json(project_snapshot({"type": "hello", "server_time": time.time(), **state.snapshot()}, ws.state.user))
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
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "Webhook must contain valid JSON") from None
    if not isinstance(payload, dict):
        raise HTTPException(400, "Webhook must be an object")
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
        status = db.event_status(event_id) if event_id else None
        if not status or status["processed"] or event_id in _webhook_events_in_progress:
            return JSONResponse({"status": "duplicate", "event_id": event_id})

    if event_id:
        state.is_duplicate(event_id)  # keep the hot-path cache aligned

    sequence_id = payload.get("SequenceId")
    try:
        sequence_id = int(sequence_id) if sequence_id is not None else None
    except (TypeError, ValueError):
        sequence_id = None
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
    if event_id:
        _webhook_events_in_progress.add(event_id)
    try:
        if handler is None:
            log.info("unhandled EventClass=%s (EventId=%s)", event_class, event_id)
            db.mark_processed(event_id, "unhandled event class")
            return JSONResponse({"status": "ignored", "event_class": event_class})
        try:
            await handler(payload)
            db.mark_processed(event_id)
            if event_class == "car_spot_action":
                try:   # a car moved: relight at once rather than at the next environment tick
                    await _refresh_lights(payload.get("ServerDateTime"))
                except Exception:  # noqa: BLE001 - lighting must never fail a webhook
                    log.exception("light refresh failed")
        except Exception as exc:  # noqa: BLE001 - retain failed events for redelivery
            log.exception("handler failed for EventClass=%s EventId=%s", event_class, event_id)
            db.mark_failed(event_id, str(exc))
            return JSONResponse({"status": "error", "event_class": event_class}, status_code=500)
        return JSONResponse({"status": "processed", "event_class": event_class})
    finally:
        if event_id:
            _webhook_events_in_progress.discard(event_id)


# --------------------------------------------------------------------------- #
# Maintenance queue helper
# --------------------------------------------------------------------------- #
async def _queue_repair(component_type: str, name: str, *, via_zone: bool = False) -> None:
    component = state.spots.get(name) or state.barriers.get(name) or state.fans.get(name)
    if name in state.pending_repairs or (component and component.under_maintenance):
        return
    if component_type == "BarrierGate" and not via_zone:
        zone = _zone_of_gate(name)
        if zone:
            # 4.26: a zone gate is repaired as part of closing the whole zone.
            # A broken gate goes now; a healthy one waits for its turn.
            _start_zone_maintenance(zone, name)
            if not (component and component.broken):
                await _schedule_gate_repairs()
                return
    if component_type == "BarrierGate":
        holder = _gate_repair_slot_holder()
        if holder and holder != name:
            return   # one gate in repair at a time (4.28); _schedule_gate_repairs retries
        if component and not component.broken and settings.autopilot:
            db.set_meta(f"pending_proactive_repair:{name}", "1")   # logged as fixed_proactive
            db.record_component_event(name, component_type, "repair_triggered_proactive")
    if component_type == "ParkingSpot":
        action: Callable[[], Awaitable[None]] = lambda: client.spot_repair(name)
    elif component_type == "BarrierGate":
        action = lambda: client.barrier_repair(name)
    elif component_type == "ExhaustFan":
        action = lambda: client.fan_repair(name)
    else:
        return
    if component_type == "ExhaustFan" and component is not None:
        for other in state.fans.values():
            if (other.name != name and other.zone_parent == component.zone_parent
                    and (other.under_maintenance or other.name in state.pending_repairs)):
                return   # one fan per zone in repair: the zone keeps ventilating (4.31)
    if component_type == "ParkingSpot":
        spot = state.spots.get(name)
        if spot and (spot.occupant_plate or spot.status in (SpotStatus.OCCUPIED, SpotStatus.RESERVED)):
            state.queue_deferred_repair(name, component_type)
            return
    state.pending_repairs[name] = component_type

    async def dropped():
        state.pending_repairs.pop(name, None)
        db.set_meta(f"pending_proactive_repair:{name}", "0")
        db.record_component_event(name, component_type, "repair_failed")
        state.log_activity(f"Repair failed for {name}; will retry on the next maintenance sweep",
                           level="error", capability="logs:view_maint")

    async def guarded_repair():
        component = state.spots.get(name) or state.barriers.get(name) or state.fans.get(name)
        if component and component.under_maintenance:
            state.pending_repairs.pop(name, None)
            return
        if component_type == "ParkingSpot":
            spot = state.spots.get(name)
            if spot and (spot.occupant_plate or spot.status in (SpotStatus.OCCUPIED, SpotStatus.RESERVED)):
                state.queue_deferred_repair(name, component_type)
                state.pending_repairs.pop(name, None)
                return
        if component_type == "BarrierGate" and component and not component.broken:
            if component.state in (BarrierPosition.OPENING, BarrierPosition.CLOSING):
                raise RuntimeError("wait for gate movement to finish before repair")
            if component.operator_override or component.held_vehicles:
                raise RuntimeError("gate is in operational use; postpone preventive repair")
        if component_type == "ExhaustFan" and component and component.is_on and not component.broken:
            zone = state.zones.get(component.zone_parent)
            if zone and zone.gas_co_level >= settings.co_fan_off_threshold:
                raise RuntimeError("fan is required for unsafe CO; postpone preventive repair")
            stopped = await act(f"stop fan {name} before repair", lambda: client.fan_off(name))
            if settings.autopilot and not stopped:
                raise RuntimeError("could not stop fan before repair")
            if stopped:
                cycles, runtime = state.set_fan_on(name, False)
                db.sync_component_wear(name, component_type, cycles, runtime)
                db.record_component_event(name, component_type, "off")
        sent = await act(f"repair {component_type}:{name}", action)
        if settings.autopilot and not sent:
            raise RuntimeError("repair command failed")
        if sent:
            component = state.spots.get(name) or state.barriers.get(name) or state.fans.get(name)
            if component:
                component.under_maintenance = True
                if component_type == "ParkingSpot":
                    component.status = SpotStatus.MAINTENANCE
            db.record_component_event(name, component_type, "repair_started")
        state.pending_repairs.pop(name, None)
    try:
        await maintenance_queue.submit(f"repair {component_type}:{name}", guarded_repair, priority=10, on_drop=dropped)
    except Exception:
        await dropped()
        raise


# --------------------------------------------------------------------------- #
# Event handlers
# --------------------------------------------------------------------------- #
async def _handle_car_spot_action(payload: dict[str, Any]) -> None:
    plate = payload["CarPlateNumber"]
    spot_name = payload["SpotName"]
    spot_type = payload.get("SpotType", "")
    direction = payload.get("Direction", "")

    if spot_type == "EntrySpot" and direction == "CarIn":
        _retire_previous_visit(plate)
    if spot_type == "EntrySpot" and direction == "CarIn" and plate in state.active_dispatches:
        session = state.get_session(plate)
        if session is not None and _barrier_for_sensor(spot_name) == _entry_gate_for_session(session):
            session.reached_zone = True   # at its own zone's sensor: that zone lights up
        if session is not None and session.staged_via == spot_name and session.phase == SessionPhase.ASSIGNED:
            # The car has reached its zone's own sensor and is waiting at the
            # closed zone gate: open it now and send the car on to its bay.
            _waiting_at[plate] = spot_name
            _left_entry.discard(plate)
            session.entry_departed = False
            _persist(plate)
            _start_dispatch_retry(plate, session.assigned_spot, sensor=spot_name)
            return
        log.info("idempotent entrance short-circuit for %s (%s)", plate, state.active_dispatches[plate])
        return
    await _sync_if_no_live_bays()

    if spot_type == "EntrySpot" and direction == "CarOut":
        session = state.get_session(plate)
        if session:
            if session.assigned_spot is None:
                _record_neglect_and_discard(plate, "Left entrance while access was unavailable")
                return
            _left_entry.add(plate)
            session.entry_departed = True
            _persist(plate)
            if _holding_gate.get(plate) and _holding_gate[plate] == _barrier_for_sensor(spot_name):
                # Out of the box in front of its gate: the car is going through.
                # Close behind it unless the next car already holds the gate.
                _holding_gate.pop(plate, None)
                _spawn(_close_idle_gates_later(settings.entry_gate_close_delay_s))
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
        _persist(plate)
        state.log_activity(f"{plate} parked at {spot_name}")
        _holding_gate.pop(plate, None)   # backstop if the sensor exit was missed
        await _close_idle_gates()
        await _schedule_gate_repairs()   # a car inbound to a closed zone has arrived
        return

    if spot_type == "Park" and direction == "CarOut":
        state.mark_left_spot(plate)
        _persist(plate)
        state.mark_spot_vacant(spot_name)
        state.log_activity(f"{plate} vacated {spot_name}")
        ready = state.pop_ready_repair(spot_name)
        if ready:
            await _queue_repair(ready, spot_name)
            log.info("deferred repair for %s now queued after vacancy", spot_name)
        return

    if spot_type == "ExitSpot" and direction == "CarIn":
        session = state.get_session(plate)
        if session is None or ((session.entry_gate == "(adopted)" or session.ghost_id) and _valid_exit(session)):
            # Never seen at an entrance: either unknown here, or it appeared
            # straight into a bay ("adopted"). Both are ghost cars (4.25).
            await _handle_ghost_car(plate, spot_name, payload)
            return
        if not _valid_exit(session):
            log.info("ignored route-crossing exit sensor for %s at %s", plate, spot_name)
            return
        if session.charged:
            return
        session.exit_confirmed = True
        state.mark_at_exit(plate)
        session.exit_gate = spot_name
        # Charging inline here is too early - see _charge_at_exit, which logs
        # the amount once it has actually computed and sent it.
        _persist(plate)
        _spawn(_charge_at_exit(plate, spot_name))
        return

    if spot_type == "ExitSpot" and direction == "CarOut":
        session = state.get_session(plate)
        if session is None or not session.exit_confirmed or not _valid_exit(session):
            return
        _archive(plate)
        state.log_activity(f"{plate} left the facility via {spot_name}")
        log.info("%s left the facility via %s", plate, spot_name)
        _spawn(_close_idle_gates_later())   # no sensor past the exit gate: give the car time to clear it
        await _schedule_gate_repairs()   # the zone this car left may now be empty
        return


def _valid_exit(session) -> bool:
    return ((session.parked_at is not None or session.left_spot_at is not None
             or session.entry_gate == "(ghost-override)" or session.ghost_id)
            and time.monotonic() - session.created_at >= settings.min_dwell_time_s / max(settings.game_speed, 0.1))


def _archive(plate: str) -> None:
    """Move a finished session out of memory and into SQLite for the dashboard."""
    session = state.get_session(plate)
    if session is None:
        return
    db.record_session({
        "plate": session.plate,
        "session_id": session.session_id,
        "zone": session.zone,
        "car_type": session.car_type,
        "spot": session.assigned_spot,
        "entry_gate": session.entry_gate,
        "exit_gate": session.exit_gate,
        "arrived_at": session.arrived_wall or None,
        "parked_at": session.parked_wall or None,
        "left_spot_at": session.left_spot_wall or None,
        "minutes": round(session.measured_minutes * settings.game_speed, 2),
        "planned_minutes": session.planned_minutes,
        "parking_cost": session.expected_parking,
        "charging_cost": session.expected_charging,
        "paid_amount": session.expected_amount if session.paid else None,
        "payment_ok": 1 if session.paid else 0 if session.payment_suspect else None,
    })
    state.complete_session(plate)
    _left_entry.discard(plate)
    _waiting_at.pop(plate, None)
    _holding_gate.pop(plate, None)


def _record_neglect_and_discard(plate: str, reason: str) -> None:
    """Persist an unserved arrival without counting it as completed throughput."""
    session = state.get_session(plate)
    if session is None:
        return
    db.record_neglect(session, reason)
    state.neglected_vehicles.appendleft({"plate": plate, "gate": session.entry_gate, "reason": reason})
    state.log_activity(f"Neglected vehicle {plate}: {reason}", level="warn")
    state.complete_session(plate)
    db.delete_active_session(session.session_id)
    _left_entry.discard(plate)
    _waiting_at.pop(plate, None)
    _holding_gate.pop(plate, None)


async def _charge_at_exit(plate: str, spot_name: str) -> None:
    session = state.get_session(plate)
    if session is None:
        return
    identity = session.session_id
    await asyncio.sleep(settings.exit_charge_delay_s / max(settings.game_speed, 0.1))
    session = state.get_session(plate)
    if session is None or session.session_id != identity or session.paid or session.charge_attempted:
        return
    parking, charging, minutes = compute_charge(session)
    session.expected_parking = parking
    session.expected_charging = charging
    session.expected_amount = round(parking + charging, 2)
    _persist(plate)
    if not settings.autopilot:
        await act(f"charge {plate}", lambda: client.car_charge(plate, parking, charging))
        return
    if not db.claim_charge(session):
        session.charge_attempted = True
        return
    session.charge_attempted = True
    state.mark_charged(plate)
    _persist(plate)
    sent = await act(f"charge {plate} parking={parking} charging={charging}",
                     lambda: client.car_charge(plate, parking, charging))
    state.log_activity(f"Invoice {'sent' if sent else 'attempt failed; staff review required'} for {plate}: {session.expected_amount:.2f}",
                       capability="logs:view_fin")


async def _hold_exit(plate: str, sensor: str) -> None:
    name = _barrier_for_sensor(sensor)
    if not name:
        state.log_activity(f"Unable to map exit sensor {sensor} to a barrier; review {plate}", level="error")
        return
    barrier = state.barriers[name]
    barrier.held_vehicles.add(plate)
    db.set_meta(f"gate_holds:{name}", json.dumps(sorted(barrier.held_vehicles)))
    if (not barrier.broken and not barrier.under_maintenance and name not in state.pending_repairs
            and barrier.state not in (BarrierPosition.CLOSED, BarrierPosition.CLOSING)):
        if await act(f"hold exit {name} for {plate}", lambda: client.barrier_close(name)):
            state.update_barrier_state(name, "Closing")
            db.sync_component_wear(name, "BarrierGate", state.record_barrier_cycle(name), 0)


async def _handle_ghost_car(plate: str, exit_spot: str, payload: dict[str, Any]) -> None:
    """A car that was never seen at an entrance reached an exit (4.25).

    Either it is unknown here, or it appeared straight into a bay (an
    "adopted" session). It is billed automatically with the ML-estimated fee
    (``POST /car/{plate}/charge`` is the simulator's "ask car for payment"),
    its exit barrier is held closed, and a payment does NOT release it: staff
    release it from the dashboard banner (POST /api/ghost-cars/{id}/release).
    """
    try:
        fee = ml_agent.ghost_car_anomaly_imputation(plate, payload.get("CarType", "Normal"))
    except Exception:  # noqa: BLE001 - the hold must proceed even if imputation fails
        log.exception("ghost_car_anomaly_imputation failed for %s; using median/flat fallback", plate)
        fee = db.median_parking_cost()
        if fee is None:
            fee = round(settings.unknown_car_minutes * settings.parking_rate_per_minute, 2)
    ghost_id = db.record_ghost_car(plate, exit_spot, fee)
    await _hold_exit(plate, exit_spot)
    session = state.get_session(plate) or state.start_session(
        plate, gate="(ghost)", car_type=payload.get("CarType", "Normal"))
    session.ghost_id = ghost_id
    session.exit_gate = exit_spot
    session.exit_confirmed = True
    if not session.charge_attempted:
        session.expected_parking = float(fee)
        session.expected_charging = 0.0
        session.expected_amount = session.expected_parking
    state.mark_at_exit(plate)
    _persist(plate)
    state.log_activity(f"Ghost car {plate} at {exit_spot}: never scanned at an entrance - "
                       f"billing the estimated fee and holding it for staff release", level="error")
    log.error("GHOST CAR %s at %s (ghost_id=%d, fee=%.2f) - invoiced and held for staff",
              plate, exit_spot, ghost_id, fee)
    _spawn(_invoice_ghost(plate))
    await _broadcast_ghost(ghost_id, plate, exit_spot)


async def _invoice_ghost(plate: str) -> None:
    session = state.get_session(plate)
    if session is None:
        return
    identity = session.session_id
    # Same settling wait as a normal exit invoice: a charge sent on the CarIn
    # itself is rejected with "Car is not waiting at the exit".
    await asyncio.sleep(settings.exit_charge_delay_s / max(settings.game_speed, 0.1))
    session = state.get_session(plate)
    if session is None or session.session_id != identity or session.charge_attempted or session.paid:
        return
    fee = session.expected_parking or 0.0
    if not settings.autopilot:
        await act(f"ghost invoice {plate}", lambda: client.car_charge(plate, fee, 0.0))
        return
    if not db.claim_charge(session):   # at most one invoice per visit (ChargeCarForParkingTwice)
        session.charge_attempted = True
        return
    session.charge_attempted = True
    state.mark_charged(plate)
    _persist(plate)
    sent = await act(f"ghost invoice {plate} {fee:.2f}", lambda: client.car_charge(plate, fee, 0.0))
    state.log_activity(f"Ghost car {plate} invoiced {fee:.2f}" if sent else
                       f"Ghost car {plate}: invoice failed - staff review required",
                       level="info" if sent else "error", capability="logs:view_fin")
    if session.ghost_id:
        await _broadcast_ghost(session.ghost_id, plate, session.exit_gate)


def _ghost_status(plate: str) -> dict[str, Any]:
    session = state.get_session(plate)
    return {"invoiced": bool(session and session.charge_attempted),
            "payment_received": bool(session and session.paid)}


async def _broadcast_ghost(ghost_id: int, plate: str, gate: Optional[str], *, resolved: bool = False) -> None:
    try:
        await manager.broadcast({
            "type": "alert", "alert_type": "GHOST_CAR_RESOLVED" if resolved else "UNREGISTERED_VEHICLE_EXIT",
            "plate": plate, "gate": gate, "ghost_id": ghost_id, **_ghost_status(plate),
            "server_time": time.time(),
        })
    except Exception:  # noqa: BLE001 - a broadcast failure must not lose the hold
        log.exception("ghost-car broadcast failed for %s", plate)

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
    state.log_activity(f"{component_type} {name} broken", level="warn", capability="logs:view_maint")
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
    state.pending_repairs.pop(name, None)
    db.mark_component_repaired(name)
    was_proactive = db.get_meta(f"pending_proactive_repair:{name}") == "1"
    db.set_meta(f"pending_proactive_repair:{name}", "0")
    try:
        repair_cost = float(payload.get("RepairCost")) if payload.get("RepairCost") is not None else None
    except (TypeError, ValueError):
        repair_cost = None
    db.record_component_event(name, component_type,
                              "fixed_proactive" if was_proactive else "fixed_reactive",
                              amount=repair_cost)
    state.log_activity(f"{component_type} {name} fixed", capability="logs:view_maint")
    log.info("component fixed: %s %s", component_type, name)
    if component_type == "ExhaustFan" and name in state.fans:
        zone = state.zones.get(state.fans[name].zone_parent)
        if zone:
            await _handle_carbon_monoxide_event({"ZoneName": zone.name,
                "CarbonMonoxideLevel": zone.gas_co_level, "DangerLevel": zone.danger_level})
    elif component_type == "BarrierGate":
        for zone, info in list(state.zone_maintenance.items()):
            info["todo"].discard(name)
            if not info["todo"]:
                state.zone_maintenance.pop(zone, None)
                state.log_activity(f"{zone} reopened: its gates are repaired", capability="logs:view_maint")
        await _schedule_gate_repairs()
        await _ensure_barriers_open()


async def _handle_carbon_monoxide_event(payload: dict[str, Any]) -> None:
    zone_name = payload["ZoneName"]
    co_level = float(payload.get("CarbonMonoxideLevel", 0.0))
    danger_level = payload.get("DangerLevel", "Safe")
    state.update_zone(zone_name, co_level, danger_level)
    state.log_activity(f"CO {danger_level} in {zone_name} ({co_level:.1f})",
                       level="warn" if danger_level in ("High", "Critical") else "info")
    log.warning("CO event in %s: level=%.2f danger=%s", zone_name, co_level, danger_level)

    # Fixed hysteresis (4.23): ON above CO_FAN_ON_THRESHOLD (50), then keep
    # ventilating down to CO_FAN_OFF_THRESHOLD (15). The wide band leaves
    # headroom for a fan to be repaired before CO climbs back to 50. This
    # replaces the occupancy-dependent ML edges merged from main (4.22).
    for fan_name in state.fans_in_zone(zone_name):
        fan = state.fans[fan_name]
        if fan.broken or fan.under_maintenance or fan_name in state.pending_repairs:
            continue
        if not fan.is_on and co_level > settings.co_fan_on_threshold:
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
    if name in state.barriers:
        db.sync_component_wear(name, "BarrierGate", state.barriers[name].cycle_count, 0)

    barrier = state.barriers.get(name)
    if barrier and action == "Open" and (barrier.operator_override or barrier.held_vehicles):
        if barrier.broken or barrier.under_maintenance or name in state.pending_repairs:
            state.log_activity(f"Cannot enforce hold on unavailable gate {name}", level="error")
            return
        if await act(f"enforce hold on {name}", lambda n=name: client.barrier_close(n)):
            state.update_barrier_state(name, "Closing")
            db.sync_component_wear(name, "BarrierGate", state.record_barrier_cycle(name), 0)


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
    # Corrections are evidence for review, never a second charge or a changed invoice.
    state.log_activity(f"Charge discrepancy for {payload.get('ComponentName')}: sent {sent:.2f}, expected {should_be:.2f}; review required",
                       level="warn", capability="logs:view_fin")


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
    valid = bool(session and session.charge_attempted and expected is not None and math.isfinite(amount)
                 and abs(amount - expected) <= 0.01)

    if payload.get("EventId"):
        db.record_payment(
            event_id=payload["EventId"], plate=plate, amount=amount, expected=expected,
            valid=valid, reason=payload.get("Reason"),
            server_datetime=payload.get("ServerDateTime"),
        )

    if not valid:
        if session:
            session.payment_suspect = True
            _persist(plate)
        shown = "unknown" if expected is None else f"{expected:.2f}"
        state.log_activity(f"SUSPECT PAYMENT {plate}: expected {shown}, got {amount:.2f}", level="warn", capability="logs:view_fin")
        if session and session.exit_gate:
            await _hold_exit(plate, session.exit_gate)
        state.log_activity(f"Vehicle {plate} held at exit for review", level="warn")
        log.warning("SUSPECT PAYMENT %s: reported %.2f, expected %s - holding at exit",
                    plate, amount, shown)
        return

    if session.paid:
        # Payment is durable before release. A duplicate can safely resume a
        # failed release without issuing a second invoice or recording payment twice.
        await _release_paid(session)
        return
    if not state.mark_paid(plate, amount):
        log.warning("unsolicited or duplicate payment_made for %s (amount %.2f) - ignored", plate, amount)
        return

    if session.ghost_id:
        state.log_activity(f"Ghost car {plate} paid ({amount:.2f}) - waiting for staff to release it",
                           capability="logs:view_fin")
        _persist(plate)
        await _broadcast_ghost(session.ghost_id, plate, session.exit_gate)
        return
    state.log_activity(f"Payment accepted for {plate} ({amount:.2f}) - releasing", capability="logs:view_fin")
    _persist(plate)
    await _release_paid(session)


_releases_in_progress: set[str] = set()


async def _release_paid(session) -> None:
    if session.released or not session.paid or session.session_id in _releases_in_progress:
        return
    if session.ghost_id and not session.release_authorized:
        return   # a ghost car leaves only when staff release it (4.25)
    # Recovery may run recursively inside a simulator request after HTTP 401.
    # Do not wait for that same request, or start a competing release command.
    _releases_in_progress.add(session.session_id)
    try:
        await _send_paid_release(session)
    finally:
        _releases_in_progress.discard(session.session_id)


async def _send_paid_release(session) -> None:
    plate = session.plate
    gate = _barrier_for_sensor(session.exit_gate or "")
    if gate:
        barrier = state.barriers[gate]
        barrier.held_vehicles.discard(plate)
        db.set_meta(f"gate_holds:{gate}", json.dumps(sorted(barrier.held_vehicles)))
        if barrier.operator_override or barrier.held_vehicles or barrier.broken or barrier.under_maintenance or gate in state.pending_repairs:
            return
        if barrier.state not in (BarrierPosition.OPEN, BarrierPosition.OPENING):
            if not await act(f"release paid vehicle at {gate}", lambda: client.barrier_open(gate)):
                return
            state.update_barrier_state(gate, "Opening")
            db.sync_component_wear(gate, "BarrierGate", state.record_barrier_cycle(gate), 0)
        # leavepark is routed at once; sent while the gate is still rising the
        # simulator finds "No valid escape spot" and the paid car sits there.
        await _wait_for_barrier_open(gate)
    if await act(f"car {plate} -> leavepark", lambda: client.car_goto(plate, "leavepark")):
        session.released = True
        _persist(plate)



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
    state.log_activity(f"PENALTY: {reason} (-{fine})", level="error", capability="logs:view_fin")
    if "neglect" in reason.lower():
        plate, session = _find_session_by_loose_plate(component_name)
        if session and session.parked_at is None:
            db.record_neglect(session, "Simulator reported entry neglect")
            state.neglected_vehicles.appendleft({"plate": plate, "gate": session.entry_gate, "reason": "Simulator reported entry neglect"})
            if session.assigned_spot:
                state.release_reservation(session.assigned_spot, plate)
            _archive(plate)
    log.error("PENALTY: %s - fine %s (%s %s)", reason, fine, component_type, component_name)

    # Keep simulator corrections as evidence; never resend a charge.
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
