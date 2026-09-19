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
from enum import Enum
from typing import Any, Optional

from app.config import settings


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


@dataclass
class ExhaustFan:
    name: str
    zone_parent: str = ""
    is_on: bool = False
    broken: bool = False
    under_maintenance: bool = False


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
        with self._lock:
            for item in raw:
                detected = item.get("detectedCars") or []
                # The live simulator reports this as either a plate-name list or a
                # bare occupancy count depending on build - handle both shapes.
                if isinstance(detected, list):
                    occupied = bool(detected)
                    occupant_plate = detected[0] if detected else None
                else:
                    occupied = bool(detected)
                    occupant_plate = None
                self.spots[item["name"]] = Spot(
                    name=item["name"],
                    purpose=item.get("purpose", "Park"),
                    parking_for_car_type=item.get("parkingForCarType", "Any"),
                    zone_parent=item.get("zoneParent", ""),
                    status=SpotStatus.OCCUPIED if occupied else SpotStatus.AVAILABLE,
                    broken=bool(item.get("broken", False)),
                    under_maintenance=bool(item.get("isUnderMaintenance", False)),
                    occupant_plate=occupant_plate,
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
            return True

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
                barrier.state = BarrierPosition(state_value)
            except ValueError:
                pass

    def update_zone(self, name: str, co_level: float, danger_level: str) -> None:
        with self._lock:
            zone = self.zones.setdefault(name, Zone(name=name))
            zone.gas_co_level = co_level
            zone.danger_level = danger_level

    def fans_in_zone(self, zone_name: str) -> list[str]:
        with self._lock:
            return [f.name for f in self.fans.values() if f.zone_parent == zone_name]

    # ------------------------------------------------------------------ #
    # Vehicle sessions
    # ------------------------------------------------------------------ #
    def start_session(self, plate: str, gate: str) -> VehicleSession:
        with self._lock:
            session = self.sessions.get(plate)
            if session is None:
                session = VehicleSession(plate=plate, entry_gate=gate)
                self.sessions[plate] = session
            else:
                session.entry_gate = gate
                session.phase = SessionPhase.ARRIVED
            return session

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
            self.mark_spot_occupied(spot_name, plate)
            session = self.sessions.get(plate)
            if session is not None:
                session.phase = SessionPhase.PARKED
                session.assigned_spot = spot_name

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

    def complete_session(self, plate: str) -> None:
        with self._lock:
            self.sessions.pop(plate, None)

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
