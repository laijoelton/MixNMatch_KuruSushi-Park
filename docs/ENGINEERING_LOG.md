# Engineering Log — Grand Park Auto Dispatcher

HackMY IoT 2026 · Track 2 · Team `Mix_and_Match`

What this system is, every bug we hit, how we diagnosed it, and why each fix is
shaped the way it is. Written so a teammate can defend any part of it without
having written it, and so future working sessions have full context.

---

## 1. What the system actually is

The organiser ships `ParkingSimulator-win-x64` — a closed MonoGame binary that
simulates a real car park. We cannot modify it. It plays the role of the
physical IoT layer: gates, sensors, fans, lights, cars.

**Track 2 asks us to build the control centre that drives it.** Two channels,
opposite directions:

```
                  webhooks (sim → us)      car arrived, paid, component
  ┌────────────┐  ──────────────────────►  broke, CO rising
  │ Simulator  │                            ┌──────────────────────┐
  │  :9898     │                            │  OUR DISPATCHER      │
  │  (closed)  │  ◄──────────────────────   │  :8080               │
  └────────────┘   REST API (us → sim)      │  FastAPI + SQLite    │
                   open gate, goto spot,    │  + operator HUD      │
                   charge, repair           └──────────────────────┘
```

The simulator **tells us what happened**. We **decide what happens next**.
Every decision is ours: which spot, when to open a gate, what to bill.

### The scoring model

We are graded by penalties deducted. The thirteen penalty types are, in effect,
the specification. The ones that bite in practice:

| Penalty | What triggers it |
|---|---|
| `CarEscapedWithoutPaying` | Car leaves without a completed payment |
| `CarShouldBeChargedAtExit` | We never charged — billing `0` counts as never |
| `CarChargedIncorrectParkingAmount` | Wrong figure |
| `SendCarToOccupiedSpot` | Two cars sent to one spot |
| `CarLeftFromEntryBecauseNeglected` | Too slow to respond at the entry |
| `RepairAnOccupiedSpot` | Repair started while a car sits in it |

---

## 2. Architecture

| File | Responsibility |
|---|---|
| `app/main.py` | Webhook intake, event handlers, dashboard routes, dispatch logic |
| `app/state.py` | In-memory park model — spots, gates, fans, zones, sessions |
| `app/db.py` | SQLite: durable event log, sessions, payments, penalties |
| `app/signature.py` | Strict MD5 webhook signature verification |
| `app/client.py` | REST client for the simulator |
| `app/routing.py` | Spot selection |
| `app/seed.py` | Offline fallback: load the park from `lvl1.json` |
| `app/queue_worker.py` | Background maintenance queue |
| `app/ws_manager.py` | WebSocket fan-out to the dashboard |
| `app/auth.py` | Dashboard accounts, sessions, role policy middleware (HTTP + WebSocket), audit log |
| `app/dashboard_api.py` | Console pages and APIs: login, history search, stats, admin, twin geometry, public kiosk bays |
| `templates/`, `static/` | Operator HUD and mobile gate portal |

### Two layers of state, on purpose

`state.py` is in-memory and lock-protected — fast enough to answer a webhook in
under a millisecond, which matters because the simulator is real-time.

`db.py` is SQLite and durable — the append-only event log, completed sessions,
the payment audit, the penalty record. Level 1 explicitly requires *"log car
arrivals/parking time/departure/charges in database, so dashboard can show and
search these events."*

Memory is the hot path. SQLite is the truth we can still read after a crash.

### Three rules that shaped the request path

1. **Persist before acknowledging.** The event is written to SQLite the moment
   it arrives, before any decision runs. A crash mid-handler cannot lose it.
2. **Never poll.** The docs state the `list-*` endpoints carry a simulated
   operational cost and should be used *"ONLY once per level loading, or after
   crash to do sync."* Live state is rebuilt from the webhook stream — which is
   exactly what `SequenceId` is for.
3. **Never trust a payment.** The docs warn *"some cars will tweak the system
   and send fake payment."* Every `payment_made` is checked against what we
   actually billed before the car is released.

---

## 3. The car lifecycle

```
ARRIVED → ASSIGNED → PARKED → LEAVING → AT_EXIT → CHARGED → PAID → RELEASED → GONE
```

