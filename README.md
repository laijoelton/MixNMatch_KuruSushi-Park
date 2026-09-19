# KuruSushi-Park Dispatcher — Grand Park Auto Supervisory Layer

HackMY IoT 2026 · Track 2: Next-Gen Parking Solutions

**Staff-facing dashboards layered on the `feature/simulator-core` dispatch
engine**, without changing any of that engine's dispatch, penalty-mitigation
or webhook logic. `main` is the full, unified system.

Every page requires a staff sign-in. The only unauthenticated surfaces are
`/healthz` and `/webhooks/simulator` — the simulator has no cookie to send us.

## Dashboards

| Route | Audience | What it shows |
|---|---|---|
| `GET /` | Operator | Dark-mode HUD: telemetry cards (available/occupied/reserved/broken, active sessions, penalty count & fines, maintenance queue depth), an HTML5 Canvas digital twin of the dispatch ring, live barrier list, broken-component list, activity log, penalty log, and manual controls (sync, simulate arrival/dry-run, open/close barriers). |
| `GET /dashboard` | Operator | Full-width canvas over the real simulator geometry, with animated dispatch paths and per-plate route tracking. |
| `GET /admin` | All staff | Role-gated console: zones, gate control, earnings, raw event log, session history — each pane rendered only for roles that hold the matching permission. |

All pages are pure observers: they render whatever `app/state.py::snapshot()`
already holds and push their own explicit actions through the same JSON API
the headless core exposes (`/api/manual/*`) — they never bypass the
dispatch/idempotency/penalty-mitigation logic described below.

**Live updates:** `app/ws_manager.py::ConnectionManager` fan-outs one state
snapshot per tick (`BROADCAST_INTERVAL_S`, default 1s) to every connected
browser over `/ws/live`. `static/js/canvas_twin.js` projects spots and
barriers onto the same deterministic sorted-name ring `app/routing.py` uses
for dispatch, so the twin's layout is numerically the same ring the router
reasons about — not a cosmetic approximation. Each socket is filtered to the
permissions of the session that opened it, so a broad feed cannot be used to
read around a narrower endpoint.

## System Architecture & Pitch Overview

The organizer's `ParkingSimulator-win-x64` binary is a closed test harness: it
owns vehicle generation, pathfinding, and physical movement internally, and
exposes only a REST control surface (`/api/v1`) plus outbound webhooks. This
service never touches that binary — it is a **stateless-on-restart, external
supervisory controller** that authenticates against the simulator, mirrors
its component state locally, and issues dispatch/maintenance/billing
commands strictly in reaction to webhook events.

The one piece of KuruSushi-Park's original kaiten-sushi conveyor design that
survives into this submission is its **circular shortest-path heuristic**:
`app/routing.py` ports `circular_delta` / `circular_distance` verbatim from
`KuruSushi-Park/core/conveyor_engine.py` and repurposes them. Since the
organizer's simulator exposes named components (`S1`, `ENTRY1`, …) with no
physical ring geometry, every station name is deterministically projected
onto a synthetic ring of size *N* (sorted-name ordering, rebuilt on every
sync). Arriving vehicles are then routed to the available spot with the
minimum bidirectional circular offset:

```
dist(T, G) = min((T - G) mod N, (G - T) mod N)
```

This gives the same "nearest slot on the loop" dispatch character the
original conveyor model was built around, without requiring — or being able
to require — any change to the organizer's compiled test harness.

## Event-Driven Hook Flow

