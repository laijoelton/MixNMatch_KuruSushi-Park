"""Extract the simulator's road graph into a clean file for the pathfinder.

The level config is a 38KB blob with rendering data mixed in. This pulls out
just the navigation-relevant parts so the C/C++ module does not have to parse
it:

    python -m scripts.export_graph            # lvl2 -> data/graph.json
    python -m scripts.export_graph lvl1       # or any other level, explicitly

Output shape:

    {
      "level": "lvl2",
      "nodes":  [{"name": "P2", "x": 206.99, "y": 951.96, "emitter": false}],
      "edges":  [{"from": "P3", "to": "P6", "direction": 0}],
      "spots":  [{"name": "S3", "x": 665, "y": 713, "zone": "ZONE1",
                  "purpose": "Park", "car_type": "Any"}],
      "gates":  [{"name": "gateA", "x": 0, "y": 0, "zone": "ZONE1"}]
    }

``direction`` is carried through verbatim from the simulator: treat a
connection as one-way unless you confirm otherwise by observation.

The pathfinder should emit data/distances.json:

    {"ENTRY1": {"S1": 340.2, "S3": 512.8, ...}}

``app/routing.py`` picks that up automatically at startup.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

LEVEL = sys.argv[1] if len(sys.argv) > 1 else "lvl2"

CANDIDATES = [
    Path("ParkingSimulator-win-x64/ParkingSimulator-win-x64/settings") / f"{LEVEL}.json",
    Path("settings") / f"{LEVEL}.json",
    Path(f"{LEVEL}.json"),
]

source = next((p for p in CANDIDATES if p.exists()), None)
if source is None:
    print(f"Could not find {LEVEL}.json. Looked in:")
    for c in CANDIDATES:
        print("  ", c)
    raise SystemExit(1)

raw = json.loads(source.read_text(encoding="utf-8-sig"))

nodes: list[dict] = []
edges: list[dict] = []
seen_nodes: set[str] = set()

for path in raw.get("Paths", []):
    for point in path.get("Points", []):
        name = point.get("Name")
        if not name or name in seen_nodes:
            continue
        seen_nodes.add(name)
        nodes.append(
            {
                "name": name,
                "x": point.get("X"),
                "y": point.get("Y"),
                "emitter": bool(point.get("IsCarEmitter")),
                "drain": bool(point.get("IsDrain")),
            }
        )
    for conn in path.get("Connections", []):
        edges.append(
            {
                "from": conn.get("From"),
                "to": conn.get("To"),
                "direction": conn.get("Direction"),
            }
        )

spots = [
    {
        "name": s["Name"],
        "x": s.get("X"),
        "y": s.get("Y"),
        "zone": s.get("ZoneParent") or None,
        "purpose": s.get("Purpose"),
        "car_type": s.get("CarType"),
    }
    for s in raw.get("ParkingSpots", [])
]

gates = [
    {
        "name": g["Name"],
        "x": g.get("X"),
        "y": g.get("Y"),
        "zone": g.get("ZoneParent") or None,
    }
    for g in raw.get("Gates", [])
]

out = {"level": LEVEL, "nodes": nodes, "edges": edges, "spots": spots, "gates": gates}

dest = Path("data/graph.json")
dest.parent.mkdir(parents=True, exist_ok=True)
dest.write_text(json.dumps(out, indent=2), encoding="utf-8")

# Edges referencing nodes that carry no coordinates cannot be weighted by
# distance; the pathfinder needs to know before it starts.
missing = {e["from"] for e in edges if e["from"] not in seen_nodes} | {
    e["to"] for e in edges if e["to"] not in seen_nodes
}

print(f"source : {source}")
print(f"wrote  : {dest}")
print(f"  nodes : {len(nodes)}")
print(f"  edges : {len(edges)}")
print(f"  spots : {len(spots)}  ({sum(1 for s in spots if s['purpose'] == 'Park')} parkable)")
print(f"  gates : {len(gates)}")
if missing:
    print(f"\n  NOTE: {len(missing)} edge endpoint(s) have no coordinate entry:")
    print("        " + ", ".join(sorted(missing)[:15]))
    print("        Treat these as junctions with unknown position.")
print("\nnext: pathfinder reads data/graph.json, writes data/distances.json")
