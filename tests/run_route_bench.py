"""Route optimization test harness for the Grand Park Auto dispatcher.

Two independent modes, run with ``--mode``:

``stress`` (default, no simulator needed)
    Hammers ``app.state.ParkingState.reserve_spot`` with real concurrent OS
    threads dispatching against a synthetic lot, to prove the reservation
    lock actually prevents ``Penalty_SendCarToOccupiedSpot`` under load
    rather than just looking correct in single-threaded testing. Reports
    throughput and ranking-latency percentiles. Deterministic, fast, CI-safe.

``live``
    Connects to a real running simulator (or tests/mock_sim_server.py) via
    the same ``app.client.client`` the dispatcher uses, performs the startup
    sync exactly once - never polls list-* in a loop, per the organizer's
    documented rule - then benchmarks ranking latency against that live spot
    list. Pass ``--dispatch`` to also send real ``goto`` commands and measure
    end-to-end REST latency; without it, this mode only reads.

Usage::

    python -m tests.run_route_bench --mode stress --cars 500 --spots 120 --concurrency 32
    python -m tests.run_route_bench --mode live                     # read-only
    python -m tests.run_route_bench --mode live --dispatch --trials 20
"""
from __future__ import annotations

import argparse
import asyncio
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, ".")

from app.routing import (  # noqa: E402
    RankingWeights, find_best_spot, find_best_spot_weighted, ring,
)
from app.state import ParkingState  # noqa: E402


# --------------------------------------------------------------------------- #
# Shared reporting
# --------------------------------------------------------------------------- #
def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round(p * (len(s) - 1))))
    return s[idx]


def _print_latency(label: str, samples_ms: list[float]) -> None:
    if not samples_ms:
        print(f"  {label}: no samples")
        return
    print(f"  {label}: n={len(samples_ms)} "
          f"mean={statistics.mean(samples_ms):.3f}ms "
          f"p50={_pct(samples_ms, 0.50):.3f}ms "
          f"p95={_pct(samples_ms, 0.95):.3f}ms "
          f"max={max(samples_ms):.3f}ms")


# --------------------------------------------------------------------------- #
# Mode B: synthetic concurrency stress test
# --------------------------------------------------------------------------- #
@dataclass
class DispatchOutcome:
    plate: str
    spot: Optional[str]
    ok: bool
    rank_latency_ms: float
    attempts: int


def _seed_synthetic_state(num_spots: int, num_gates: int) -> ParkingState:
    state = ParkingState(max_processed_events=10000)
    spots_payload = []
    for g in range(num_gates):
        spots_payload.append({"name": f"GATE{g}", "purpose": "EntrySpot",
                              "parkingForCarType": "Any", "zoneParent": "",
                              "detectedCars": [], "broken": False, "isUnderMaintenance": False})
    zones = ["ZONE_A", "ZONE_B", "ZONE_C"]
    for i in range(num_spots):
        spots_payload.append({"name": f"S{i}", "purpose": "Park", "parkingForCarType": "Any",
                              "zoneParent": zones[i % len(zones)],
                              "detectedCars": [], "broken": False, "isUnderMaintenance": False})
    state.load_spots(spots_payload)
    ring.rebuild(list(state.spots.keys()))
    return state


def _zone_congestion(state: ParkingState) -> dict[str, float]:
    """Live occupancy ratio per zone - recomputed per trial, same as a real
    dispatcher would derive it from ParkingState.snapshot()."""
    totals: dict[str, int] = {}
    occupied: dict[str, int] = {}
    for spot in state.spots.values():
        if spot.purpose != "Park":
            continue
        totals[spot.zone_parent] = totals.get(spot.zone_parent, 0) + 1
        if spot.status.value in ("OCCUPIED", "RESERVED"):
            occupied[spot.zone_parent] = occupied.get(spot.zone_parent, 0) + 1
    return {z: occupied.get(z, 0) / totals[z] for z in totals}


def _zone_of(state: ParkingState) -> dict[str, str]:
    return {s.name: s.zone_parent for s in state.spots.values() if s.purpose == "Park"}


def _one_dispatch(state: ParkingState, gates: list[str], weighted: bool, max_attempts: int) -> DispatchOutcome:
    plate = f"CAR{random.randint(100000, 999999)}"
    gate = random.choice(gates)

    t0 = time.perf_counter()
    if weighted:
        candidates = state.available_spots()
        ranked = find_best_spot_weighted(
            gate, candidates, zone_of=_zone_of(state), zone_congestion=_zone_congestion(state),
            weights=RankingWeights(distance=1.0, congestion=8.0),
        )
        ordered = [ranked] if ranked else []
        # Fall back to the full ranking if the top pick loses a race below.
        if ranked is None:
            ordered = []
    else:
        candidates = state.available_spots()
        ordered = [find_best_spot(gate, candidates)]
    rank_latency_ms = (time.perf_counter() - t0) * 1000

    attempts = 0
    for _ in range(max_attempts):
        pool = state.available_spots()
        if not pool:
            return DispatchOutcome(plate, None, False, rank_latency_ms, attempts)
        target = (find_best_spot_weighted(gate, pool, zone_of=_zone_of(state),
                                          zone_congestion=_zone_congestion(state),
                                          weights=RankingWeights(distance=1.0, congestion=8.0))
                 if weighted else find_best_spot(gate, pool))
        if target is None:
            return DispatchOutcome(plate, None, False, rank_latency_ms, attempts)
        attempts += 1
        if state.reserve_spot(target, plate):
            return DispatchOutcome(plate, target, True, rank_latency_ms, attempts)
        # Lost the race for that spot to another thread - loop and re-rank.
    return DispatchOutcome(plate, None, False, rank_latency_ms, attempts)


