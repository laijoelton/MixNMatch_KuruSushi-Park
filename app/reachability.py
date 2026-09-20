"""Which bays a car can actually reach from the entrance it came in through.

Level 3 is not one car park. Its road network is two disconnected halves:

    ENTRY1, ENTRY2, ENTRY3   ->  90 bays  (ZONE1-3, indoor)
    Entry104, OENTRY1..4     -> 160 bays  (ZONE4-7, outdoor)

Nothing in the simulator's REST API says so, and a dispatcher that does not
know it sends an indoor car to an outdoor bay - where it cannot go. The car
circles until the driver gives up ("Car left from entry because driver felt
neglected."), its reservation expires, the bay is handed to someone else, and
whoever finally arrives is fined for parking in an occupied spot. It gets worse
the longer a run lasts, because the unreachable half stays emptier and so keeps
winning the zone-ratio comparison.

So we compute it ourselves, once per level, from the level file the simulator
ships: build its road graph, attach every spot and entry sensor to the nearest
node, and run Dijkstra from each entrance. That yields both the reachable set
and the real driving distance, which is a better bay ranking than the
name-ordered synthetic ring in ``app/routing.py``.

This is pure local computation over a file we already read for the map - no
`list-*` calls, nothing polled.
"""
from __future__ import annotations

import heapq
import json
import logging
import math
from collections import defaultdict
from pathlib import Path
from typing import Optional

from app.layout import _find_level_file

log = logging.getLogger("dispatcher.reach")

# level -> {entry sensor -> {bay -> driving distance}}
_TABLES: dict[str, dict[str, dict[str, float]]] = {}


def _euclid(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


def _dijkstra(adj: dict[str, list[tuple[str, float]]], source: str) -> dict[str, float]:
    dist = {source: 0.0}
    queue = [(0.0, source)]
    while queue:
        d, node = heapq.heappop(queue)
        if d > dist.get(node, math.inf):
            continue
        for neighbour, weight in adj.get(node, ()):
            step = d + weight
            if step < dist.get(neighbour, math.inf):
                dist[neighbour] = step
                heapq.heappush(queue, (step, neighbour))
    return dist


def _build(level: str) -> dict[str, dict[str, float]]:
    path = _find_level_file(level)
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("reachability: cannot read %s (%s)", path, exc)
        return {}

    nodes: dict[str, dict] = {}
    adj: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for road in raw.get("Paths", []):
        for point in road.get("Points", []):
            name = point.get("Name")
            if name and point.get("X") is not None and name not in nodes:
                nodes[name] = {"x": point["X"], "y": point["Y"]}
    for road in raw.get("Paths", []):
        for conn in road.get("Connections", []):
            a, b = conn.get("From"), conn.get("To")
            if a in nodes and b in nodes:
                weight = _euclid(nodes[a]["x"], nodes[a]["y"], nodes[b]["x"], nodes[b]["y"])
                # Undirected: the simulator's own paths are driven both ways.
                adj[a].append((b, weight))
                adj[b].append((a, weight))
    if not nodes:
        return {}

    def attach(x: float, y: float) -> tuple[Optional[str], float]:
        best, best_d = None, math.inf
        for name, node in nodes.items():
            d = _euclid(x, y, node["x"], node["y"])
            if d < best_d:
                best, best_d = name, d
        return best, best_d

    bays, entries = {}, {}
    for spot in raw.get("ParkingSpots", []):
        if spot.get("X") is None or spot.get("Y") is None:
            continue
        if spot.get("Purpose") == "Park":
            bays[spot["Name"]] = attach(spot["X"], spot["Y"])
        elif spot.get("Purpose") == "EntrySpot":
            entries[spot["Name"]] = attach(spot["X"], spot["Y"])

    table: dict[str, dict[str, float]] = {}
    for entry, (node, stub) in entries.items():
        if node is None:
            continue
        distances = _dijkstra(adj, node)
        reachable = {bay: round(stub + distances[bnode] + bstub, 1)
                     for bay, (bnode, bstub) in bays.items()
                     if bnode is not None and bnode in distances}
        if reachable:
            table[entry] = reachable
    if table:
        sizes = ", ".join(f"{name}:{len(hits)}" for name, hits in sorted(table.items()))
        log.info("reachability %s: %d bays, per entrance %s", level, len(bays), sizes)
    return table


def table_for(level: Optional[str]) -> dict[str, dict[str, float]]:
    """Reachable bays per entry sensor for ``level``, computed once and cached."""
    if not level:
        return {}
    if level not in _TABLES:
        _TABLES[level] = _build(level)
    return _TABLES[level]


def reachable_from(level: Optional[str], entry_sensor: str) -> Optional[dict[str, float]]:
    """Bay -> driving distance from ``entry_sensor``.

    ``None`` means "no opinion" - an unknown level or an entrance the table does
    not cover. Callers must treat that as "every bay is allowed" rather than
    turning cars away on missing data.
    """
    return table_for(level).get(entry_sensor)