**No polling, ever.** `GET list-parking-spots`, `list-barriers`, `list-zones`
and `list-exhaust-fans` are called exactly once, during application startup
(`app/main.py`'s `lifespan`), and again only if an operator explicitly calls
`POST /api/manual/sync`. From that point forward, `app/state.py` is mutated
exclusively by `POST /webhooks/simulator`.

| Inbound `EventClass`      | Local state mutation                          | Outbound simulator call(s) |
|----------------------------|------------------------------------------------|------------------------------|
| `car_spot_action` (`EntrySpot`/`CarIn`) | new session created                | `POST /car/{plate}/goto/{spot}` — dispatched immediately (mitigates `Penalty_CarLeftFromEntryBecauseNeglected`) |
| `car_spot_action` (`Park`/`CarIn`)      | spot → `OCCUPIED`                  | — |
| `car_spot_action` (`Park`/`CarOut`)     | spot → `AVAILABLE`/`BROKEN`/`MAINTENANCE`, deferred repair flushed | Queued `POST /parking-spots/{name}/repair` if one was pending |
| `car_spot_action` (`ExitSpot`/`CarIn`)  | session → `CHARGED` (idempotent)   | `POST /car/{plate}/charge` — exactly once, only here (mitigates `Penalty_ChargeCarForParkingTwice`, `Penalty_ChargeCarWhileNotAtExitSpot`) |
| `car_spot_action` (`ExitSpot`/`CarOut`) | session closed                     | — |
| `component_broken`        | component flagged broken; if a `ParkingSpot` is occupied, repair is deferred until it vacates (mitigates `Penalty_RepairAnOccupiedSpot`) | Queued `POST .../repair` |
| `component_fixed`         | broken/maintenance flags cleared               | — |
| `carbon_monoxide_event`   | zone CO level/danger recorded                  | `POST /exhaust-fans/{name}/on` for every fan in a `High`/`Critical` zone (mitigates `Penalty_ZonePollutedWithHighCO`) |
| `gate_action`              | barrier position mirrored                      | — |
| `payment_made`             | validated against the amount this service billed; mismatches are logged and rejected, not accepted blindly (handles "some payments are fake") | — |
| `penalty`                   | counted, fined, logged for observability       | — |
| `test_webhook`              | acknowledged                                    | — |

**Idempotency & ordering:** every payload's `EventId` is checked against a
bounded FIFO cache (`app/state.py::_BoundedEventCache`) before any handler
runs; duplicates are dropped. `SequenceId` is tracked to detect and log gaps
(missed webhook deliveries), without blocking processing of the event that
arrived.

**Spot fault avoidance:** `state.available_spots()` filters out any spot
that is not `AVAILABLE`, or is `broken`/`under_maintenance`, before routing
ever runs — this is what prevents `Penalty_SendCarToOccupiedSpot` and
`Penalty_SendCarToBrokenOrUnderMaintenanceSpot`.

**Never-die engine (fault tolerance):**
- `app/client.py`'s `SimulatorClient._request` retries transient network
  errors and 5xx responses with linear backoff (default 3 attempts) before
  giving up, and transparently re-authenticates on a `401`.
- `app/queue_worker.py`'s `PriorityTaskQueue` is a background asyncio worker
  that drains repair/maintenance calls off the webhook-handling critical
  path — a slow or failing simulator repair call is retried (default 5
  attempts, linear backoff) without ever blocking the next inbound webhook
  or crashing the process. Repairs are prioritized (`priority=10`) ahead of
  routine work.
- Every webhook handler runs inside a top-level `try/except` in
  `simulator_webhook` — a bad or unexpected payload is logged and
  acknowledged, never allowed to crash the receiver or drop the organizer's
  retry.

**Webhook signature verification:** `app/main.py::verify_signature`
reproduces the organizer's documented recipe — sort all payload fields
except `Signature` alphabetically, join their values with `|`, hash the
result. If `WEBHOOK_SECRET` is set, the hash is HMAC-keyed with it;
otherwise a plain digest is computed, matching the unsigned examples in the
organizer's docs. The digest algorithm is configurable (`WEBHOOK_HASH_ALGO`,
default `sha256`) since the exact algorithm is not stated in the published
Webhooks reference.

## JSON API Surface

| Method & Path | Purpose |
|---|---|
| `GET /` | Operator HUD page |
| `WS /ws/live` | Live state snapshot stream, filtered per role |
| `GET /healthz` | Liveness + counts + maintenance queue stats + connected dashboard clients |
| `GET /api/state` | Full state snapshot (spots, barriers, zones, fans, sessions, penalties, activity log) |
| `GET /api/spots` | Spot list + occupancy counts |
| `GET /api/gates` | Known entry-spot names |
| `GET /api/broken` | Currently broken/under-maintenance components + deferred repairs |
| `POST /api/manual/sync` | One-shot, operator-triggered re-sync of spots/barriers/zones/fans |
| `POST /api/manual/arrival` | Manually replay the entry-dispatch path for a plate (`dry_run: true` previews ranking only) |
| `POST /api/manual/barrier/{name}/open` \| `/close` | Manual barrier control |
| `POST /api/manual/repair/{name}` | Queue a repair for any component |
| `POST /webhooks/simulator` | Inbound event receiver from Grand Park Auto |