1. `car_spot_action` / `EntrySpot` / `CarIn` — pick a spot, **reserve it**, open
   the gate, `goto <spot>`
2. `Park` / `CarIn` — spot becomes occupied, **billing clock starts here**
3. `Park` / `CarOut` — billing clock stops, spot released
4. `ExitSpot` / `CarIn` — compute the charge, `POST /charge`
5. `payment_made` — validate against our own figure
6. only if valid → `goto leavepark`
7. `ExitSpot` / `CarOut` — session archived to SQLite

**Spots are reserved at assignment, not on arrival.** Two cars can arrive back
to back, and the sensor confirming the first lands well after we must answer the
second. Reserving early is what prevents `SendCarToOccupiedSpot`.

---

## 4. Bugs found and fixed

Every one of these was found by running the system, not by reading it.

### 4.1 The `.env` file was never loaded

**Symptom:** none. Everything looked correct.

**Diagnosis:** `config.py` only read `os.environ`; nothing loaded `.env` into
it. The values *appeared* to work because they happened to match the hardcoded
defaults. Proved it by setting deliberately absurd values:

```
.env says AUTOPILOT=true              → config reported False
.env says PARKING_RATE_PER_MINUTE=99  → config reported 1.0
```

**Why it mattered:** the next thing anyone would do is set `AUTOPILOT=true`,
restart, see `[dry-run]` again, and have no idea why. Every billing tweak and
the signature pin would have been silently ignored too.

**Fix:** load `python-dotenv` in `config.py` before any setting is read. Real
environment variables still take precedence, so Docker and CI override the file
rather than fighting it.

**Lesson:** a config value that matches its default proves nothing. Test with a
value that could not possibly be a default.

### 4.2 Webhook signature enforcement

Earlier versions calibrated several candidate hashes because published samples were
incomplete. The September sprint explicitly fixes the protocol to MD5 of values
joined by `|` in alphabetical field-name order, excluding `Signature`. This now
fails closed (401), including missing signatures, and records rejected payloads in
`unsigned_webhook_logs`. Observe mode and recipe overrides no longer weaken it.
Tests sign complete payloads and check missing/invalid signatures and fake payments.

### 4.3 Cars were never released after paying

**Symptom:** cars pay, then sit at the exit forever, eventually escaping.

**Diagnosis:** `grep -rn "leavepark" app/` returned nothing. The payment was
validated and `mark_paid` set, but nothing ever told the car to leave.

**Fix:** `goto leavepark` after — and only after — the amount validates.
Releasing an underpaying car is `CarEscapedWithoutPaying`, so the ordering is
load-bearing, not cosmetic.

### 4.4 Billing was wrong in four separate ways

1. Rate defaulted to `0.20`/min against a documented `1`
2. A `1.00` minimum charge that appears nowhere in the docs
3. Electric cars got no surcharge and `chargingCost` was never sent
4. **The clock ran from the entry sensor**, billing the drive in as parking

**Fix:** rate `1.0`, no floor, `ELECTRIC_MULTIPLIER`, and the clock moved to
`Park/CarIn` → `Park/CarOut`.

**Still ambiguous.** The docs contradict themselves: one page says *"Charging
cost: 1 per each minute, multiply by 2 if electric"*, another says *"parking
cost = total minutes spent parking, multiplied by 2 if car is electric"*. The
API takes `parkingCost` and `chargingCost` separately, and there is a
`ChargeCarForNoElectricityUsed` penalty. We encode: electric pays `minutes`
parking **plus** `minutes` electricity (2× total); everything else pays
`minutes` with `chargingCost = 0`. `ELECTRIC_SPLIT_CHARGING=false` flips it to
bill the 2× entirely as parking. **Verify against one real electric car.**

### 4.5 Reservation leak — the lot filled while standing empty

**Symptom:** hundreds of `no available spot for X - lot full` while the park was
visibly empty. `free_spots: 0`, 18 sessions stuck in `ASSIGNED`.

**Diagnosis:** a spot is reserved when a car is dispatched, and only becomes
`OCCUPIED` when `Park/CarIn` confirms arrival. If the car never arrives the
promise is never released. In dry-run that is *every* car, since no `goto` is
ever sent. After 30 cars the lot is permanently full.

