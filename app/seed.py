"""Load park layout from the simulator's level file, for offline development.

The live source of truth is always the startup sync against the REST API. This
fallback exists so the dispatcher can be developed and tested with the
simulator closed -- otherwise every code change needs the game running.

Enabled with ``SEED_FROM_LEVEL=lvl1`` and only used when the startup sync
fails.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("dispatcher.seed")

_CANDIDATE_DIRS = [
    Path("ParkingSimulator-win-x64/ParkingSimulator-win-x64/settings"),
    Path("settings"),
    Path("."),
]


def _find_level(level: str) -> Path | None:
    for directory in _CANDIDATE_DIRS:
        candidate = directory / f"{level}.json"
        if candidate.exists():
            return candidate
    return None


def load_level(level: str) -> dict[str, list[dict[str, Any]]] | None:
    """Return payloads shaped like the REST list-* endpoints, or None."""
    source = _find_level(level)
    if source is None:
        log.error("seed: could not find %s.json", level)
        return None

    raw = json.loads(source.read_text(encoding="utf-8-sig"))

    spots = [
        {
            "name": s["Name"],
            "purpose": s.get("Purpose", "Park"),
            "parkingForCarType": s.get("CarType", "Any"),
            "zoneParent": s.get("ZoneParent", ""),
            "detectedCars": [],
            "broken": False,
            "isUnderMaintenance": False,
        }
        for s in raw.get("ParkingSpots", [])
    ]
    barriers = [
        {
            "name": g["Name"],
            "zoneParent": g.get("ZoneParent", ""),
            "broken": False,
            "isUnderMaintenance": False,
            "state": g.get("State", "Closed"),
        }
        for g in raw.get("Gates", [])
    ]
    fans = [
        {
            "name": f["Name"],
            "zoneParent": f.get("ZoneParent", ""),
            "broken": False,
            "isUnderMaintenance": False,
            "isOn": bool(f.get("IsOn")),
        }
        for f in raw.get("Exhausts", [])
    ]
    zones = [
        {"name": z["Name"], "gasCarbonMonoxideLevel": 0, "risk": "Safe"}
        for z in raw.get("Zones", [])
    ]

    log.info(
        "seed: loaded %s from %s (%d spots, %d gates, %d fans, %d zones)",
        level, source, len(spots), len(barriers), len(fans), len(zones),
    )
    return {"spots": spots, "barriers": barriers, "fans": fans, "zones": zones}
