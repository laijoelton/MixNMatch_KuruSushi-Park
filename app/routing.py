"""Bay ranking by real driving geometry.

Ranking prefers, in order:

1. A precomputed driving-distance table (``data/distances.json``), produced by
   ``scripts/export_graph.py`` then ``scripts/build_distances.py``.
2. Straight-line distance over live component coordinates.
3. Stable alphabetical order, only when no geometry is known at all.

There is deliberately no synthetic ring. The previous implementation projected
station names onto a ring by sorted order, which made ``S1`` adjacent to ``S10``
and had nothing to do with the physical lot.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Iterable, Optional, Sequence

_DISTANCES: dict[str, dict[str, float]] = {}
_COORDS: dict[str, tuple[float, float]] = {}


def reset() -> None:
    """Drop all loaded geometry. Used by tests for isolation."""
    _DISTANCES.clear()
    _COORDS.clear()


def set_distance_table(table: dict[str, dict[str, float]]) -> None:
    _DISTANCES.clear()
    _DISTANCES.update({
        str(origin): {str(spot): float(d) for spot, d in targets.items()}
        for origin, targets in table.items()
        if isinstance(targets, dict)
    })


def set_coordinates(coords: dict[str, tuple[float, float]]) -> None:
    _COORDS.clear()
    _COORDS.update({str(k): (float(v[0]), float(v[1])) for k, v in coords.items()})


def load_distance_table(path: str = "data/distances.json") -> bool:
    """Load a precomputed origin -> spot driving-distance table, if present."""
    file = Path(path)
    if not file.exists():
        return False
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(raw, dict):
        return False
    set_distance_table(raw)
    return bool(_DISTANCES)


def has_distance_table() -> bool:
    return bool(_DISTANCES)


def _cost(gate_name: str, spot: str) -> Optional[float]:
    """Best known cost from ``gate_name`` to ``spot``, or None if unknown."""
    table = _DISTANCES.get(gate_name)
    if table and spot in table:
        return table[spot]
    origin, target = _COORDS.get(gate_name), _COORDS.get(spot)
    if origin is not None and target is not None:
        return math.hypot(origin[0] - target[0], origin[1] - target[1])
    return None


def rank_spots(gate_name: str, available_spots: Iterable[str]) -> list[tuple[str, float]]:
    """All ``available_spots`` ordered by ascending cost from ``gate_name``.

    Spots with no known geometry sort last, then alphabetically, so an unknown
    name can never displace a bay we can actually measure.
    """
    scored = []
    for spot in available_spots:
        cost = _cost(gate_name, spot)
        scored.append((spot, math.inf if cost is None else cost))
    return sorted(scored, key=lambda item: (item[1], item[0]))


def find_best_spot(gate_name: str, available_spots: Sequence[str]) -> Optional[str]:
    """The cheapest bay to drive to from ``gate_name``, or None if there are none."""
    if not available_spots:
        return None
    return rank_spots(gate_name, available_spots)[0][0]