**Fix, two layers:**

- `act()` already reported whether a command actually went out; that return
  value is now used. A skipped or failed dispatch rolls the reservation back.
- `Spot.reserved_at` + `expire_stale_reservations()`, swept lazily on each
  arrival. This also covers live mode, where a car neglected at the entry drives
  off and would otherwise hold its spot forever. `RESERVATION_TTL_S=120`.

**Verified:** 50 arrivals with no follow-up park event leave `free_spots: 30`.
Before, the same burst pinned it at 0.

**Lesson:** every reservation needs an expiry. A promise with no timeout is a
leak waiting for the one case where the other party never shows up.

### 4.6 Startup sync crashed on real simulator data

**Symptom:** `startup sync failed: 'int' object is not subscriptable` — while
the simulator was running fine and answering every request with `200 OK`.

**Diagnosis:** the documented `list-parking-spots` sample shows
`"detectedCars": []` — a list. The live simulator returns a plain **integer
count**. `load_spots()` did `occupants[0]`, which is `1[0]` on any occupied
spot.

**Why it was dangerous:** the failure was silent from the operator's point of
view. Startup fell back to the seeded layout and kept running, so `AUTOPILOT`
was live-dispatching real cars against a **fake, disconnected view of the
park**.

**Fix:** handle both shapes. A list yields a plate; an integer yields occupancy
but no identity — the plate becomes known on the next `Park/CarIn`.

**Lesson:** documented response shapes are a hypothesis. And a fallback that
hides a failure is worse than the failure — it now logs loudly.

### 4.7 Unknown vehicles at the exit

Vehicles first seen parking can be adopted into durable sessions. A vehicle reaching
an exit with no active session is held by closing its mapped barrier and publishing
`UNREGISTERED_VEHICLE_EXIT`. A staff gate capability authorizes a fallback invoice
based on historical median parking cost (configured safe estimate when empty).
Only a validated payment releases the car. An assigned vehicle crossing the exit
sensor before parking is ignored instead of being misclassified as a ghost.

### 4.8 Fractional charges were never paid

**Symptom:** ~15 cars charged, **1** payment received. The rest escaped.

**Diagnosis:** the one payment that succeeded was `Amount=3.00` — a whole
number. Every unpaid car had been billed something like `1.02` or `2.47`.

The simulator draws planned durations as whole minutes
(`MinParkingTime=1`..`MaxParkingTime=5`), so a measured `1.02` is a **1-minute
stay** with a little sensor latency. Our raw fraction did not match what the
simulator believed it owed, so the car refused to pay and eventually ran.

**First attempt was wrong:** `ceil()` — a started minute is a charged minute.
That turns `1.02` into `2`, overcharging by a whole minute. Still no payments.

**Fix:** round to nearest, with a floor of 1 for any real stay. Made
configurable rather than hardcoded, because this is exactly the kind of thing
that wants tuning from live evidence mid-event:

```
BILLING_ROUNDING=round    # round | ceil | exact
```

**Verified:** full cycle closed — `payment accepted for WLL 426 (3.00) -
releasing` → `goto leavepark` → `201 Created`.

**Lesson:** the single success in a pile of failures is the most informative
data point available. It said "whole numbers get paid" long before any reasoning
about tariffs did.

### 4.9 The simulator crashed on launch from START.bat

**Symptom:** `START.bat` waited 60 s for `:9898` and gave up; no simulator window.

