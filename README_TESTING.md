# Route optimization test harness

Two things live in `tests/`:

- **`mock_sim_server.py`** - a standalone FastAPI stand-in for the real
  `ParkingSimulator-win-x64` binary. Same REST surface (`auth/login`,
  `list-parking-spots`, `list-barriers`, `list-exhaust-fans`, `list-zones`,
  `car/{name}/goto/{dest}`, `car/{name}/charge`, barrier/fan/spot control),
  plus `/_mock/*` control endpoints to script scenarios and fire webhooks -
  useful because the real binary is Windows-only and can't run in CI.
- **`run_route_bench.py`** - the benchmark harness, two independent modes.

## Setup

```bash
pip install -r requirements-test.txt
```

## Mode B: concurrency stress test (no simulator needed)

Seeds a synthetic lot in memory and hammers `ParkingState.reserve_spot` with
real OS threads dispatching simultaneously, to prove the reservation lock
actually prevents two cars being sent to the same spot under load - this is
the `Penalty_SendCarToOccupiedSpot` guard, verified under concurrency rather
than assumed from reading the code.

```bash
python -m tests.run_route_bench --mode stress --cars 500 --spots 120 --concurrency 32
python -m tests.run_route_bench --mode stress --weighted   # exercise rank_spots_weighted instead
```

Exit code is non-zero if any spot was double-booked, or if fewer cars parked
than the lot had room for. Prints throughput (dispatches/sec) and ranking
latency percentiles (p50/p95).

Sample output:

```
=== Mode B: synthetic concurrency stress ===
  cars=500 spots=120 gates=2 concurrency=32 weighted=False
  wall time: 0.412s  throughput: 1213.6 dispatches/sec
  successes: 120/500  failures (lot full / lost race and exhausted retries): 380
  double-booked spots (must be 0 - this is the Penalty_SendCarToOccupiedSpot guard): 0
  ranking latency: n=500 mean=0.041ms p50=0.031ms p95=0.089ms max=0.412ms
  average reservation attempts per success (>1 means real lost races, expected under load): 1.08
  RESULT: PASS
```

`successes` caps at `--spots` - that's the lot filling up, not a bug; the
number that matters is the duplicate count, which must always be `0`.

## Mode A: live simulator ranking + optional dispatch

Connects to whatever `SIMULATOR_BASE_URL` in your `.env` currently points
at - the real game **or** `mock_sim_server.py`. Calls `list-parking-spots`
exactly once (never polls, per the organizer's documented rule) and
benchmarks `find_best_spot` ranking latency against that one snapshot.

```bash
# read-only: ranks candidates, never touches the simulator
python -m tests.run_route_bench --mode live --trials 20

# also sends real goto commands (synthetic BENCH#### plates - watch the
# dashboard, they'll show up as real dispatched cars)
python -m tests.run_route_bench --mode live --dispatch --trials 10
```

### Running Mode A against the mock instead of the real game

```bash
uvicorn tests.mock_sim_server:app --port 9898
# separate shell, with SIMULATOR_BASE_URL=http://127.0.0.1:9898 in .env
python -m tests.run_route_bench --mode live --dispatch --trials 20
```

### Both modes in one run

```bash
python -m tests.run_route_bench --mode both --dispatch
```

## Scripting scenarios against the mock

```bash
curl -X POST http://127.0.0.1:9898/_mock/reset \
  -H "Content-Type: application/json" \
  -d '{"park_spots": 30, "webhook_url": "http://127.0.0.1:8080/webhooks/simulator"}'

curl -X POST http://127.0.0.1:9898/_mock/break \
  -H "Content-Type: application/json" \
  -d '{"name": "S3", "component_type": "ParkingSpot", "fine_amount": 10}'

curl -X POST http://127.0.0.1:9898/_mock/emit \
  -H "Content-Type: application/json" \
  -d '{"payload": {"EventClass": "car_spot_action", "CarPlateNumber": "TST 001", "SpotName": "S3", "SpotType": "Park", "Direction": "CarOut"}}'

curl http://127.0.0.1:9898/_mock/webhooks   # everything the mock has sent so far
```

Point the real dispatcher at the mock (`SIMULATOR_BASE_URL=http://127.0.0.1:9898`,
`WebhookUrl` in the mock's reset call pointing back at
`http://127.0.0.1:8080/webhooks/simulator`) to exercise the full webhook
pipeline - reservation, billing, payment validation, penalty handling -
without the real binary at all.
