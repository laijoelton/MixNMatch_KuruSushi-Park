"""Zone balancing and zone-gate lookup. Pure functions over plain state objects.

A car is sent to the zone with the lowest ratio of unusable bays:

    (occupied + reserved + broken + under repair) / total bays in the zone

Each bay is counted once whatever combination of states it is in, so a broken
bay with a car still parked in it is one bay, and the ratio never exceeds 1.
"""
from __future__ import annotations

import re
from typing import Collection, Iterable, Optional

from app.state import Barrier, Spot, SpotStatus


def _natural(name: str) -> list:
    raise NotImplementedError("TODO: reimplement _natural")


def zone_ratios(spots: Iterable[Spot], pending: Collection[str] = ()) -> dict[str, float]:
    raise NotImplementedError("TODO: reimplement zone_ratios")


def pick_zone(candidates: Collection[str], ratios: dict[str, float]) -> Optional[str]:
    """Lowest ratio wins; a tie goes to the nearer zone (ZONE1 before ZONE2)."""
    raise NotImplementedError("TODO: reimplement pick_zone")


def entry_gate_for_zone(zone: str, barriers: Iterable[Barrier],
                        positions: dict[str, tuple[float, float]],
                        entry_points: list[tuple[float, float]],
                        exclude: Collection[str] = ()) -> Optional[str]:
    """The zone's own entry barrier: of the gates tagged with ``zone``, the one
    nearest an entry sensor (a zone's exit gate sits on the far side).

    ``None`` when the zone has no tagged gate, so the caller can fall back to
    the gate nearest the car's entry sensor (Level 1).
    """
    raise NotImplementedError("TODO: reimplement entry_gate_for_zone")