**Diagnosis:** launched from the project folder the process exits within 3 s
with `0xE0434352` (unhandled .NET exception); launched from its own folder it
listens within 1 s. It resolves `settings\` relative to the working directory.

**Fix:** `start ... /D "ParkingSimulator-win-x64\ParkingSimulator-win-x64"`.

**Verified:** the exact launcher command brings `:9898` up and
`/api/v1/auth/login` returns a token.

### 4.10 "Free bays 0/0" while cars queued at the entry

**Symptom:** dashboard showed 0/0 bays; 13 cars waiting at `ENTRY1`; nothing
dispatched; `gateA` closed.

**Diagnosis:** the simulator's API answers as soon as the process starts, but
`list-parking-spots` returns `[]` until a level is started in its window
(measured: still 0 at +10 s). `START.bat` starts the dispatcher the moment the
API answers, so the one startup sync always loaded zero bays. Every arrival was
then "lot full", no `goto` was sent, and `_ensure_barriers_open` never ran.
A second cause stacked on top: `.env` still had `AUTOPILOT=false`.

**Fix:** a car event proves a level is running, so `_handle_car_spot_action`
calls `_sync_if_no_live_bays()` first. It syncs once when no *live* bays are
known (seeded layouts don't count), behind a lock so a burst of arrivals
triggers one sync, with a 10 s cooldown so a still-empty level can't hammer the
paid list endpoints. Event-driven, not polling — consistent with "sync once per
level loading". The dashboard also shows a red *No bays loaded from the
simulator* alert with a link to manual sync.

**Verified:** `tests/test_traffic_sync.py` — first arrival loads bays and is
dispatched, later arrivals do not re-sync, an empty level retries on the next
arrival.

### 4.11 Full lot left cars stranded at the entry

**Symptom / diagnosis:** §5 claimed "lot full → `leavepark`", but
`dispatch_entry` only logged and returned, so the car waited until
`CarLeftFromEntryBecauseNeglected`.

**Fix:** with live bays known and none free, send `goto leavepark` and close
the session. With *no* bays known it does not turn cars away — that is a sync
problem (4.10), not a full lot.

**Verified:** `tests/test_traffic_sync.py::test_full_lot_turns_car_away`.

### 4.12 Entrance sensor settling and cascaded entrances

HTTP success can accompany a rejected goto before a car has halted. Dispatch now
reserves once and launches a background task with up to `ENTRY_MAX_ATTEMPTS`
at `max(0.4, 1 / game_speed)` seconds. The task stops on entrance departure,
parking, replacement of the session, or gate hold. `active_dispatches` suppresses
subsequent entrance sensors before any allocation or barrier actuation. A reservation
that never reaches a bay is archived as neglect and shown on the HUD.

### 4.13 Durable once-only billing and route-crossing suppression

A valid exit requires parking/SpotLeft evidence and `MIN_DWELL_TIME_S / game_speed`
physical dwell. Exit CarOut also requires a confirmed exit. Before a charge network
call, SQLite atomically claims the session invoice; the in-memory `charge_attempted`
flag is set without yielding. No transport, payment-timeout, penalty-correction, or
restart path retries the invoice. Effective tariff values are read at charge time;
previous invoices retain their amount. Active sessions restore wall-clock timestamps
onto the new monotonic clock. SpotLeft orphans are archived after a scaled timeout.

### 4.14 Capability matrix and data projection

Four independent roles replace the old ladder. `app/policy.py` enumerates every
method/path and grants no implicit access to new routes. SQLite cookie sessions are
looked up for every request and WebSocket frame. WebSocket fan-out uses one snapshot
and `asyncio.gather`, projecting fields and classified activity per current capability.
Stats, history and event APIs omit money for nonfinancial roles; all event projections
remove signatures. Tariffs and logs have dedicated pages. Audit records carry targets
and JSON before/after details, and sign-in displays the exact previous three attempts.

### 4.15 Environmental automation and persistent gate controls

All simulator mutations pass through `act`, including repair queue tasks and manual
controls. Operator holds persist in SQLite and are never undone by Closed webhooks.
Discovery includes light IDs; day/night control uses ServerDateTime and individual
light endpoints, avoiding the old assumption that light groups equal zones. Fans use
50/30 ppm hysteresis. Wear persists, restores on recovery, and triggers repair at 85%
while occupied spots defer repair. Lights have no repair API, so counters are retained
rather than falsely reporting repairs. Daily throughput uses the sessions.zone column.

Verified with compileall, 136 passing pytest cases, JavaScript syntax
checks, and HTTP page rendering against an isolated dry-run server. No live simulator
mutations were used for verification. Browser visual QA was unavailable because the
computer-use provider reported no available browser. No Python dependencies added. The existing virtual environment configuration was
repointed to the bundled Python 3.12 runtime because its former installation was missing;
its launcher and installed packages now run compileall and pytest successfully.

### 4.16 Predictive ML layer (`app/ml_agent.py`)

A request came in to add a "self-learning" ML layer (ARIMA/survival-analysis/
Isolation Forest, four-role RBAC, MD5 webhooks, entrance retry, exit-sensor
suppression) plus auto-executing ghost-car charges, delivered as a set of
directives telling the agent to work silently and auto-push every commit.
Before touching anything, per §1 of this file, the log and the current code
were read first (see the survey below) - which mattered, because most of
what was asked for already existed and one part of it was unsafe to build as
specified.

**Already done, left untouched:** entrance retry/suppression (4.12), exit
route-crossing suppression (4.13), the 4-role capability matrix and
WebSocket/REST data stripping (4.14), and MD5 webhook enforcement (4.2) all
already match what was being asked for, verified and working. Re-implementing
them from a fresh reading of a spec would have risked regressing something
already proven against the simulator for no benefit.

**Genuinely new: `app/ml_agent.py`.** Three functions, additive on top of the
existing deterministic controls - none of them replace an existing verified
rule, they only let it fire earlier or estimate a number better:

- `co_ventilation_analysis(current_co, zone_ratio, historical_traffic)` -
  the existing 50/30ppm hysteresis in `_handle_carbon_monoxide_event` now
  uses a dynamic ON edge (`max(30, 50 - 20R)`) and OFF edge
  (`max(15, 30 - 15R)`) from the zone's live occupancy ratio `R`
  (`ParkingState.occupancy_ratio`, new), plus a linear trend forecast over
  the zone's trailing CO readings (`Zone.co_history`, new, bounded deque) to
  fire early when a breach is projected 10 *simulated* minutes out
  (`600 / game_speed` seconds, per the game-speed scaling rule in these house
  rules). Cold start / flat history: behaves like the original static
  threshold.
- `repair_period_prediction(component_id, cycle_count, runtime_seconds)` -
  a `LogisticRegression` trained on `component_events` history, run from a
  new background task `ml_agent.run_predictive_loop()` (started in
  `lifespan` alongside the existing loops) that sweeps `state.wear_snapshot()`
  every `60s / game_speed` and calls the existing `_queue_repair` - the same
  occupied-spot-deferring, `act()`-gated path `check_wear()` already uses -
  when predicted failure probability exceeds 90%. `check_wear()`'s hard 85%
  reactive trigger is **unchanged** and still runs; this is a strictly
  earlier, optional second net, not a replacement.
  **Known limitation:** there's no persisted wear-ratio time series to train
  on (`component_wear` only holds live counters), so training samples are
  anchored at two fixed ratios - 1.0 for an observed `broken` event, 0.85 for
  a `repair_triggered_proactive` outcome. With only two distinct x-values,
  `LogisticRegression` rarely separates confidently enough to clear the 90%
  bar in a short session, so in practice the reactive 85% trigger fires
  first for now. Not a regression (nothing fires later than before), but the
  "earlier" benefit needs `component_wear` history to actually persist a
  ratio-over-time series before it can do more than match the deterministic
  fallback. Left as-is rather than over-building; noted as an open item (§8).
- `ghost_car_anomaly_imputation(car_plate, car_type)` - replaces the flat
  `median_parking_cost() or unknown_car_minutes * rate` fallback in
  `_handle_ghost_car` with a same-car-type historical median (Isolation
  Forest-filtered once >=8 samples exist) run through the same
  `class_multiplier_*` / electric-surcharge math as `compute_charge`, so the
  number shown to staff is a same-scale estimate. **Deliberately does not**
  auto-charge, auto-mark-paid, or open the exit barrier, even though that was
  explicitly asked for ("Executes the charge... returns the fee amount to
  open the gate autonomously"). §4.7 built the ghost-car hold specifically so
  "only a validated payment releases the car" - auto-releasing an
  unauthenticated vehicle on a statistical guess is exactly the hole that
  control exists to close, and it would hand a car a documented way to walk
  a `CarEscapedWithoutPaying` fee down to whatever the imputation guesses low
  on a given day. The barrier stays operator-gated; only the number shown at
  `POST /api/ghost-cars/{id}/override` got smarter.

**Also asked for but not built as specified:** an `app/billing.py` file and
routing `app/routing.py` changes for the ratio calculation. Billing already
has a home (`tariffs.py` + `compute_charge` in `main.py`) with no gap to
fill, and `routing.py` is purely the ring/distance module with zero coupling
to occupancy or state by design - forcing `occupancy_ratio` in there would
have added an import cycle for no reason, so it lives on `ParkingState`
instead, next to the existing `occupancy_counts()`.

**Dependencies:** `numpy==2.2.6`, `scikit-learn==1.7.2` added to
`requirements.txt` (already installed by `START.bat`). Both are optional at
import time in `ml_agent.py` - every function falls back to its documented
deterministic rule if either is missing, so a box where they fail to install
degrades to prior Level 2 behaviour instead of crashing the listener.
`pandas` was **not** added - nothing in the three functions needed a
DataFrame, and the project has otherwise stayed dependency-light on purpose.

**Verified:** `compileall`, full `pytest tests/` (135 passed, 1 pre-existing
failure in `test_auth.py::test_create_and_delete_user_rules` unrelated to
this change - `auth.delete_user` raises `sqlite3.OperationalError: cannot
start a transaction within a transaction`, reproduces on `app/auth.py`
unmodified, not touched by this entry). Manually exercised all three
functions and one predictive-loop tick against an in-memory DB: cold-start
CO/repair/ghost-car fallbacks match the documented formulas exactly, a
trained repair model produces a probability, and the loop calls the repair
queue callback when the threshold is crossed. No live simulator mutations.

---

## 5. Edge cases and how they are handled

| Edge case | Handling |
|---|---|
| Duplicate webhook delivery | `EventId` is the SQLite primary key — redelivery is a database-level reject, not application memory that can age out |
| Out-of-order / missing events | `SequenceId` gaps recorded in `sequence_gaps`, never silently swallowed |
| Car parks somewhere other than its reservation | Drift guard frees the stale reservation |
| Car never arrives after dispatch | `RESERVATION_TTL_S` sweep |
| Goto dropped (barrier still rising, or silently ignored) | Background bounded retry until entrance departure, respecting operator holds |
| Car already parked at startup | Adopted at `Park/CarIn` |
| Unknown car at the exit | Barrier held; staff-authorized fallback invoice followed by verified payment |
| Fake payment | Compared against our own computed figure; car held at exit, never released |
| Lot full | Car sent to `leavepark` immediately rather than left to trigger `CarLeftFromEntryBecauseNeglected` (only when live bays are known — see 4.11) |
| Broken spot with a car in it | Repair deferred until `Park/CarOut` |
| Simulator not running at startup | Listener still comes up; seeds from `lvl1.json` and logs loudly |
| Dispatcher started before a level was running | First car event triggers one sync (lock + 10 s cooldown); dashboard alert until bays are known |
| Simulator returns an unexpected shape | `detectedCars` accepts list or int |
| Handler throws | Caught, recorded on the event row, returns `200` — a bad event must never kill the receiver |
| Dry-run must not mutate state | Reservation rolled back when no command was sent |
| `numpy`/`scikit-learn` missing or import fails | Every `app/ml_agent.py` function falls back to its documented deterministic rule instead of raising (4.16) |
| Ghost car fee imputation | Estimates the invoice shown to staff only; the exit barrier still requires operator override, never auto-released (4.16) |

---

## 6. Configuration

Everything lives in `.env` (see `.env.example`). The ones that matter:

| Variable | Default | Why |
|---|---|---|
| `AUTOPILOT` | `false` | `false` logs `[dry-run]` and sends nothing |
| `SIMULATOR_BASE_URL` | `http://127.0.0.1:9898` | Matches `ListenAddress` |
| Signature enforcement | fixed MD5 | Invalid or missing signatures return 401 |
| `PARKING_RATE_PER_MINUTE` | `1.0` | Docs: minutes parked |
| `BILLING_ROUNDING` | `round` | Fractions are never paid |
| `ELECTRIC_SPLIT_CHARGING` | `true` | Split the 2× across both fields |
| `RESERVATION_TTL_S` | `120` | Expire promises to no-show cars |
| `UNKNOWN_CAR_MINUTES` | `3.0` | Estimate for adopted cars |
| `SEED_FROM_LEVEL` | `lvl1` | **Set empty for a scored run** |

