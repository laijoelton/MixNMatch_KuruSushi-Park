"""In-memory, thread-safe mirror of Grand Park Auto simulator state.

Per the organizer's API guidelines, list-* endpoints must be called only
once at startup (or after a crash) - all state after that point is derived
exclusively from inbound webhook events. This module is the single source
of truth the rest of the service reads and writes.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from app.config import settings


def _utcnow() -> str:
    """Wall-clock stamp for the dashboard; monotonic time cannot be a date."""
    return datetime.now(timezone.utc).isoformat()


def _extract_plate(entry) -> Optional[str]:
    """A detectedCars list entry may be a bare plate string or an object."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("plate") or entry.get("CarPlateNumber") or entry.get("name")
    return None


class SpotStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    OCCUPIED = "OCCUPIED"
    BROKEN = "BROKEN"
    MAINTENANCE = "MAINTENANCE"


class BarrierPosition(str, Enum):
    OPEN = "Open"
    CLOSED = "Closed"
    OPENING = "Opening"
    CLOSING = "Closing"


class SessionPhase(str, Enum):
    ARRIVED = "ARRIVED"
    ASSIGNED = "ASSIGNED"
    PARKED = "PARKED"
    EXIT_REQUESTED = "EXIT_REQUESTED"
    AT_EXIT = "AT_EXIT"
    CHARGED = "CHARGED"
    COMPLETED = "COMPLETED"


@dataclass
class Spot:
    name: str
    purpose: str = "Park"
    parking_for_car_type: str = "Any"
    zone_parent: str = ""
    status: SpotStatus = SpotStatus.AVAILABLE
    broken: bool = False
    under_maintenance: bool = False
    occupant_plate: Optional[str] = None

    # When the spot was promised to a car, so a reservation for a car that
    # never arrives can be swept instead of holding the spot forever.
    reserved_at: Optional[float] = None

    @property
    def dispatchable(self) -> bool:
        return (
            self.purpose == "Park"
            and self.status == SpotStatus.AVAILABLE
            and not self.broken
            and not self.under_maintenance
        )


@dataclass
class Barrier:
    name: str
    zone_parent: str = ""
    state: BarrierPosition = BarrierPosition.CLOSED
    broken: bool = False
    under_maintenance: bool = False
    # Wear: one cycle per open/close command sent to the simulator.
    cycle_count: int = 0


@dataclass
class ExhaustFan:
    name: str
    zone_parent: str = ""
    is_on: bool = False
    broken: bool = False
    under_maintenance: bool = False
    # Wear: one cycle per on/off toggle, plus accumulated seconds spent on.
    cycle_count: int = 0
    runtime_seconds: float = 0.0
    turned_on_at: Optional[float] = None


@dataclass
class Light:
    name: str
    zone_parent: str = ""
    is_on: bool = True
    cycle_count: int = 0
    runtime_seconds: float = 0.0
    turned_on_at: Optional[float] = field(default_factory=time.monotonic)


@dataclass
class Zone:
    name: str
    gas_co_level: float = 0.0
    risk: str = "Safe"
    danger_level: str = "Safe"


@dataclass
class VehicleSession:
    plate: str
    entry_gate: str
    phase: SessionPhase = SessionPhase.ARRIVED
    assigned_spot: Optional[str] = None
    exit_gate: Optional[str] = None
    expected_amount: Optional[float] = None
    charged: bool = False
    paid: bool = False
    created_at: float = field(default_factory=time.monotonic)

    # What the driver booked. The simulator bills this, not the wall-clock
    # time we observe -- see compute_charge().
    planned_minutes: float = 0.0

    # Car type decides the electric surcharge, and a car billed for
    # electricity it never used incurs Penalty_ChargeCarForNoElectricityUsed.
    car_type: str = "Normal"

    # Billing runs from the moment the car occupies the spot, not from when it
    # reached the entry -- the drive in is not parking time.
    parked_at: Optional[float] = None
    left_spot_at: Optional[float] = None

    # Split of the last computed charge, kept for payment validation.
    expected_parking: Optional[float] = None
    expected_charging: Optional[float] = None

    # Wall-clock stamps for the dashboard (monotonic is not a date).
    arrived_wall: str = ""
    parked_wall: str = ""
    left_spot_wall: str = ""

    @property
    def is_electric(self) -> bool:
        return self.car_type.strip().lower() == "electric"

    @property
    def billable_minutes(self) -> float:
        """Minutes between parking and leaving the spot (or now, if still in)."""
        if self.parked_at is None:
            return 0.0
        end = self.left_spot_at if self.left_spot_at is not None else time.monotonic()
        return max(0.0, (end - self.parked_at) / 60.0)