## Setup & Execution Guide

### 1. Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SIMULATOR_BASE_URL` | yes | `http://127.0.0.1:9898` | Base URL of the running `ParkingSimulator-win-x64` REST server (`ListenAddress` in `settings/settings.json`) |
| `SIMULATOR_EMAIL` | yes | — | Login `Name` from `settings/settings.json` (default build ships `admin`) |
| `SIMULATOR_PASSWORD` | yes | — | Login `Password` from `settings/settings.json` (default build ships `admin`) |
| `WEBHOOK_SECRET` | no | empty | Shared secret for HMAC-signing webhook verification |
| `WEBHOOK_HASH_ALGO` | no | `sha256` | Digest algorithm name (any `hashlib` algorithm) |
| `WEBHOOK_PORT` | no | `8080` | Port this service listens on |
| `SIMULATOR_TIMEOUT_S` | no | `10.0` | HTTP timeout for simulator REST calls |
| `MAX_PROCESSED_EVENTS` | no | `5000` | Size of the `EventId` dedup cache |
| `PARKING_RATE_PER_MINUTE` | no | `0.20` | Billing rate used to compute `parkingCost` |
| `MINIMUM_CHARGE` | no | `1.00` | Floor applied to computed parking cost |
| `BROADCAST_INTERVAL_S` | no | `1.0` | Dashboard snapshot push interval over `/ws/live` |

### 2. Install and run locally

```bash
pip install -r requirements.txt
export SIMULATOR_BASE_URL="http://127.0.0.1:9898"
export SIMULATOR_EMAIL="admin"
export SIMULATOR_PASSWORD="admin"
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### 3. Run with Docker

```bash
docker build -t kurusushi-dispatcher .
docker run -p 8080:8080 \
  -e SIMULATOR_BASE_URL="http://host.docker.internal:9898" \
  -e SIMULATOR_EMAIL="admin" \
  -e SIMULATOR_PASSWORD="admin" \
  -e WEBHOOK_SECRET="shared-secret" \
  kurusushi-dispatcher
```

### 4. Point the simulator at this service

1. Edit `ParkingSimulator-win-x64/ParkingSimulator-win-x64/settings/settings.json`
   and set `"WebhookUrl"` to `http://<this-host>:8080/webhooks/simulator`.
2. Launch `ParkingSimulator.exe`, select/start a level.
3. Confirm connectivity with `GET /api/v1/test` against the simulator — it
   should trigger a `test_webhook` event, logged by this service.

### 5. Verify end-to-end operation

1. `curl http://localhost:8080/healthz` — confirms startup sync populated
   spots/barriers/zones from the simulator.
2. Open `http://localhost:8080/` — the status dot should turn green
   ("live") and the digital twin should render one dot per synced spot.
3. A vehicle arriving at an entry spot in the simulator produces a real
   `dispatched <plate> from <gate> to <spot>` log line and activity-log
   entry, and the simulator visibly routes the car; the twin's matching dot
   pulses and turns teal (occupied).
4. Once parked, `GET /api/state` shows the session's phase as `PARKED`.
5. Sending it to `exit` charges exactly once at `ExitSpot/CarIn` and
   completes the session at `ExitSpot/CarOut`.
6. Breaking a component triggers a queued, automatically-applied repair,
   deferred correctly if the affected spot is occupied, and shows up in the
   HUD's broken-components panel until fixed.
7. Sign in as each of the four roles in turn (`admin`, `operator`,
   `accountant`, `engineer`) and confirm `/admin` renders only that role's
   panes — see `docs/CHALLENGE_CHECKLIST.md` for the permission table.

## Known hazards (read before a scored run)

### The webhook signature recipe is unconfirmed