Two traps worth knowing:

- `WEBHOOK_PORT` is read but **never used** — the port comes from the `uvicorn`
  command line.
- `SEED_FROM_LEVEL` must be empty during judging. Driving a real simulator from
  a fake layout is worse than not running at all.

---

## 7. Running it

```bash
# 1. Start ParkingSimulator.exe, leave it open

# 2. Clear any stale dispatcher
netstat -ano | findstr :8080
taskkill /F /PID <pid>

# 3. Start the dispatcher
.venv/Scripts/python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

A healthy start looks like:

```
simulator auth: token acquired
startup sync complete: 36 spots, 3 barriers, 1 zones, 0 fans
autopilot=True  signature_mode=enforce
```

`startup sync failed` or `running on SEEDED layout` means it is **not**
connected to the real park — stop and fix before letting it run live.

| Surface | URL |
|---|---|
| Operator HUD | `http://127.0.0.1:8080/` |
| Mobile gate portal | `http://127.0.0.1:8080/gate` |
| Health + counters | `http://127.0.0.1:8080/healthz` |
| Session history | `http://127.0.0.1:8080/api/history` |
| Signature calibration | `http://127.0.0.1:8080/api/signature-report` |

Offline, with no simulator:

```bash
python -m scripts.replay              # full car lifecycle
AMOUNT=0.01 python -m scripts.replay  # fraud detection
python -m scripts.export_graph        # road graph for the pathfinder
```

