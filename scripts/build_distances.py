"""Compute true driving distances over the simulator's road graph.

    python -m scripts.export_graph        # first: lvl1.json -> data/graph.json
    python -m scripts.build_distances     # then:  graph.json -> data/distances.json

`app/routing.py` loads `data/distances.json` at startup and uses real driving
distance instead of the name-ordered synthetic ring.

This is the reference implementation. It is deliberately plain Dijkstra so the
C/C++ pathfinder has something to validate against: same graph in, same numbers
out. If the two disagree, one of them is wrong.


What the graph actually looks like (measured, lvl1)
---------------------------------------------------

* 63 nodes, 62 edges. The main component is 60 nodes / 60 edges, so it contains
  exactly **one cycle** -- it is a tree with a single loop. There is essentially
  one path between any two points, which is why pathfinding is not where the
  wins are.

* **Edges are bidirectional.** The `Direction` field is not a one-way flag.
  Treating `From -> To` as directed reaches 0 of 30 parking spots from ENTRY1;
  undirected reaches all 30. Direction appears to be a rendering/heading hint.

* Parking spots are not graph nodes. Each sits ~97 units off its nearest node
  (the stub from the aisle into the bay), and the mapping is 1:1 -- 30 spots,
  30 distinct nodes, no collisions.

Cost model
----------

    cost(entry, spot) = dijkstra(entry_node, spot_node) + stub_length

and, when `--round-trip` is passed:

    cost += dijkstra(spot_node, nearest_exit_node)

The outbound leg matters: a bay close to the entrance but far from any exit
costs the same total driving, just spread differently, and the return trip is
what generates CO while the lot is busy.
"""
from __future__ import annotations

import heapq
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

GRAPH = Path("data/graph.json")
OUT = Path("data/distances.json")
ROUND_TRIP = "--round-trip" in sys.argv


def euclid(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


def load_graph() -> dict:
    if not GRAPH.exists():
        print(f"{GRAPH} not found. Run:  python -m scripts.export_graph")
        raise SystemExit(1)
    return json.loads(GRAPH.read_text(encoding="utf-8"))


def build_adjacency(nodes: dict, edges: list) -> dict[str, list[tuple[str, float]]]:
    """Undirected adjacency weighted by straight-line distance between nodes."""
    adj: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for e in edges:
        a, b = e.get("from"), e.get("to")
        if a not in nodes or b not in nodes:
            continue
        na, nb = nodes[a], nodes[b]
        w = euclid(na["x"], na["y"], nb["x"], nb["y"])
        adj[a].append((b, w))
        adj[b].append((a, w))  # see module docstring: not one-way
    return adj


def dijkstra(adj: dict[str, list[tuple[str, float]]], source: str) -> dict[str, float]:
    dist = {source: 0.0}
    pq = [(0.0, source)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, math.inf):
            continue
        for v, w in adj.get(u, ()):
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return dist


def nearest_node(nodes: dict, x: float, y: float) -> tuple[str, float]:
    """Attach a spot/gate to the road network, returning the stub length too."""
    best, best_d = None, math.inf
    for name, n in nodes.items():
        d = euclid(x, y, n["x"], n["y"])
        if d < best_d:
            best, best_d = name, d
    return best, best_d


def main() -> None:
    g = load_graph()
    nodes = {n["name"]: n for n in g["nodes"] if n.get("x") is not None}
    adj = build_adjacency(nodes, g["edges"])

    parks = [s for s in g["spots"] if s["purpose"] == "Park"]
    entries = [s for s in g["spots"] if s["purpose"] == "EntrySpot"]
    exits_ = [s for s in g["spots"] if s["purpose"] in ("ExitSpot", "LeaveParking")]

    if not entries:
        print("no EntrySpot in the graph - nothing to measure from")
        raise SystemExit(1)

    spot_attach = {s["name"]: nearest_node(nodes, s["x"], s["y"]) for s in parks}
    exit_attach = {s["name"]: nearest_node(nodes, s["x"], s["y"]) for s in exits_}

    # Outbound leg: spot -> closest exit. Computed once per spot.
    exit_cost: dict[str, float] = {}
    if ROUND_TRIP and exit_attach:
        for spot, (snode, sstub) in spot_attach.items():
            d = dijkstra(adj, snode)
            best = math.inf
            for _, (enode, estub) in exit_attach.items():
                if enode in d:
                    best = min(best, d[enode] + estub)
            exit_cost[spot] = 0.0 if best is math.inf else best

    table: dict[str, dict[str, float]] = {}
    unreachable: list[str] = []

    for entry in entries:
        enode, estub = nearest_node(nodes, entry["x"], entry["y"])
        d = dijkstra(adj, enode)
        row: dict[str, float] = {}
        for spot, (snode, sstub) in spot_attach.items():
            if snode not in d:
                unreachable.append(f"{entry['name']}->{spot}")
                continue
            cost = estub + d[snode] + sstub
            if ROUND_TRIP:
                cost += exit_cost.get(spot, 0.0)
            row[spot] = round(cost, 1)
        table[entry["name"]] = row
        print(f"{entry['name']:<10} attaches at {enode:<6} -> {len(row)} spots reachable")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(table, indent=1, sort_keys=True), encoding="utf-8")

    print(f"\nwrote {OUT}  (mode: {'round-trip' if ROUND_TRIP else 'inbound only'})")
    if unreachable:
        print(f"  WARNING: {len(unreachable)} unreachable pair(s): {unreachable[:5]}")

    # A quick sanity read: nearest and furthest bays from the first entry.
    first = entries[0]["name"]
    row = table[first]
    if row:
        ranked = sorted(row.items(), key=lambda kv: kv[1])
        print(f"\nfrom {first}:")
        print("  nearest :", ", ".join(f"{k}={v:.0f}" for k, v in ranked[:5]))
        print("  furthest:", ", ".join(f"{k}={v:.0f}" for k, v in ranked[-5:]))
        spread = ranked[-1][1] / ranked[0][1] if ranked[0][1] else 0
        print(f"  furthest is {spread:.1f}x the nearest "
              f"-- that ratio is the headroom good routing can win back")


if __name__ == "__main__":
    main()
