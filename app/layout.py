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
from typing import Any, Iterable, Optional

from app.config import settings

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


# --------------------------------------------------------------------------- #
# List-based geometry + level auto-detection (operator console twin)
#
# ``load_layout`` above keeps its name-keyed dict shape for the split
# dashboard. The console uses lists so every physical component remains
# representable, plus fans, lights and zone types, and it must work out
# *which* level is running without being told.
# --------------------------------------------------------------------------- #
LEVELS = ("lvl1", "lvl2", "lvl3")
MATCH_THRESHOLD = 0.8
_geometry_cache: dict[str, dict[str, Any]] = {}


def _empty_geometry() -> dict[str, Any]:
    return {"level": None, "zones": [], "spots": [], "gates": [], "fans": [], "lights": [], "bounds": None}


def _geometry_bounds(zones: list[dict], spots: list[dict]) -> Optional[dict[str, float]]:
    """Frame the zones and the spots cars actually use - not the far-off escape points."""
    xs: list[float] = []
    ys: list[float] = []
    for z in zones:
        xs += [z["x"] - z["w"] / 2, z["x"] + z["w"] / 2]
        ys += [z["y"] - z["h"] / 2, z["y"] + z["h"] / 2]
    for s in spots:
        if s["purpose"] in ("Park", "EntrySpot", "ExitSpot"):
            xs.append(s["x"])
            ys.append(s["y"])
    if not xs:
        return None
    return {"min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)}


def load_geometry(level: str) -> dict[str, Any]:
    """Every drawable element of ``level`` as lists, in simulator world coordinates."""
    with _lock:
        if level in _geometry_cache:
            return _geometry_cache[level]
        source = _find_level_file(level)
        if source is None:
            return _empty_geometry()
        raw = json.loads(source.read_text(encoding="utf-8-sig"))

        zones = [{"name": z["Name"], "x": float(z["X"]), "y": float(z["Y"]),
                  "w": float(z.get("Width", 0)), "h": float(z.get("Height", 0)),
                  "type": z.get("ZoneType", "Open")} for z in raw.get("Zones", [])]
        spots = [{"name": s["Name"], "x": float(s["X"]), "y": float(s["Y"]),
                  "rotation": float(s.get("Rotation", 0)), "purpose": s.get("Purpose", "Park"),
                  "car_type": s.get("CarType", "Any"), "zone": s.get("ZoneParent", "")}
                 for s in raw.get("ParkingSpots", [])]
        gates = [{"name": g["Name"], "x": float(g["X"]), "y": float(g["Y"]),
                  "rotation": float(g.get("Rotation", 0)), "zone": g.get("ZoneParent", "")}
                 for g in raw.get("Gates", [])]
        fans = [{"name": f["Name"], "x": float(f["X"]), "y": float(f["Y"]),
                 "zone": f.get("ZoneParent", "")} for f in raw.get("Exhausts", [])]
        lights = [{"name": li["Name"], "x": float(li["X"]), "y": float(li["Y"]),
                   "zone": li.get("ZoneParent", ""), "group": li.get("Group", "")}
                  for li in raw.get("Lights", [])]

        geometry = {"level": level, "zones": zones, "spots": spots, "gates": gates,
                    "fans": fans, "lights": lights, "bounds": _geometry_bounds(zones, spots)}
        _geometry_cache[level] = geometry
        return geometry


def detect_level(names: Iterable[str]) -> Optional[str]:
    """The level whose spot names best match ``names``, if the match is convincing.

    Jaccard similarity (shared / combined) rather than plain overlap: level 1's
    names are a subset of level 2's, so overlap alone would call level 1 "level 2".
    """
    live = set(names)
    if not live:
        return None
    best, best_score = None, 0.0
    for level in LEVELS:
        known = {s["name"] for s in load_geometry(level)["spots"]}
        if not known:
            continue
        score = len(live & known) / len(live | known)
        if score > best_score:
            best, best_score = level, score
    return best if best_score >= MATCH_THRESHOLD else None


_announced_level: Optional[str] = None


def announce_level(level: Optional[str]) -> None:
    """The level the simulator console says it just loaded, before any bays sync."""
    global _announced_level
    _announced_level = level


def running_level() -> Optional[str]:
    """Level of the live park (from synced spot names), else the level the
    simulator announced loading, else the configured seed level."""
    from app.state import state  # local import: layout must not depend on state at import time
    return detect_level(list(state.spots.keys())) or _announced_level or (settings.seed_from_level or None)


def current_geometry() -> dict[str, Any]:
    level = running_level()
    return load_geometry(level) if level else _empty_geometry()