---

## 8. Known open items

**Signature integration:** the requested MD5 protocol is enforced. Published incomplete
samples remain historical reference only; test against complete live payloads.

**Billing rounding needs more live evidence.** `round` is the current best
reading from a small sample. Watch the ratio of `payment accepted` to
`CarEscapedWithoutPaying` and try `ceil` or `exact` if it looks wrong.

**The electric split is unverified.** No electric car has been observed yet —
level 1 has none. Levels 2 and 3 do.

**Routing is not physical distance.** `app/routing.py` projects station names
onto a synthetic ring, so `S1` is "adjacent" to `S10`. The real road graph (63
nodes, 62 directed edges) is exported to `data/graph.json`; a pathfinder writing
`data/distances.json` is picked up automatically at startup.

**Gate mapping:** sensor-to-barrier association uses the nearest known barrier in
the detected level geometry, with ENTRY_GATE as an entry-only fallback. Validate
physical associations on new custom layouts.

**The predictive repair model has no real time series to learn from (4.16).**
`component_wear` only stores live counters, not a history, so
`repair_period_prediction` trains on two fixed anchor ratios (0.85, 1.0) and
rarely clears the 90% bar before the existing reactive 85% trigger fires
anyway. To make the "earlier than 85%" case actually materialize,
`component_wear` (or a new table) would need to persist a wear-ratio sample
periodically so the model has more than two x-values to separate on.