class _BoundedEventCache:
    """Fixed-capacity FIFO set used to dedup inbound ``EventId`` values."""

    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, capacity)
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._lock = threading.RLock()

    def seen_before(self, event_id: str) -> bool:
        with self._lock:
            if event_id in self._seen:
                self._seen.move_to_end(event_id)
                return True
            self._seen[event_id] = None
            if len(self._seen) > self._capacity:
                self._seen.popitem(last=False)
            return False


class ParkingState:
    """Central, lock-protected snapshot of everything the dispatcher needs."""

    def __init__(self, max_processed_events: int = 5000) -> None:
        self._lock = threading.RLock()
        self.spots: dict[str, Spot] = {}
        self.barriers: dict[str, Barrier] = {}
        self.fans: dict[str, ExhaustFan] = {}
        self.lights: dict[str, Light] = {}
        self.zones: dict[str, Zone] = {}
        self.sessions: dict[str, VehicleSession] = {}
        self.last_sequence_id: int = 0
        self.deferred_repairs: dict[str, str] = {}
        self.penalty_count: int = 0
        self.total_fines: float = 0.0
        self.penalty_log: deque = deque(maxlen=200)
        self.activity_log: deque = deque(maxlen=200)
        self.started_at: float = time.time()
        self._events = _BoundedEventCache(max_processed_events)

    # ------------------------------------------------------------------ #
    # Bootstrap (called once at startup from the list-* REST responses)
    # ------------------------------------------------------------------ #
    def load_spots(self, raw: list[dict]) -> None:
        """Load spots from list-parking-spots.

        The documented sample shows ``detectedCars`` as a list (``[]`` when
        empty, presumably plate strings or car objects when occupied). The
        live simulator instead returns a plain integer count (``0`` or
        ``1``+). Both shapes are handled: a list yields a plate when one is
        present, an int yields only occupancy, not an identity -- the plate
        becomes known on the next ``Park/CarIn`` webhook, same as it would for
        a spot the dispatcher did not reserve itself.
        """
        with self._lock:
            for item in raw:
                # The live simulator reports this as either a plate-name list
                # or a bare occupancy count depending on build - handle both.
                detected = item.get("detectedCars")
                if isinstance(detected, list):
                    occupied = bool(detected)
                    plate = _extract_plate(detected[0]) if detected else None
                elif isinstance(detected, (int, float)):
                    occupied = detected > 0
                    plate = None
                else:
                    occupied = False
                    plate = None

                self.spots[item["name"]] = Spot(
                    name=item["name"],
                    purpose=item.get("purpose", "Park"),
                    parking_for_car_type=item.get("parkingForCarType", "Any"),
                    zone_parent=item.get("zoneParent", ""),
                    status=SpotStatus.OCCUPIED if occupied else SpotStatus.AVAILABLE,
                    broken=bool(item.get("broken", False)),
                    under_maintenance=bool(item.get("isUnderMaintenance", False)),
                    occupant_plate=plate,
                )

    def load_barriers(self, raw: list[dict]) -> None:
        with self._lock:
            for item in raw:
                self.barriers[item["name"]] = Barrier(
                    name=item["name"],
                    zone_parent=item.get("zoneParent", ""),
                    state=BarrierPosition(item.get("state", "Closed")),
                    broken=bool(item.get("broken", False)),
                    under_maintenance=bool(item.get("isUnderMaintenance", False)),
                )

    def load_fans(self, raw: list[dict]) -> None:
        with self._lock:
            for item in raw:
                self.fans[item["name"]] = ExhaustFan(
                    name=item["name"],
                    zone_parent=item.get("zoneParent", ""),
                    is_on=bool(item.get("isOn", False)),
                    broken=bool(item.get("broken", False)),
                    under_maintenance=bool(item.get("isUnderMaintenance", False)),
                )

    def load_lights(self, raw: list[dict]) -> None:
        with self._lock:
            for item in raw:
                self.lights[item["name"]] = Light(
                    name=item["name"],
                    zone_parent=item.get("zoneParent", ""),
                    is_on=bool(item.get("isOn", True)),
                )

    def load_zones(self, raw: list[dict]) -> None:
        with self._lock:
            for item in raw:
                self.zones[item["name"]] = Zone(
                    name=item["name"],
                    gas_co_level=float(item.get("gasCarbonMonoxideLevel", 0.0)),
                    risk=item.get("risk", "Safe"),
                )

    # ------------------------------------------------------------------ #
    # Event dedup / ordering
    # ------------------------------------------------------------------ #
    def is_duplicate(self, event_id: str) -> bool:
        return self._events.seen_before(event_id)

    def observe_sequence(self, sequence_id: Optional[int]) -> Optional[int]:
        """Record ``sequence_id`` and return the size of any detected gap (0/None if none)."""
        if sequence_id is None:
            return None
        with self._lock:
            gap = 0
            if self.last_sequence_id and sequence_id > self.last_sequence_id + 1:
                gap = sequence_id - self.last_sequence_id - 1
            if sequence_id > self.last_sequence_id:
                self.last_sequence_id = sequence_id
            return gap

    # ------------------------------------------------------------------ #
    # Spot mutations
    # ------------------------------------------------------------------ #
    def expire_stale_reservations(self, ttl_seconds: float) -> list[str]:
        """Release reservations for cars that never turned up.

        A reservation is a promise that a specific car is on its way. If the
        car is neglected at the entry and drives off, or a dispatch command
        fails, nothing else ever frees that spot -- and the lot slowly reports
        itself full while standing empty. Sweeping here (rather than on a
        timer) keeps it lazy and lock-free from the caller's point of view.
        """
        released: list[str] = []
        cutoff = time.monotonic() - ttl_seconds
        with self._lock:
            for spot in self.spots.values():
                if (
                    spot.status == SpotStatus.RESERVED
                    and spot.reserved_at is not None
                    and spot.reserved_at < cutoff
                ):
                    spot.status = SpotStatus.AVAILABLE
                    spot.occupant_plate = None
                    spot.reserved_at = None
                    released.append(spot.name)
        return released

    def available_spots(self, car_type: str = "Any") -> list[str]:
        with self._lock:
            return [
                s.name for s in self.spots.values()
                if s.dispatchable and (s.parking_for_car_type in ("Any", car_type))
            ]

    def reserve_spot(self, spot_name: str, plate: str) -> bool:
        with self._lock:
            spot = self.spots.get(spot_name)
            if spot is None or not spot.dispatchable:
                return False
            spot.status = SpotStatus.RESERVED
            spot.occupant_plate = plate
            spot.reserved_at = time.monotonic()
            return True

    def release_reservation(self, spot_name: str, plate: str) -> None:
        """Undo a reservation when the dispatch command did not actually go out."""
        with self._lock:
            spot = self.spots.get(spot_name)
            if spot is None or spot.status != SpotStatus.RESERVED:
                return
            if spot.occupant_plate == plate:
                spot.status = SpotStatus.AVAILABLE
                spot.occupant_plate = None
                spot.reserved_at = None

    def mark_spot_occupied(self, spot_name: str, plate: str) -> None:
        with self._lock:
            spot = self.spots.setdefault(spot_name, Spot(name=spot_name))
            spot.status = SpotStatus.OCCUPIED
            spot.occupant_plate = plate

    def mark_spot_vacant(self, spot_name: str) -> None:
        with self._lock:
            spot = self.spots.get(spot_name)
            if spot is None:
                return
            spot.occupant_plate = None
            spot.status = (
                SpotStatus.BROKEN if spot.broken
                else SpotStatus.MAINTENANCE if spot.under_maintenance
                else SpotStatus.AVAILABLE
            )

    def set_component_broken(self, component_type: str, name: str) -> None:
        with self._lock:
            if component_type == "ParkingSpot" and name in self.spots:
                spot = self.spots[name]
                spot.broken = True
                if spot.occupant_plate is None:
                    spot.status = SpotStatus.BROKEN
            elif component_type == "BarrierGate" and name in self.barriers:
                self.barriers[name].broken = True
            elif component_type == "ExhaustFan" and name in self.fans:
                self.fans[name].broken = True

    def set_component_fixed(self, component_type: str, name: str) -> None:
        with self._lock:
            if component_type == "ParkingSpot" and name in self.spots:
                spot = self.spots[name]
                spot.broken = False
                spot.under_maintenance = False
                if spot.occupant_plate is None:
                    spot.status = SpotStatus.AVAILABLE
            elif component_type == "BarrierGate" and name in self.barriers:
                barrier = self.barriers[name]
                barrier.broken = False
                barrier.under_maintenance = False
            elif component_type == "ExhaustFan" and name in self.fans:
                fan = self.fans[name]
                fan.broken = False
                fan.under_maintenance = False
            self.deferred_repairs.pop(name, None)

    def queue_deferred_repair(self, name: str, component_type: str) -> None:
        with self._lock:
            self.deferred_repairs[name] = component_type

    def pop_ready_repair(self, name: str) -> Optional[str]:
        """If ``name`` has a pending repair and is now vacant, clear and return its type."""
        with self._lock:
            component_type = self.deferred_repairs.get(name)
            if component_type is None:
                return None
            spot = self.spots.get(name)
            if spot is not None and spot.occupant_plate is not None:
                return None
            del self.deferred_repairs[name]
            return component_type

    def update_barrier_state(self, name: str, state_value: str) -> None:
        with self._lock:
            barrier = self.barriers.setdefault(name, Barrier(name=name))
            try:
                new_state = BarrierPosition(state_value)
            except ValueError:
                return
            # Count a wear cycle each time the barrier actually starts moving,
            # i.e. transitions into Opening/Closing - not on every repeated
            # "Open"/"Closed" webhook while it sits still.
            if new_state in (BarrierPosition.OPENING, BarrierPosition.CLOSING) and barrier.state != new_state:
                barrier.cycle_count += 1
            barrier.state = new_state

    def update_zone(self, name: str, co_level: float, danger_level: str) -> None:
        with self._lock:
            zone = self.zones.setdefault(name, Zone(name=name))
            zone.gas_co_level = co_level
            zone.danger_level = danger_level

    def fans_in_zone(self, zone_name: str) -> list[str]:
        with self._lock:
            return [f.name for f in self.fans.values() if f.zone_parent == zone_name]

    def lights_in_zone(self, zone_name: str) -> list[str]:
        with self._lock:
            return [l.name for l in self.lights.values() if l.zone_parent == zone_name]

    def zone_names(self) -> list[str]:
        with self._lock:
            names = {z for z in self.zones.keys()}
            names |= {b.zone_parent for b in self.barriers.values() if b.zone_parent}
            names |= {f.zone_parent for f in self.fans.values() if f.zone_parent}
            names |= {l.zone_parent for l in self.lights.values() if l.zone_parent}
            names |= {s.zone_parent for s in self.spots.values() if s.zone_parent}
            return sorted(names)

    # ------------------------------------------------------------------ #
    # Wear tracking (Level 2): cycles on barriers/fans/lights, runtime on
    # fans/lights. Persisted separately via app.db.upsert_component_wear -
    # this only holds the live counters the dashboard reads.
    # ------------------------------------------------------------------ #
    def record_barrier_cycle(self, name: str) -> int:
        """One open/close command was actually sent. Returns the new count."""
        with self._lock:
            barrier = self.barriers.setdefault(name, Barrier(name=name))
            barrier.cycle_count += 1
            return barrier.cycle_count

    def set_fan_on(self, name: str, on: bool) -> tuple[int, float]:
        """Toggle a fan and update its wear counters. Returns (cycles, runtime_s)."""
        with self._lock:
            fan = self.fans.setdefault(name, ExhaustFan(name=name))
            now = time.monotonic()
            if on and not fan.is_on:
                fan.is_on = True
                fan.cycle_count += 1
                fan.turned_on_at = now
            elif not on and fan.is_on:
                fan.is_on = False
                fan.cycle_count += 1
                if fan.turned_on_at is not None:
                    fan.runtime_seconds += max(0.0, now - fan.turned_on_at)
                fan.turned_on_at = None
            return fan.cycle_count, fan.runtime_seconds

    def set_light_on(self, name: str, on: bool) -> tuple[int, float]:
        with self._lock:
            light = self.lights.setdefault(name, Light(name=name, turned_on_at=None))
            now = time.monotonic()
            if on and not light.is_on:
                light.is_on = True
                light.cycle_count += 1
                light.turned_on_at = now
            elif not on and light.is_on:
                light.is_on = False
                light.cycle_count += 1
                if light.turned_on_at is not None:
                    light.runtime_seconds += max(0.0, now - light.turned_on_at)
                light.turned_on_at = None
            return light.cycle_count, light.runtime_seconds

    def wear_snapshot(self) -> list[dict[str, Any]]:
        """Live wear counters for every tracked component, for the wear-threshold sweep."""
        with self._lock:
            out: list[dict[str, Any]] = []
            for b in self.barriers.values():
                out.append({"name": b.name, "type": "BarrierGate",
                           "cycle_count": b.cycle_count, "runtime_seconds": 0.0,
                           "broken": b.broken, "under_maintenance": b.under_maintenance})
            for f in self.fans.values():
                runtime = f.runtime_seconds
                if f.is_on and f.turned_on_at is not None:
                    runtime += max(0.0, time.monotonic() - f.turned_on_at)
                out.append({"name": f.name, "type": "ExhaustFan",
                           "cycle_count": f.cycle_count, "runtime_seconds": runtime,
                           "broken": f.broken, "under_maintenance": f.under_maintenance})
            for l in self.lights.values():
                runtime = l.runtime_seconds
                if l.is_on and l.turned_on_at is not None:
                    runtime += max(0.0, time.monotonic() - l.turned_on_at)
                out.append({"name": l.name, "type": "Light",
                           "cycle_count": l.cycle_count, "runtime_seconds": runtime,
                           "broken": False, "under_maintenance": False})
            return out

    # ------------------------------------------------------------------ #
    # Vehicle sessions
    # ------------------------------------------------------------------ #
    def start_session(self, plate: str, gate: str, car_type: str = "Normal",
                      planned_minutes: float = 0.0) -> VehicleSession:
        with self._lock:
            session = self.sessions.get(plate)
            if session is None:
                session = VehicleSession(
                    plate=plate,
                    entry_gate=gate,
                    car_type=car_type or "Normal",
                    planned_minutes=planned_minutes or 0.0,
                    arrived_wall=_utcnow(),
                )
                self.sessions[plate] = session
            else:
                session.entry_gate = gate
                session.phase = SessionPhase.ARRIVED
                if car_type:
                    session.car_type = car_type
                if planned_minutes:
                    session.planned_minutes = planned_minutes
            return session

    def set_planned_minutes(self, plate: str, planned_minutes: float) -> None:
        """Record the booked duration; later events repeat it, so keep the last."""
        if not planned_minutes:
            return
        with self._lock:
            session = self.sessions.get(plate)
            if session is not None:
                session.planned_minutes = planned_minutes

    def get_session(self, plate: str) -> Optional[VehicleSession]:
        with self._lock:
            return self.sessions.get(plate)

    def assign_spot(self, plate: str, spot_name: str) -> None:
        with self._lock:
            session = self.sessions.get(plate)
            if session is not None:
                session.assigned_spot = spot_name
                session.phase = SessionPhase.ASSIGNED

    def mark_parked(self, plate: str, spot_name: str) -> None:
        with self._lock:
            session = self.sessions.get(plate)

            # If the car ended up somewhere other than the spot we reserved,
            # free the reservation -- otherwise that spot leaks and the lot
            # slowly appears full.
            if session is not None and session.assigned_spot and session.assigned_spot != spot_name:
                stale = self.spots.get(session.assigned_spot)
                if stale is not None and stale.occupant_plate == plate:
                    self.mark_spot_vacant(session.assigned_spot)

            self.mark_spot_occupied(spot_name, plate)
            if session is not None:
                session.phase = SessionPhase.PARKED
                session.assigned_spot = spot_name
                # Start the billing clock here, not at the entry sensor.
                if session.parked_at is None:
                    session.parked_at = time.monotonic()
                    session.parked_wall = _utcnow()

    def mark_left_spot(self, plate: str) -> None:
        """Stop the billing clock when the car vacates its spot."""
        with self._lock:
            session = self.sessions.get(plate)
            if session is not None and session.left_spot_at is None:
                session.left_spot_at = time.monotonic()
                session.left_spot_wall = _utcnow()

    def mark_exit_requested(self, plate: str, exit_gate: str) -> None:
        with self._lock:
            session = self.sessions.get(plate)
            if session is not None:
                session.phase = SessionPhase.EXIT_REQUESTED
                session.exit_gate = exit_gate

    def mark_at_exit(self, plate: str) -> None:
        with self._lock:
            session = self.sessions.get(plate)
            if session is not None:
                session.phase = SessionPhase.AT_EXIT

    def mark_charged(self, plate: str) -> None:
        with self._lock:
            session = self.sessions.get(plate)
            if session is not None:
                session.charged = True
                session.phase = SessionPhase.CHARGED

    def mark_paid(self, plate: str, amount: float) -> bool:
        """Returns True iff a session existed, was charged and not already paid."""
        with self._lock:
            session = self.sessions.get(plate)
            if session is None or not session.charged or session.paid:
                return False
            session.paid = True
            return True

    def complete_session(self, plate: str) -> Optional[VehicleSession]:
        """Remove the session and hand it back so it can be archived to SQLite."""
        with self._lock:
            return self.sessions.pop(plate, None)

    # ------------------------------------------------------------------ #
    # Telemetry: penalties, activity log, dashboard snapshot
    # ------------------------------------------------------------------ #
    def entry_gates(self) -> list[str]:
        with self._lock:
            return sorted(s.name for s in self.spots.values() if s.purpose == "EntrySpot")

    def record_penalty(self, reason: str, fine: float, component_type: str, component_name: str) -> None:
        with self._lock:
            self.penalty_count += 1
            self.total_fines += fine
            self.penalty_log.appendleft({
                "reason": reason, "fine": fine, "type": component_type,
                "component": component_name, "at": time.time(),
            })

    def log_activity(self, message: str, level: str = "info") -> None:
        with self._lock:
            self.activity_log.appendleft({"message": message, "level": level, "at": time.time()})

    def broken_components(self) -> list[dict[str, Any]]:
        with self._lock:
            out: list[dict[str, Any]] = []
            for s in self.spots.values():
                if s.broken or s.under_maintenance:
                    out.append({"name": s.name, "type": "ParkingSpot",
                               "broken": s.broken, "under_maintenance": s.under_maintenance,
                               "occupied": s.occupant_plate is not None})
            for b in self.barriers.values():
                if b.broken or b.under_maintenance:
                    out.append({"name": b.name, "type": "BarrierGate",
                               "broken": b.broken, "under_maintenance": b.under_maintenance,
                               "occupied": False})
            for f in self.fans.values():
                if f.broken or f.under_maintenance:
                    out.append({"name": f.name, "type": "ExhaustFan",
                               "broken": f.broken, "under_maintenance": f.under_maintenance,
                               "occupied": False})
            return out

    def occupancy_counts(self) -> dict[str, int]:
        with self._lock:
            counts = {status.value: 0 for status in SpotStatus}
            for s in self.spots.values():
                if s.purpose == "Park":
                    counts[s.status.value] += 1
            return counts

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "spots": [
                    {"name": s.name, "purpose": s.purpose, "status": s.status.value,
                     "broken": s.broken, "under_maintenance": s.under_maintenance,
                     "occupant_plate": s.occupant_plate, "zone": s.zone_parent,
                     "car_type": s.parking_for_car_type}
                    for s in self.spots.values()
                ],
                "barriers": [
                    {"name": b.name, "state": b.state.value, "broken": b.broken,
                     "under_maintenance": b.under_maintenance, "zone": b.zone_parent}
                    for b in self.barriers.values()
                ],
                "fans": [
                    {"name": f.name, "is_on": f.is_on, "broken": f.broken,
                     "under_maintenance": f.under_maintenance, "zone": f.zone_parent}
                    for f in self.fans.values()
                ],
                "lights": [
                    {"name": l.name, "is_on": l.is_on, "zone": l.zone_parent}
                    for l in self.lights.values()
                ],
                "wear": self.wear_snapshot(),
                "zones": [
                    {"name": z.name, "co_level": z.gas_co_level, "risk": z.risk,
                     "danger_level": z.danger_level}
                    for z in self.zones.values()
                ],
                "sessions": [
                    {"plate": s.plate, "entry_gate": s.entry_gate, "phase": s.phase.value,
                     "assigned_spot": s.assigned_spot, "exit_gate": s.exit_gate,
                     "charged": s.charged, "paid": s.paid}
                    for s in self.sessions.values()
                ],
                "occupancy": {status.value: sum(1 for s in self.spots.values()
                                                if s.purpose == "Park" and s.status == status)
                             for status in SpotStatus},
                "penalties": {"count": self.penalty_count, "total_fines": round(self.total_fines, 2),
                             "recent": list(self.penalty_log)[:20]},
                "activity": list(self.activity_log)[:30],
                "deferred_repairs": dict(self.deferred_repairs),
                "last_sequence_id": self.last_sequence_id,
                "uptime_s": round(time.time() - self.started_at, 1),
            }

state = ParkingState(max_processed_events=settings.max_processed_events)
