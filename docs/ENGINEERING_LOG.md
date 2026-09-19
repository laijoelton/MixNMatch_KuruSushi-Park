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
| `app/signature.py` | Webhook signature verification with runtime calibration |
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

### 4.2 Webhook signature verification rejected every event

**Symptom:** would have been total — `HTTPException(401)` on every webhook.

**Diagnosis:** two compounding problems.

`WEBHOOK_HASH_ALGO` defaulted to `sha256`, producing 64 hex characters. The
simulator sends 32. `hmac.compare_digest` could never match.

Worse, the documented recipe is wrong. The docs say: drop `Signature`, sort
remaining field names alphabetically, join values with `|`, hash. We tested that
against the organiser's own `component_broken` sample across MD5/SHA1/SHA256 ×
4 separators × 3 key orderings × 7 shared-secret guesses. **Zero matches.**
Their worked example includes a `RealDateTime` field the sample payloads omit,
so the published samples are incomplete and cannot be verified offline.

**Fix:** `app/signature.py` runs in *calibration mode*. It scores 36 candidate
recipes (`algo:separator:key-order`) against every live event and tallies which
match, while `WEBHOOK_SIGNATURE_MODE=observe` never rejects anything.

```bash
python -m scripts.signature_report    # after live traffic
```

If a recipe reaches 100%, pin it and switch to `enforce`. If none does, the
signature covers a field or secret not in the docs — ask the organisers.

**Design principle:** when the spec is wrong and the cost of a false negative
(dropping real events) vastly exceeds a false positive (accepting unverified
ones), fail open and instrument. Do not guess in code.

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

### 4.7 Cars already in the lot escaped without paying

**Symptom:** `Car escaped without paying after parking for some time` ×19.

**Diagnosis:** cars parked before the dispatcher started — or that survived a
restart — have no session. At the exit, `get_session()` returned `None`, we
returned early, never charged, and the car escaped free.

**Fix:** adopt them. At `Park/CarIn` with no session, create one so the billing
clock runs. At the exit with no session, create one and charge an estimate
(`UNKNOWN_CAR_MINUTES=3.0`, the midpoint of the simulator's `MinParkingTime=1`
and `MaxParkingTime=5`).

**The reasoning:** we cannot know the true duration, so we will probably take
`CarChargedIncorrectParkingAmount`. But billing `0` reads to the simulator as
*not charging at all*, which earns `CarShouldBeChargedAtExit` **and**
`CarEscapedWithoutPaying`. One wrong number is cheaper than two penalties.

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

### 4.12 The first car after a sync never left the entry

**Symptom:** after the auto-sync (4.10) every car was dispatched and parked
except the first one, `NQP 718`, which stayed `ASSIGNED` to S9 at `ENTRY1`.

**Diagnosis (from the event log):** `ENTRY1 CarIn` at 59.600 triggered the
sync, which found `gateA` Closed and sent `open`, then the goto was sent
immediately. `gateA` only reported `Open` at 59.918. Every later car shows
`ENTRY1 CarOut` ~0.9 s after its `CarIn`; `NQP 718` never did. The goto sent
through a still-rising barrier was dropped silently (201, no movement) - the
same failure mode as early exit charges (4.x exit timing). Two things made it
possible: `_ensure_barriers_open` recorded the barrier as `Open` the instant
the command was sent, and nothing watched for a car failing to leave the entry.

**Fix:** a barrier we just commanded is recorded as `Opening`; only its own
`gate_action: Open` webhook marks it `Open`. `dispatch_entry` waits for that
(`GATE_OPEN_WAIT_S`, 5 s scaled by game speed, then dispatches anyway). Every
dispatched car is watched: `EntrySpot/CarOut` marks it as having left; if it
has not after `ENTRY_RETRY_S` (8 s scaled), the goto is sent once more.

**Verified:** `tests/test_entry_dispatch.py` - waits for the Open webhook,
gives up instead of hanging, never waits on open/unknown/broken barriers,
resends only for a car that did not leave.

---

## 5. Edge cases and how they are handled

| Edge case | Handling |
|---|---|
| Duplicate webhook delivery | `EventId` is the SQLite primary key — redelivery is a database-level reject, not application memory that can age out |
| Out-of-order / missing events | `SequenceId` gaps recorded in `sequence_gaps`, never silently swallowed |
| Car parks somewhere other than its reservation | Drift guard frees the stale reservation |
| Car never arrives after dispatch | `RESERVATION_TTL_S` sweep |
| Goto dropped (barrier still rising, or silently ignored) | Wait for the barrier's own `Open` webhook; resend once if no `EntrySpot/CarOut` within `ENTRY_RETRY_S` |
| Car already parked at startup | Adopted at `Park/CarIn` |
| Unknown car at the exit | Adopted and charged an estimate |
| Fake payment | Compared against our own computed figure; car held at exit, never released |
| Lot full | Car sent to `leavepark` immediately rather than left to trigger `CarLeftFromEntryBecauseNeglected` (only when live bays are known — see 4.11) |
| Broken spot with a car in it | Repair deferred until `Park/CarOut` |
| Simulator not running at startup | Listener still comes up; seeds from `lvl1.json` and logs loudly |
| Dispatcher started before a level was running | First car event triggers one sync (lock + 10 s cooldown); dashboard alert until bays are known |
| Simulator returns an unexpected shape | `detectedCars` accepts list or int |
| Handler throws | Caught, recorded on the event row, returns `200` — a bad event must never kill the receiver |
| Dry-run must not mutate state | Reservation rolled back when no command was sent |

---

## 6. Configuration

Everything lives in `.env` (see `.env.example`). The ones that matter:

| Variable | Default | Why |
|---|---|---|
| `AUTOPILOT` | `false` | `false` logs `[dry-run]` and sends nothing |
| `SIMULATOR_BASE_URL` | `http://127.0.0.1:9898` | Matches `ListenAddress` |
| `WEBHOOK_SIGNATURE_MODE` | `observe` | Calibrate; never drop events |
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
autopilot=True  signature_mode=observe
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

**The signature recipe is still unconfirmed.** Handled, not solved. Run
`scripts/signature_report.py` after live traffic. Stay on `observe` until a
recipe hits 100%.

**Billing rounding needs more live evidence.** `round` is the current best
reading from a small sample. Watch the ratio of `payment accepted` to
`CarEscapedWithoutPaying` and try `ceil` or `exact` if it looks wrong.

**The electric split is unverified.** No electric car has been observed yet —
level 1 has none. Levels 2 and 3 do.

**Routing is not physical distance.** `app/routing.py` projects station names
onto a synthetic ring, so `S1` is "adjacent" to `S10`. The real road graph (63
nodes, 62 directed edges) is exported to `data/graph.json`; a pathfinder writing
`data/distances.json` is picked up automatically at startup.

**One entry gate is assumed.** `ENTRY_GATE=gateA` is a level-1 simplification.
Level 2 has 3 entry spots, level 3 has 8.

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
