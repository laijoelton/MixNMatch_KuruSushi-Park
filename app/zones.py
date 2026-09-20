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
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def zone_ratios(spots: Iterable[Spot], pending: Collection[str] = ()) -> dict[str, float]:
    totals: dict[str, int] = {}
    unusable: dict[str, int] = {}
    for spot in spots:
        if spot.purpose != "Park":
            continue
        totals[spot.zone_parent] = totals.get(spot.zone_parent, 0) + 1
        if (spot.status != SpotStatus.AVAILABLE or spot.broken or spot.under_maintenance
                or spot.name in pending):
            unusable[spot.zone_parent] = unusable.get(spot.zone_parent, 0) + 1
    return {zone: unusable.get(zone, 0) / total for zone, total in totals.items()}


def pick_zone(candidates: Collection[str], ratios: dict[str, float]) -> Optional[str]:
    """Lowest ratio wins; a tie goes to the nearer zone (ZONE1 before ZONE2)."""
    if not candidates:
        return None
    return min(candidates, key=lambda zone: (ratios.get(zone, 1.0), _natural(zone)))


def pick_zone_staged(candidates: Collection[str], ratios: dict[str, float],
                      balance_ceiling: float = 0.40) -> tuple[Optional[str], str]:
    """Three-stage occupancy lifecycle over an already-filtered candidate set
    (reachable, category-compatible, gate-usable, not under maintenance -
    those cuts happen in ``dispatch_entry`` and are load-bearing: 4.39 found
    that a car sent to an unreachable-but-empty zone just circles and leaves).

    Stage 1, cascading: any candidate zone still under ``balance_ceiling`` -
    route by priority (nearest/lowest zone number first), ignoring the exact
    ratio. This also resolves the 30%-40% gap in the original two-threshold
    reading of the spec (enter cascading below 30%, stay in it up to 40%): a
    zone sitting only in that gap, with nothing below 30%, is still routed by
    priority rather than left undefined - the threshold that actually decides
    the outcome is the 40% ceiling, so a separate 30% "entry" check would be
    dead code once that gap is handled at all.
    Stage 2, load balancing: every candidate is at or above the ceiling -
    route to whichever has the lowest ratio, breaking priority order to
    spread load.
    Stage 3, exhaustion: every candidate is completely full (ratio == 1.0), or
    there are no candidates at all - no zone is returned, so the caller drops
    the dispatch and raises a full-capacity warning instead of guessing.

    Returns ``(zone, stage)`` with ``stage`` one of ``"stage1"``, ``"stage2"``,
    ``"stage3"``.
    """
    if not candidates:
        return None, "stage3"
    live = {zone: ratios.get(zone, 1.0) for zone in candidates}
    if all(ratio >= 1.0 for ratio in live.values()):
        return None, "stage3"
    under_ceiling = [zone for zone in candidates if live[zone] < balance_ceiling]
    if under_ceiling:
        return min(under_ceiling, key=_natural), "stage1"
    return min(candidates, key=lambda zone: (live[zone], _natural(zone))), "stage2"


def entry_gate_for_zone(zone: str, barriers: Iterable[Barrier],
                        positions: dict[str, tuple[float, float]],
                        entry_points: list[tuple[float, float]],
                        exclude: Collection[str] = ()) -> Optional[str]:
    """The zone's own entry barrier: of the gates tagged with ``zone``, the one
    nearest an entry sensor (a zone's exit gate sits on the far side).

    ``None`` when the zone has no tagged gate, so the caller can fall back to
    the gate nearest the car's entry sensor (Level 1).
    """
    if not zone:
        return None
    gates = [b.name for b in barriers if b.zone_parent == zone and b.name not in exclude]
    if len(gates) <= 1 or not entry_points:
        return gates[0] if gates else None

    def distance(name: str) -> float:
        if name not in positions:
            return float("inf")
        x, y = positions[name]
        return min((x - ex) ** 2 + (y - ey) ** 2 for ex, ey in entry_points)

    return min(gates, key=lambda name: (distance(name), _natural(name)))
