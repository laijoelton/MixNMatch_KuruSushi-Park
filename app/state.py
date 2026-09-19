"""In-memory, thread-safe mirror of Grand Park Auto simulator state.

Per the organizer's API guidelines, list-* endpoints must be called only
once at startup (or after a crash) - all state after that point is derived
exclusively from inbound webhook events. This module is the single source
of truth the rest of the service reads and writes.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from app.config import settings


def _utcnow() -> str:
    """Wall-clock stamp for the dashboard; monotonic time cannot be a date."""
    raise NotImplementedError("TODO: reimplement _utcnow")


def _extract_plate(entry) -> Optional[str]:
    """A detectedCars list entry may be a bare plate string or an object."""
    raise NotImplementedError("TODO: reimplement _extract_plate")


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
    is_accessible: bool = False
    cycle_count: int = 0

    @property
    def dispatchable(self) -> bool:
        raise NotImplementedError("TODO: reimplement dispatchable")


@dataclass
class Barrier:
    name: str
    zone_parent: str = ""
    state: BarrierPosition = BarrierPosition.CLOSED
    broken: bool = False
    under_maintenance: bool = False
    # Wear: one cycle per open/close command sent to the simulator.
    cycle_count: int = 0
    operator_override: bool = False
    held_vehicles: set[str] = field(default_factory=set)
    # Opens since the last repair: what the balanced repair schedule ranks by (4.28).
    opens_since_repair: int = 0
    # Staff pressed Open: held open, the automation leaves it alone (4.33).
    operator_open: bool = False


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
    broken: bool = False
    under_maintenance: bool = False


@dataclass
class Zone:
    name: str
    gas_co_level: float = 0.0
    risk: str = "Safe"
    danger_level: str = "Safe"
    # Trailing (monotonic_timestamp, co_level) readings, for the predictive
    # ventilation forecast in app.ml_agent. Bounded so a long-running level
    # can't grow this unbounded; not a durable log (see db.py for that).
    co_history: deque = field(default_factory=lambda: deque(maxlen=180))


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
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    charge_attempted: bool = False
    zone: str = ""
    entry_departed: bool = False
    # The zone's own entry sensor the car was sent to first (ENTRY3 for a
    # ZONE3 bay); its gate opens only once the car is waiting there.
    staged_via: Optional[str] = None
    # Ghost car (4.25): never seen at an entrance. Billed automatically at the
    # exit, but only released when staff say so (release_authorized).
    ghost_id: Optional[int] = None
    # The car has reached its own zone's entry sensor (ENTRY3 for ZONE3):
    # only then does that zone light up for it (4.31).
    reached_zone: bool = False
    # Has driven out of the sensor box in front of its zone gate, i.e. is
    # through it: it no longer needs that gate held open (4.32).
    passed_zone_gate: bool = False
    release_authorized: bool = False
    exit_confirmed: bool = False
    released: bool = False
    payment_suspect: bool = False

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
        raise NotImplementedError("TODO: reimplement is_electric")

    @property
    def billable_minutes(self) -> float:
        raise NotImplementedError("TODO: reimplement billable_minutes")

    @property
    def measured_minutes(self) -> float:
        """Minutes between parking and leaving the spot (or now, if still in)."""
        raise NotImplementedError("TODO: reimplement measured_minutes")


class _BoundedEventCache:
    """Fixed-capacity FIFO set used to dedup inbound ``EventId`` values."""

    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, capacity)
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._lock = threading.RLock()

    def seen_before(self, event_id: str) -> bool:
        raise NotImplementedError("TODO: reimplement seen_before")


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
        self.active_dispatches: dict[str, str] = {}
        self.neglected_vehicles: deque = deque(maxlen=100)
        self.last_sequence_id: int = 0
        self.deferred_repairs: dict[str, str] = {}
        self.pending_repairs: dict[str, str] = {}
        # Zones closed for gate maintenance (4.26):
        # zone -> {"entry", "exit", "trigger", "todo": gates still to repair}
        self.zone_maintenance: dict[str, dict[str, Any]] = {}
        # Gates whose repair the simulator accepted but never finished (4.35).
        self.stuck_repairs: set[str] = set()
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
        raise NotImplementedError("TODO: reimplement load_spots")

    def load_barriers(self, raw: list[dict]) -> None:
        raise NotImplementedError("TODO: reimplement load_barriers")

    def load_fans(self, raw: list[dict]) -> None:
        raise NotImplementedError("TODO: reimplement load_fans")

    def load_lights(self, raw: list[dict]) -> None:
        raise NotImplementedError("TODO: reimplement load_lights")

    def load_zones(self, raw: list[dict]) -> None:
        raise NotImplementedError("TODO: reimplement load_zones")

    # ------------------------------------------------------------------ #
    # Event dedup / ordering
    # ------------------------------------------------------------------ #
    def is_duplicate(self, event_id: str) -> bool:
        raise NotImplementedError("TODO: reimplement is_duplicate")

    def observe_sequence(self, sequence_id: Optional[int]) -> Optional[int]:
        """Record ``sequence_id`` and return the size of any detected gap (0/None if none)."""
        raise NotImplementedError("TODO: reimplement observe_sequence")

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
        raise NotImplementedError("TODO: reimplement expire_stale_reservations")

    def available_spots(self, car_type: str = "Any") -> list[str]:
        raise NotImplementedError("TODO: reimplement available_spots")

    def reserve_spot(self, spot_name: str, plate: str) -> bool:
        raise NotImplementedError("TODO: reimplement reserve_spot")

    def release_reservation(self, spot_name: str, plate: str) -> None:
        """Undo a reservation when the dispatch command did not actually go out."""
        raise NotImplementedError("TODO: reimplement release_reservation")

    def mark_spot_occupied(self, spot_name: str, plate: str) -> None:
        raise NotImplementedError("TODO: reimplement mark_spot_occupied")

    def mark_spot_vacant(self, spot_name: str) -> None:
        raise NotImplementedError("TODO: reimplement mark_spot_vacant")

    def set_component_broken(self, component_type: str, name: str) -> None:
        raise NotImplementedError("TODO: reimplement set_component_broken")

    def set_component_fixed(self, component_type: str, name: str) -> None:
        raise NotImplementedError("TODO: reimplement set_component_fixed")

    def queue_deferred_repair(self, name: str, component_type: str) -> None:
        raise NotImplementedError("TODO: reimplement queue_deferred_repair")

    def pop_ready_repair(self, name: str) -> Optional[str]:
        """If ``name`` has a pending repair and is now vacant, clear and return its type."""
        raise NotImplementedError("TODO: reimplement pop_ready_repair")

    def update_barrier_state(self, name: str, state_value: str) -> None:
        raise NotImplementedError("TODO: reimplement update_barrier_state")

    def update_zone(self, name: str, co_level: float, danger_level: str) -> None:
        raise NotImplementedError("TODO: reimplement update_zone")

    def co_history(self, zone_name: str) -> list[tuple[float, float]]:
        """Trailing CO readings for a zone, oldest first. Empty if unknown."""
        raise NotImplementedError("TODO: reimplement co_history")

    def occupancy_ratio(self, zone_name: str) -> float:
        """R = (reserved + occupied) / capacity for this zone's parking spots.

        Reserved bays are cars already dispatched but not yet parked - they
        represent load about to land, so counting only OCCUPIED undercounts
        a zone that's about to fill up.
        """
        raise NotImplementedError("TODO: reimplement occupancy_ratio")

    def fans_in_zone(self, zone_name: str) -> list[str]:
        raise NotImplementedError("TODO: reimplement fans_in_zone")

    def lights_in_zone(self, zone_name: str) -> list[str]:
        raise NotImplementedError("TODO: reimplement lights_in_zone")

    def zone_names(self) -> list[str]:
        raise NotImplementedError("TODO: reimplement zone_names")

    # ------------------------------------------------------------------ #
    # Wear tracking (Level 2): cycles on barriers/fans/lights, runtime on
    # fans/lights. Persisted separately via app.db.upsert_component_wear -
    # this only holds the live counters the dashboard reads.
    # ------------------------------------------------------------------ #
    def record_barrier_cycle(self, name: str) -> int:
        """Return movement count, already updated by update_barrier_state()."""
        raise NotImplementedError("TODO: reimplement record_barrier_cycle")

    def set_fan_on(self, name: str, on: bool) -> tuple[int, float]:
        """Toggle a fan and update its wear counters. Returns (cycles, runtime_s)."""
        raise NotImplementedError("TODO: reimplement set_fan_on")

    def set_light_on(self, name: str, on: bool) -> tuple[int, float]:
        raise NotImplementedError("TODO: reimplement set_light_on")

    def wear_snapshot(self) -> list[dict[str, Any]]:
        """Live wear counters for every tracked component, for the wear-threshold sweep."""
        raise NotImplementedError("TODO: reimplement wear_snapshot")

    # ------------------------------------------------------------------ #
    # Vehicle sessions
    # ------------------------------------------------------------------ #
    def start_session(self, plate: str, gate: str, car_type: str = "Normal",
                      planned_minutes: float = 0.0) -> VehicleSession:
        raise NotImplementedError("TODO: reimplement start_session")

    def set_planned_minutes(self, plate: str, planned_minutes: float) -> None:
        """Record the booked duration; later events repeat it, so keep the last."""
        raise NotImplementedError("TODO: reimplement set_planned_minutes")

    def get_session(self, plate: str) -> Optional[VehicleSession]:
        raise NotImplementedError("TODO: reimplement get_session")

    def assign_spot(self, plate: str, spot_name: str) -> None:
        raise NotImplementedError("TODO: reimplement assign_spot")

    def mark_parked(self, plate: str, spot_name: str) -> None:
        raise NotImplementedError("TODO: reimplement mark_parked")

    def mark_left_spot(self, plate: str) -> None:
        """Stop the billing clock when the car vacates its spot."""
        raise NotImplementedError("TODO: reimplement mark_left_spot")

    def mark_exit_requested(self, plate: str, exit_gate: str) -> None:
        raise NotImplementedError("TODO: reimplement mark_exit_requested")

    def mark_at_exit(self, plate: str) -> None:
        raise NotImplementedError("TODO: reimplement mark_at_exit")

    def mark_charged(self, plate: str) -> None:
        raise NotImplementedError("TODO: reimplement mark_charged")

    def mark_paid(self, plate: str, amount: float) -> bool:
        """Returns True iff a session existed, was charged and not already paid."""
        raise NotImplementedError("TODO: reimplement mark_paid")

    def complete_session(self, plate: str) -> Optional[VehicleSession]:
        """Remove the session and hand it back so it can be archived to SQLite."""
        raise NotImplementedError("TODO: reimplement complete_session")

    # ------------------------------------------------------------------ #
    # Telemetry: penalties, activity log, dashboard snapshot
    # ------------------------------------------------------------------ #
    def entry_gates(self) -> list[str]:
        raise NotImplementedError("TODO: reimplement entry_gates")

    def record_penalty(self, reason: str, fine: float, component_type: str, component_name: str) -> None:
        raise NotImplementedError("TODO: reimplement record_penalty")

    def clear_penalties(self) -> None:
        """Forget recorded fines; paired with an admin clearing the penalties table."""
        raise NotImplementedError("TODO: reimplement clear_penalties")

    def reset_for_new_level(self) -> int:
        """Forget the previous level's live park: cars, bays, gates and repair
        bookkeeping. Returns how many vehicle sessions were dropped. Penalties
        and the activity feed are kept - they are the run's record."""
        raise NotImplementedError("TODO: reimplement reset_for_new_level")

    def clear_neglected(self) -> None:
        raise NotImplementedError("TODO: reimplement clear_neglected")

    def log_activity(self, message: str, level: str = "info", capability: str = "logs:view_ops") -> None:
        raise NotImplementedError("TODO: reimplement log_activity")

    def broken_components(self) -> list[dict[str, Any]]:
        raise NotImplementedError("TODO: reimplement broken_components")

    def occupancy_counts(self) -> dict[str, int]:
        raise NotImplementedError("TODO: reimplement occupancy_counts")

    def snapshot(self) -> dict[str, Any]:
        raise NotImplementedError("TODO: reimplement snapshot")

def normalize_car_type(value: str) -> str:
    raise NotImplementedError("TODO: reimplement normalize_car_type")


BarrierGate = Barrier
state = ParkingState(max_processed_events=settings.max_processed_events)
