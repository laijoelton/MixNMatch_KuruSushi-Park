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
    raise NotImplementedError("TODO: reimplement reset")


def set_distance_table(table: dict[str, dict[str, float]]) -> None:
    raise NotImplementedError("TODO: reimplement set_distance_table")


def set_coordinates(coords: dict[str, tuple[float, float]]) -> None:
    raise NotImplementedError("TODO: reimplement set_coordinates")


def load_distance_table(path: str = "data/distances.json") -> bool:
    """Load a precomputed origin -> spot driving-distance table, if present."""
    raise NotImplementedError("TODO: reimplement load_distance_table")


def has_distance_table() -> bool:
    raise NotImplementedError("TODO: reimplement has_distance_table")


def _cost(gate_name: str, spot: str) -> Optional[float]:
    """Best known cost from ``gate_name`` to ``spot``, or None if unknown."""
    raise NotImplementedError("TODO: reimplement _cost")


def rank_spots(gate_name: str, available_spots: Iterable[str]) -> list[tuple[str, float]]:
    """All ``available_spots`` ordered by ascending cost from ``gate_name``.

    Spots with no known geometry sort last, then alphabetically, so an unknown
    name can never displace a bay we can actually measure.
    """
    raise NotImplementedError("TODO: reimplement rank_spots")


def find_best_spot(gate_name: str, available_spots: Sequence[str]) -> Optional[str]:
    """The cheapest bay to drive to from ``gate_name``, or None if there are none."""
    raise NotImplementedError("TODO: reimplement find_best_spot")
