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

**Caveat, and the planned replacement.** The ring orders stations by *name*,
so "distance" is how far apart two names sort alphabetically — `S1` is
adjacent to `S10`, and `S9` is twenty steps away. That is not physical
distance. The simulator's level file does contain the real road network: 63
nodes with X/Y coordinates and 62 directed connections, plus coordinates for
every spot and gate.

`python -m scripts.export_graph` extracts it to `data/graph.json`. A
pathfinder (Dijkstra/A* over that graph — the C/C++ module) writes
`data/distances.json`:

```json
{"ENTRY1": {"S1": 340.2, "S3": 512.8}}
```

`app/routing.py` loads that at startup and uses true driving distance when
present, silently falling back to the ring when it is not. Nothing else has to
change, and the pathfinder stays a standalone program with no coupling to this
service.

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
| `carbon_monoxide_event`   | zone CO level/danger recorded                  | fans in the zone switched **on above `CO_FAN_ON_THRESHOLD` and off below it** — the docs note fans consume electricity, so leaving them running is waste (mitigates `Penalty_ZonePollutedWithHighCO`) |
| `gate_action`              | barrier position mirrored                      | — |
| `payment_made`             | validated against the amount this service billed; a mismatch holds the car at the exit instead of releasing it | `POST /car/{plate}/goto/leavepark` **only after the amount validates** (mitigates `Penalty_CarEscapedWithoutPaying`) |
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

**Webhook signature verification:** the organizer's documented recipe — drop
`Signature`, sort the remaining field names alphabetically, join their values
with `|`, hash — **does not reproduce the signatures printed in the same
document.** Tested offline against the doc's own `component_broken` sample
across MD5/SHA1/SHA256, four separators, three key orderings and several
shared-secret guesses: no match. The doc's worked signature example includes a
`RealDateTime` field that the sample payloads omit, so the published samples
are almost certainly incomplete and cannot be verified without live traffic.

`app/signature.py` therefore runs in **calibration mode**: it scores 36
candidate recipes (`algo:separator:key-order`) against every inbound event and
tallies which ones matched, without ever rejecting an event.

```bash
python -m scripts.signature_report
```

Once one recipe sits at 100% over a few hundred events, pin it:

```
WEBHOOK_SIGNATURE_RECIPE=md5:pipe:alpha
WEBHOOK_SIGNATURE_MODE=enforce
```

If nothing matches, the signature covers a field or secret not in the docs —
ask the organisers and **stay on `observe`**. Dropping real events costs far
more than accepting unverified ones.

**Durable event log:** `app/state.py` remains the in-memory hot path, but every
webhook is also written to SQLite (`app/db.py`) *before* any handler runs, so a
crash mid-decision cannot lose an event. `EventId` is the table's primary key,
which makes redelivery a database-level reject rather than something the
application has to remember. This is also what satisfies Level 1's requirement
to "log car arrivals/parking time/departure/charges in database, so dashboard
can show and search these events."

## Setup & Execution Guide

### 1. Environment variables

Copy `.env.example` to `.env` and edit. Defaults are chosen to be safe, not
clever — `AUTOPILOT=false` means nothing is sent to the simulator until you
turn it on.

| Variable | Default | Purpose |
|---|---|---|
| `SIMULATOR_BASE_URL` | `http://127.0.0.1:9898` | Simulator REST server — matches `ListenAddress` in its `settings.json` |
| `SIMULATOR_EMAIL` / `SIMULATOR_PASSWORD` | `admin` / `admin` | Credentials from the simulator's `settings.json` |
| `AUTOPILOT` | `false` | `false` logs every intended command as `[dry-run]` without sending it |
| `WEBHOOK_PORT` | `8080` | Port this service listens on |
| `DATABASE_PATH` | `data/park.db` | SQLite event log |
| `WEBHOOK_SIGNATURE_MODE` | `observe` | `observe` calibrates and never rejects; `enforce` requires a match |
| `WEBHOOK_SIGNATURE_RECIPE` | empty | Pin once `scripts.signature_report` finds a 100% recipe |
| `WEBHOOK_SECRET` | empty | Only if the organisers confirm a shared secret (switches to HMAC) |
| `PARKING_RATE_PER_MINUTE` | `1.0` | Docs: "parking cost = total minutes spent parking" |
| `MINIMUM_CHARGE` | `0.0` | Floor on the computed charge |
| `ELECTRIC_MULTIPLIER` | `2.0` | Docs: "multiplied by 2 if car is electric" |
| `ELECTRIC_SPLIT_CHARGING` | `true` | Bill the 2x as `parkingCost` + `chargingCost` rather than all as parking |
| `ENTRY_GATE` | `gateA` | Gate opened on arrival — confirm against the running simulator |
| `CO_FAN_ON_THRESHOLD` | `50` | Docs: keep fans off below 50 |
| `SIMULATOR_TIMEOUT_S` | `10.0` | HTTP timeout for simulator calls |
| `MAX_PROCESSED_EVENTS` | `5000` | In-memory `EventId` cache size (SQLite is the durable check) |

**Billing is ambiguous in the organizer's docs and must be verified early.**
One page says "Charging cost: 1 per each minute, multiply by 2 if electric";
another says "parking cost = total minutes spent parking, multiplied by 2 if
car is electric". Yet the API takes `parkingCost` and `chargingCost`
separately, and there is a `Penalty_ChargeCarForNoElectricityUsed` for billing
electricity to a car that used none. `compute_charge()` encodes the reading
that an electric car pays `minutes` parking + `minutes` electricity; set
`ELECTRIC_SPLIT_CHARGING=false` to bill the 2x entirely as parking instead.
Confirm against one real electric car before trusting it.

### 2. Install and run locally

Python 3.13+ recommended. Pydantic below 2.12 has no prebuilt wheel for 3.14
and will try to compile Rust — the pinned versions in `requirements.txt` ship
wheels.

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on Unix
pip install -r requirements.txt
cp .env.example .env            # then edit
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### 2a. Develop without the simulator

```bash
python -m scripts.export_graph      # road graph -> data/graph.json
python -m scripts.replay            # drive a full car lifecycle
curl http://127.0.0.1:8080/healthz
```

`replay.py` also exercises duplicate delivery, sequence gaps, penalties and CO
events. Run it with `AMOUNT=0.01` to watch fraud detection hold a car at the
exit.

### 2b. Operator endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | Counters: events, free spots, penalties, fines, gaps, suspect payments |
| `GET` | `/cars` | Live sessions with phase and running charge |
| `GET` | `/sessions?limit=100` | Completed session history |
| `GET` | `/events?limit=50&event_class=penalty` | Raw event log |
| `GET` | `/components` | Spots, barriers, fans, zones |
| `GET` | `/penalties` | Penalty log |
| `GET` | `/signature-report` | Calibration tally |
| `POST` | `/resync` | One-shot re-sync after a crash — **never on a timer** |

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