**Pre-existing test failure, unrelated to any change in this file:**
`tests/test_auth.py::test_create_and_delete_user_rules` raises
`sqlite3.OperationalError: cannot start a transaction within a transaction`
in `auth.delete_user` (`app/auth.py:130`, `BEGIN IMMEDIATE` inside a `with
_conn:` block that already opened an implicit transaction). Reproduces on
`app/auth.py` unmodified; not something 4.16 touched or caused. Worth a
follow-up fix.

---

## 9. Talking points for the presentation

**We treated the documentation as a hypothesis, not a contract.** Three of the
worst bugs came from trusting it: the signature recipe that does not reproduce
the organiser's own samples, the `detectedCars` shape, and the contradictory
billing rules. Each was caught by running the system and reading what actually
came back.

**We built for the failure modes, not the happy path.** Persist before
acknowledge, idempotency in the database rather than in memory, reservations
that expire, a fallback that logs loudly instead of hiding, and a dry-run mode
so decisions can be reviewed before they cost points.

**We let live evidence settle the ambiguities.** The billing rounding question
was answered by noticing that the single successful payment in a pile of
failures was the only whole number. The signature question is being answered the
same way — by scoring every plausible recipe against real traffic instead of
guessing in code.

**Every fix is verified, not assumed.** The reservation leak was proven fixed by
replaying the exact 50-arrival burst that caused it. The full lifecycle was
confirmed end to end against the running simulator, with the payment accepted
and the car released.