def run_stress(num_cars: int, num_spots: int, num_gates: int, concurrency: int, weighted: bool) -> bool:
    print(f"=== Mode B: synthetic concurrency stress ===")
    print(f"  cars={num_cars} spots={num_spots} gates={num_gates} "
          f"concurrency={concurrency} weighted={weighted}")

    state = _seed_synthetic_state(num_spots, num_gates)
    gates = [n for n, s in state.spots.items() if s.purpose == "EntrySpot"]

    outcomes: list[DispatchOutcome] = []
    wall_t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_one_dispatch, state, gates, weighted, 8) for _ in range(num_cars)]
        for fut in as_completed(futures):
            outcomes.append(fut.result())
    wall_s = time.perf_counter() - wall_t0

    successes = [o for o in outcomes if o.ok]
    failures = [o for o in outcomes if not o.ok]
    assigned_spots = [o.spot for o in successes]
    duplicates = len(assigned_spots) - len(set(assigned_spots))

    print(f"  wall time: {wall_s:.3f}s  throughput: {num_cars / wall_s:.1f} dispatches/sec")
    print(f"  successes: {len(successes)}/{num_cars}  failures (lot full / lost race and exhausted retries): {len(failures)}")
    print(f"  double-booked spots (must be 0 - this is the Penalty_SendCarToOccupiedSpot guard): {duplicates}")
    _print_latency("ranking latency", [o.rank_latency_ms for o in outcomes])
    if successes:
        avg_attempts = statistics.mean(o.attempts for o in successes)
        print(f"  average reservation attempts per success (>1 means real lost races, expected under load): {avg_attempts:.2f}")

    ok = duplicates == 0 and len(successes) == min(num_cars, num_spots)
    print("  RESULT:", "PASS" if ok else "FAIL")
    return ok


# --------------------------------------------------------------------------- #
# Mode A: live simulator (or mock) benchmark
# --------------------------------------------------------------------------- #
async def run_live(dispatch: bool, trials: int) -> bool:
    print("=== Mode A: live simulator ranking + optional dispatch ===")
    from app.client import client  # imported here so `stress` mode never needs SIMULATOR_* configured

    t0 = time.perf_counter()
    await client.login()
    login_ms = (time.perf_counter() - t0) * 1000
    print(f"  login: {login_ms:.1f}ms")

    # Exactly one list-* round trip, per the organizer's no-polling rule -
    # everything below re-uses this single snapshot.
    t0 = time.perf_counter()
    spots = await client.list_parking_spots()
    sync_ms = (time.perf_counter() - t0) * 1000
    print(f"  list-parking-spots: {sync_ms:.1f}ms, {len(spots)} spots")

    def occupied(item: dict) -> bool:
        d = item.get("detectedCars")
        return bool(d) if isinstance(d, list) else bool(d)

    available = [s["name"] for s in spots if s.get("purpose") == "Park"
                and not occupied(s) and not s.get("broken") and not s.get("isUnderMaintenance")]
    gates = [s["name"] for s in spots if s.get("purpose") == "EntrySpot"]
    ring.rebuild([s["name"] for s in spots])

    if not gates:
        print("  no EntrySpot found in this level - cannot benchmark ranking")
        return False
    if not available:
        print("  no available Park spots right now - lot is full, nothing to rank")
        return len(available) == 0  # not a harness failure, just nothing to test

    rank_samples: list[float] = []
    dispatch_samples: list[float] = []
    dispatched: list[str] = []

    for i in range(trials):
        gate = random.choice(gates)
        t0 = time.perf_counter()
        pool = [s for s in available if s not in dispatched]
        target = find_best_spot(gate, pool)
        rank_samples.append((time.perf_counter() - t0) * 1000)
        if target is None:
            break

        if dispatch:
            plate = f"BENCH{i:04d}"
            t0 = time.perf_counter()
            await client.car_goto(plate, target)
            dispatch_samples.append((time.perf_counter() - t0) * 1000)
            dispatched.append(target)
            print(f"  [{i+1}/{trials}] {gate} -> {target} for {plate} "
                  f"({dispatch_samples[-1]:.1f}ms)")
        else:
            print(f"  [{i+1}/{trials}] {gate} -> {target} (dry run, not sent)")

    _print_latency("ranking latency", rank_samples)
    if dispatch_samples:
        _print_latency("goto REST latency", dispatch_samples)
        print(f"  NOTE: {len(dispatched)} synthetic BENCH#### plates were actually dispatched "
              f"in the live simulator - they will show up on the dashboard.")

    print("  RESULT: PASS")
    return True


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["stress", "live", "both"], default="stress")
    parser.add_argument("--cars", type=int, default=300, help="[stress] total simulated arrivals")
    parser.add_argument("--spots", type=int, default=80, help="[stress] synthetic Park spot count")
    parser.add_argument("--gates", type=int, default=2, help="[stress] synthetic EntrySpot count")
    parser.add_argument("--concurrency", type=int, default=16, help="[stress] worker threads")
    parser.add_argument("--weighted", action="store_true", help="[stress] use rank_spots_weighted instead of plain ring distance")
    parser.add_argument("--trials", type=int, default=10, help="[live] number of spots to rank/dispatch")
    parser.add_argument("--dispatch", action="store_true", help="[live] actually send goto commands (default: dry run)")
    args = parser.parse_args()

    ok = True
    if args.mode in ("stress", "both"):
        ok = run_stress(args.cars, args.spots, args.gates, args.concurrency, args.weighted) and ok
    if args.mode in ("live", "both"):
        ok = asyncio.run(run_live(args.dispatch, args.trials)) and ok

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
