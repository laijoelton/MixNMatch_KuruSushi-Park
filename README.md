# KuruSushi-Park Dispatcher — Grand Park Auto Supervisory Layer

HackMY IoT 2026 · Track 2: Next-Gen Parking Solutions

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
organizer's simulator exposes named components (`S150`, `gate0`, …) with no
physical ring geometry, every station name is deterministically projected
onto a synthetic ring of size *N* (sorted-name ordering, assigned at
startup and extended on demand). Arriving vehicles are then routed to the
available spot with the minimum bidirectional circular offset:

```
dist(T, G) = min((T - G) mod N, (G - T) mod N)
```

This gives the same "nearest slot on the loop" dispatch character the
original conveyor model was built around, without requiring — or being able
to require — any change to the organizer's compiled test harness.

## Event-Driven Hook Flow

**No polling, ever.** `GET list-parking-spots`, `list-barriers`, `list-zones`
and `list-exhaust-fans` are called exactly once, during application startup
(`app/main.py`'s `lifespan`). From that point forward, `app/state.py` is
mutated exclusively by `POST /webhooks/simulator`.

| Inbound `EventClass`      | Local state mutation                          | Outbound simulator call(s) |
|----------------------------|------------------------------------------------|------------------------------|
| `car_spot_action` (`EntrySpot`/`CarIn`) | new session created                | `POST /car/{plate}/goto/{spot}` — dispatched immediately (mitigates `Penalty_CarLeftFromEntryBecauseNeglected`) |
| `car_spot_action` (`Park`/`CarIn`)      | spot → `OCCUPIED`                  | — |
| `car_spot_action` (`Park`/`CarOut`)     | spot → `AVAILABLE`/`BROKEN`/`MAINTENANCE`, deferred repair flushed | `POST /parking-spots/{name}/repair` if one was queued |
| `car_spot_action` (`ExitSpot`/`CarIn`)  | session → `CHARGED` (idempotent)   | `POST /car/{plate}/charge` — exactly once, only here (mitigates `Penalty_ChargeCarForParkingTwice`, `Penalty_ChargeCarWhileNotAtExitSpot`) |
| `car_spot_action` (`ExitSpot`/`CarOut`) | session closed                     | — |
| `component_broken`        | component flagged broken; if a `ParkingSpot` is occupied, repair is deferred until it vacates (mitigates `Penalty_RepairAnOccupiedSpot`) | `POST .../repair` |
| `component_fixed`         | broken/maintenance flags cleared               | — |
| `carbon_monoxide_event`   | zone CO level/danger recorded                  | `POST /exhaust-fans/{name}/on` for every fan in a `High`/`Critical` zone (mitigates `Penalty_ZonePollutedWithHighCO`) |
| `gate_action`              | barrier position mirrored                      | — |
| `payment_made`             | validated against the amount this service billed; mismatches are logged and rejected, not accepted blindly (handles "some payments are fake") | — |
| `penalty`                   | logged for observability                       | — |
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

**Webhook signature verification:** `app/main.py::verify_signature`
reproduces the organizer's documented recipe — sort all payload fields
except `Signature` alphabetically, join their values with `|`, hash the
result. If `WEBHOOK_SECRET` is set, the hash is HMAC-keyed with it;
otherwise a plain digest is computed, matching the unsigned examples in the
organizer's docs. The digest algorithm is configurable (`WEBHOOK_HASH_ALGO`,
default `sha256`) since the exact algorithm is not stated in the published
Webhooks reference — confirm it against the Participant Handbook and adjust
the environment variable rather than the code.

## Setup & Execution Guide

### 1. Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SIMULATOR_BASE_URL` | yes | `http://127.0.0.1:5000` | Base URL of the running `ParkingSimulator-win-x64` REST server |
| `SIMULATOR_EMAIL` | yes | — | Credential for `POST /api/v1/auth/login` |
| `SIMULATOR_PASSWORD` | yes | — | Credential for `POST /api/v1/auth/login` |
| `WEBHOOK_SECRET` | no | empty | Shared secret for HMAC-signing webhook verification |
| `WEBHOOK_HASH_ALGO` | no | `sha256` | Digest algorithm name (any `hashlib` algorithm) |
| `WEBHOOK_PORT` | no | `8080` | Port this service listens on |
| `SIMULATOR_TIMEOUT_S` | no | `10.0` | HTTP timeout for simulator REST calls |
| `MAX_PROCESSED_EVENTS` | no | `5000` | Size of the `EventId` dedup cache |
| `PARKING_RATE_PER_MINUTE` | no | `0.20` | Billing rate used to compute `parkingCost` |
| `MINIMUM_CHARGE` | no | `1.00` | Floor applied to computed parking cost |

### 2. Install and run locally

```bash
pip install -r requirements.txt
export SIMULATOR_BASE_URL="http://127.0.0.1:5000"
export SIMULATOR_EMAIL="team@example.com"
export SIMULATOR_PASSWORD="changeme"
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### 3. Run with Docker

```bash
docker build -t kurusushi-dispatcher .
docker run -p 8080:8080 \
  -e SIMULATOR_BASE_URL="http://host.docker.internal:5000" \
  -e SIMULATOR_EMAIL="team@example.com" \
  -e SIMULATOR_PASSWORD="changeme" \
  -e WEBHOOK_SECRET="shared-secret" \
  kurusushi-dispatcher
```

### 4. Point the simulator at this service

1. Launch `ParkingSimulator-win-x64`.
2. Configure its webhook target to `http://<this-host>:8080/webhooks/simulator`.
3. Confirm connectivity with `GET /api/v1/test` against the simulator — it
   should trigger a `test_webhook` event, logged by this service as
   `test webhook received: <EventId>`.

### 5. Verify end-to-end operation

1. `curl http://localhost:8080/healthz` — confirms startup sync populated
   spots/barriers/zones from the simulator.
2. Drive (or script) a vehicle to an entry spot in the simulator — this
   service should log `dispatched <plate> from <gate> to <spot>` and the
   simulator should visibly route the car.
3. Move that vehicle to its assigned bay — the dashboard/`healthz` active
   session count should reflect `PARKED`.
4. Send it to `exit` — this service charges exactly once at `ExitSpot/CarIn`
   and completes the session at `ExitSpot/CarOut`.
5. Break a component in the simulator (or wait for a random fault) — confirm
   a `component_broken` webhook triggers an automatic repair call, deferred
   correctly if the affected spot is occupied.
