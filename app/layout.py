"""Real pixel-space geometry for the split-screen dashboard's canvas twin.

The organizer's REST API exposes no coordinates - only names. The
simulator's own level file (``settings/<level>.json``) does, however,
contain the exact X/Y layout used to render the game itself: every parking
spot, gate and zone with its position, size and rotation. This module reads
that file once and caches it, so the operator canvas can draw the real lot
shape instead of a synthetic projection.

This is geometry only - never status. Live status (occupied/broken/etc.)
always comes from the webhook-driven ``ParkingState``, never from this file.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("dispatcher.layout")

_CANDIDATE_DIRS = [
    Path("ParkingSimulator-win-x64/ParkingSimulator-win-x64/settings"),
    Path("settings"),
    Path("."),
]

_lock = threading.Lock()
_cache: dict[str, dict[str, Any]] = {}


def _find_level_file(level: str) -> Optional[Path]:
    for directory in _CANDIDATE_DIRS:
        candidate = directory / f"{level}.json"
        if candidate.exists():
            return candidate
    return None


def load_layout(level: str = "lvl1") -> dict[str, Any]:
    """Return ``{spots, gates, zones, bounds}`` in raw simulator pixel space."""
    with _lock:
        if level in _cache:
            return _cache[level]

        source = _find_level_file(level)
        if source is None:
            log.warning("layout: no %s.json found - canvas will use a placeholder grid", level)
            empty = {"spots": {}, "gates": {}, "zones": {}, "bounds": None}
            _cache[level] = empty
            return empty

        raw = json.loads(source.read_text(encoding="utf-8-sig"))

        spots = {
            s["Name"]: {
                "x": float(s["X"]), "y": float(s["Y"]),
                "w": float(s.get("Width", 80)), "h": float(s.get("Height", 160)),
                "rotation": float(s.get("Rotation", 0)),
                "purpose": s.get("Purpose", "Park"),
                "zone": s.get("ZoneParent", ""),
            }
            for s in raw.get("ParkingSpots", [])
        }
        gates = {
            g["Name"]: {
                "x": float(g["X"]), "y": float(g["Y"]),
                "rotation": float(g.get("Rotation", 0)),
                "zone": g.get("ZoneParent", ""),
            }
            for g in raw.get("Gates", [])
        }
        zones = {
            z["Name"]: {
                "x": float(z["X"]), "y": float(z["Y"]),
                "w": float(z.get("Width", 0)), "h": float(z.get("Height", 0)),
            }
            for z in raw.get("Zones", [])
        }

        xs = [s["x"] for s in spots.values()] + [g["x"] for g in gates.values()]
        ys = [s["y"] for s in spots.values()] + [g["y"] for g in gates.values()]
        bounds = (
            {"min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)}
            if xs and ys else None
        )

        result = {"spots": spots, "gates": gates, "zones": zones, "bounds": bounds}
        log.info("layout: loaded %s (%d spots, %d gates, %d zones)",
                 source, len(spots), len(gates), len(zones))
        _cache[level] = result
        return result
