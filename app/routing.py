"""Circular shortest-path routing for the Grand Park Auto simulator.

The organizer's simulator has no notion of a physical ring - it only exposes
named spots, barriers and zones over REST. To reuse KuruSushi-Park's
bidirectional circular-offset dispatch heuristic (originally written for a
real rotating conveyor loop), every named station is deterministically
projected onto a synthetic ring of ``N`` positions by sorting station names.
The projection is stable across process restarts as long as the simulator's
component list does not change composition between runs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence


def circular_delta(src: int, dst: int, n: int) -> int:
    """Shortest signed step count from ``src`` to ``dst`` on a ring of ``n``.

    Positive = forward (clockwise). An exact half-loop tie resolves forward.
    Ported verbatim from KuruSushi-Park/core/conveyor_engine.py:55-65.
    """
    if n <= 0:
        raise ValueError("ring size must be positive")
    delta = (dst - src) % n
    if delta * 2 > n:
        delta -= n
    return delta


def circular_distance(src: int, dst: int, n: int) -> int:
    """Absolute shortest step count from ``src`` to ``dst`` on a ring of ``n``.

    Ported verbatim from KuruSushi-Park/core/conveyor_engine.py:68-69.
    """
    return abs(circular_delta(src, dst, n))


class StationRing:
    """Deterministic virtual-ring position for every known station name."""

    def __init__(self) -> None:
        self._index: dict[str, int] = {}
        self._order: list[str] = []

    def rebuild(self, station_names: Iterable[str]) -> None:
        self._order = sorted(set(station_names))
        self._index = {name: i for i, name in enumerate(self._order)}

    def register(self, station_name: str) -> int:
        if station_name not in self._index:
            self._order.append(station_name)
            self._order.sort()
            self._index = {name: i for i, name in enumerate(self._order)}
        return self._index[station_name]

    def position(self, station_name: str) -> int:
        if station_name not in self._index:
            return self.register(station_name)
        return self._index[station_name]

    @property
    def size(self) -> int:
        return max(1, len(self._order))


ring = StationRing()


# --------------------------------------------------------------------------- #
# Real-distance override
# --------------------------------------------------------------------------- #
# The synthetic ring above orders stations by NAME, which has nothing to do with
# where they physically are. The simulator's level file
# (settings/lvl1.json) contains the actual road graph -- 60 nodes with X/Y
# coordinates and directed Connections -- plus X/Y for every spot and gate.
#
# If a precomputed distance table is present it is used instead of the ring.
# Expected shape, driving distance from each origin to each spot:
#
#     {"ENTRY1": {"S1": 340.2, "S3": 512.8, ...}, "ENTRY2": {...}}
#
# Generate it with the C/C++ pathfinder (Dijkstra/A* over the Paths graph) and
# drop it at data/distances.json. Nothing else needs to change.
_DISTANCES: dict[str, dict[str, float]] = {}


def load_distance_table(path: str = "data/distances.json") -> bool:
    """Load a precomputed origin -> spot driving-distance table, if present."""
    global _DISTANCES
    file = Path(path)
    if not file.exists():
        return False
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(raw, dict):
        return False
    _DISTANCES = {
        str(origin): {str(spot): float(d) for spot, d in targets.items()}
        for origin, targets in raw.items()
        if isinstance(targets, dict)
    }
    return bool(_DISTANCES)


def has_distance_table() -> bool:
    return bool(_DISTANCES)


def find_best_spot(gate_name: str, available_spots: Sequence[str]) -> Optional[str]:
    """Return the ``available_spots`` entry with the minimum bidirectional
    circular step offset from ``gate_name``:

        dist = min((T - G) mod N, (G - T) mod N)

    Ties broken alphabetically for determinism.
    """
    if not available_spots:
        return None

    # Prefer real driving distance when the pathfinder has supplied a table.
    table = _DISTANCES.get(gate_name)
    if table:
        known = [s for s in available_spots if s in table]
        if known:
            return min(known, key=lambda spot: (table[spot], spot))

    n = ring.size
    origin = ring.position(gate_name)
    ranked = sorted(
        available_spots,
        key=lambda spot: (circular_distance(origin, ring.position(spot), n), spot),
    )
    return ranked[0]


def rank_spots(gate_name: str, available_spots: Iterable[str]) -> list[tuple[str, int]]:
    """All ``available_spots`` ordered by ascending circular distance from ``gate_name``."""
    n = ring.size
    origin = ring.position(gate_name)
    return sorted(
        ((spot, circular_distance(origin, ring.position(spot), n)) for spot in available_spots),
        key=lambda item: (item[1], item[0]),
    )


# --------------------------------------------------------------------------- #
# Multi-factor bay ranking
# --------------------------------------------------------------------------- #
# rank_spots()/find_best_spot() above optimise purely for ring/driving
# distance. This adds two more factors a real operator cares about, without
# touching the functions the live dispatcher already calls:
#
#   Cost(S) = w_distance * RotationalStepDist(G, S)
#           + w_congestion * ZoneCongestion(Z)
#           + w_aisle * AislePenalty(S)
#
# All three terms are dimensionless costs the caller supplies or lets
# default to zero, so a weight of 0 exactly reproduces plain-distance
# ranking - this is a strict superset of find_best_spot(), not a
# replacement, and is covered by tests/run_route_bench.py.
@dataclass(frozen=True)
class RankingWeights:
    distance: float = 1.0
    congestion: float = 0.0
    aisle: float = 0.0


def _spot_distance(gate_name: str, spot: str) -> float:
    """Same distance source find_best_spot() uses: real table if loaded,
    else the synthetic ring - so weighted ranking degrades identically."""
    table = _DISTANCES.get(gate_name)
    if table and spot in table:
        return table[spot]
    n = ring.size
    return float(circular_distance(ring.position(gate_name), ring.position(spot), n))


def score_spot(
    gate_name: str,
    spot: str,
    *,
    zone_of: Optional[Mapping[str, str]] = None,
    zone_congestion: Optional[Mapping[str, float]] = None,
    aisle_penalty: Optional[Mapping[str, float]] = None,
    weights: Optional[RankingWeights] = None,
) -> float:
    """Weighted cost for dispatching to ``spot`` from ``gate_name``. Lower is better.

    ``zone_congestion`` values are expected as an occupancy ratio in [0, 1]
    (e.g. occupied/total for that spot's zone); ``aisle_penalty`` as an
    arbitrary non-negative cost (e.g. an extra turn, a narrow bay). Both maps
    are optional and missing entries cost 0.
    """
    w = weights or RankingWeights()
    cost = w.distance * _spot_distance(gate_name, spot)
    if w.congestion and zone_of is not None and zone_congestion is not None:
        zone = zone_of.get(spot)
        if zone is not None:
            cost += w.congestion * zone_congestion.get(zone, 0.0)
    if w.aisle and aisle_penalty is not None:
        cost += w.aisle * aisle_penalty.get(spot, 0.0)
    return cost


def rank_spots_weighted(
    gate_name: str,
    available_spots: Iterable[str],
    *,
    zone_of: Optional[Mapping[str, str]] = None,
    zone_congestion: Optional[Mapping[str, float]] = None,
    aisle_penalty: Optional[Mapping[str, float]] = None,
    weights: Optional[RankingWeights] = None,
) -> list[tuple[str, float]]:
    """All ``available_spots`` ordered by ascending weighted cost. Ties broken
    alphabetically for determinism, same convention as rank_spots()."""
    scored = (
        (spot, score_spot(gate_name, spot, zone_of=zone_of, zone_congestion=zone_congestion,
                          aisle_penalty=aisle_penalty, weights=weights))
        for spot in available_spots
    )
    return sorted(scored, key=lambda item: (item[1], item[0]))


def find_best_spot_weighted(
    gate_name: str,
    available_spots: Sequence[str],
    *,
    zone_of: Optional[Mapping[str, str]] = None,
    zone_congestion: Optional[Mapping[str, float]] = None,
    aisle_penalty: Optional[Mapping[str, float]] = None,
    weights: Optional[RankingWeights] = None,
) -> Optional[str]:
    ranked = rank_spots_weighted(gate_name, available_spots, zone_of=zone_of,
                                 zone_congestion=zone_congestion, aisle_penalty=aisle_penalty,
                                 weights=weights)
    return ranked[0][0] if ranked else None