The documented recipe -- drop `Signature`, sort remaining field names
alphabetically, join values with `|`, hash -- **does not reproduce the
signatures printed in the organizer's own samples.** Tested offline against
their `component_broken` sample across MD5/SHA1/SHA256, four separators, three
key orderings and several shared-secret guesses: no match. Their worked
example includes a `RealDateTime` field the sample payloads omit, so the
published samples are incomplete.

A fixed algorithm therefore rejects every real event. `app/signature.py` runs
in calibration mode instead: it scores 36 candidate recipes against live
traffic and, while `WEBHOOK_SIGNATURE_MODE=observe`, never rejects anything.

```bash
python -m scripts.signature_report      # after a few minutes of live traffic
```

Once a recipe sits at 100%, pin it and switch to enforce:

```
WEBHOOK_SIGNATURE_RECIPE=md5:pipe:alpha
WEBHOOK_SIGNATURE_MODE=enforce
```

If nothing matches, ask the organisers and **stay on observe**. Dropping real
events costs far more than accepting unverified ones.

### Billing is ambiguous and must be verified against a real car

The docs contradict themselves: one page says *"Charging cost: 1 per each
minute, multiply by 2 if electric"*, another says *"parking cost = total
minutes spent parking, multiplied by 2 if car is electric"*. Meanwhile the API
takes `parkingCost` and `chargingCost` separately, and there is a
`Penalty_ChargeCarForNoElectricityUsed` for billing electricity to a car that
used none.

`compute_charge()` encodes the reading that an electric car pays `minutes`
parking plus `minutes` electricity (2x total), and everything else pays
`minutes` with `chargingCost = 0`. Set `ELECTRIC_SPLIT_CHARGING=false` to bill
the 2x entirely as parking instead -- no code change needed.

Billing runs from the moment the car occupies its spot, not from the entry
sensor: the drive in is not parking time.

### AUTOPILOT gates every outbound command

`AUTOPILOT=false` (the default) logs each intended action as `[dry-run]` and
sends nothing. Watch a full car cycle, confirm the spot choices and charges
look right, then set `AUTOPILOT=true` and restart.

Note that a dry-run dispatch releases its spot reservation immediately --
otherwise the lot would fill with promises to cars that never move.

### SEED_FROM_LEVEL is a development aid, not a runtime mode

When the startup sync fails and `SEED_FROM_LEVEL` is set, the park layout is
loaded from `settings/<level>.json` so the dispatcher can be developed with
the simulator closed. It logs loudly:

```
running on SEEDED layout from lvl1 - NOT live simulator state
```

**Set `SEED_FROM_LEVEL=` (empty) before a scored run.** Driving a real
simulator from a fake layout is worse than not running at all.

### Routing distance is not physical distance

`app/routing.py` projects station names onto a synthetic ring, so "distance"
is how far apart two names sort alphabetically -- `S1` is adjacent to `S10`.
The simulator's level file contains the real road network (63 nodes, 62
directed edges, plus coordinates for every spot and gate).

```bash
python -m scripts.export_graph          # -> data/graph.json
```

A pathfinder over that graph writes `data/distances.json`:

```json
{"ENTRY1": {"S1": 340.2, "S3": 512.8}}
```

`app/routing.py` picks it up at startup and uses true driving distance when
present, falling back to the ring when it is not.

## Durability

Every webhook is written to SQLite (`app/db.py`) *before* any handler runs, so
a crash mid-decision cannot lose an event. `EventId` is the primary key, which
makes redelivery a database-level reject rather than something the application
has to remember. This also satisfies Level 1's requirement to log arrivals,
parking time, departures and charges in a searchable database.

| Endpoint | Purpose |
|---|---|
| `GET /api/history` | Completed parking sessions |
| `GET /api/events` | Raw webhook log (`?event_class=penalty`) |
| `GET /api/payments` | Payment audit, including suspect ones |
| `GET /api/signature-report` | Signature calibration tally |

## Offline development

```bash
python -m scripts.export_graph     # road graph for the pathfinder
python -m scripts.replay           # full car lifecycle, no simulator needed
AMOUNT=0.01 python -m scripts.replay   # watch fraud detection hold a car
```
