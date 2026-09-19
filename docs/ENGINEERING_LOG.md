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

### 4.9 The charge was right, then drifted wrong — we were billing the wrong clock

**Symptom:** cars paid correctly for a while, then a stream of
`Penalty_CarChargedWrongAmount`, each naming an amount we had not sent.

**Diagnosis:** the penalty text carries the answer — `should be: (3)`. Parsing
65 of them gave a clean split: planned 3 → wants `3.00` (26 cases), planned 4 →
wants `4.00` (24 cases). Not one correction matched our measured duration.

The simulator bills **`PlannedParkingDurationInMinutes`** — the duration the
driver booked on arrival — not the time the car was actually in the bay. §4.8's
rounding fix had been papering over this: at `GameSpeedMultiplier=1` a measured
stay lands near the booked one, so rounding hid the mismatch. Raise the game
speed and the two diverge, which is why it looked like a drift over time rather
than a bug that was always there.

**Fix:** bill the booked duration, keeping the measured clock as a fallback for
cars whose plan we never saw (adopted cars, restarts):

```
BILLING_BASIS=planned     # planned | measured
```

`_planned_of(payload)` reads it off the arrival event; `compute_charge()`
prefers it. `_apply_charge_correction()` parses `should be: (X)` out of a
penalty and re-bills, so a wrong charge costs one fine instead of an escape.

**Verified:** a full run finished with **0 penalties and 0 fines**.

**Lesson:** the simulator was telling us the right answer in every rejection
message. We were reading the penalties as a score, not as data.

### 4.10 Two half-built dashboards, and a launcher pointing at the wrong one

**Symptom:** "I'm still not on the new dashboard, it's still the old one."

**Two independent causes**, which is why changing one did not help:

1. `START.bat` opened `http://127.0.0.1:8080/` — the *older* operator HUD. The
   split dashboard lives at `/dashboard`.
2. The split dashboard was only half-wired. `split_dashboard.css` styled
   `.phone-pane/.phone-frame/.phone-screen`, and `static/js/user_waze_gps.js`
   defined `window.DriverPortal` — but `templates/dashboard.html` never loaded
   that script, had none of the IDs it needed, and used `operator-pane-full`,
   so the left pane took the whole width and the phone pane did not exist.

**Fix:** launcher opens `/dashboard` (`/` is still served and still linked);
template loads the script, carries the required IDs, and uses `operator-pane`.

**Lesson:** "it looks the same" can be two bugs wearing one coat. The URL was
the obvious suspect and fixing it alone would have changed nothing visible.

### 4.11 Merging the auth branch: four bugs the merge itself surfaced

Merging `feature/admin-operator-auth` (staff login + roles) was clean — two
trivial conflicts — but reviewing and running it turned up four real problems.

**a. The live WebSocket was wide open.** Every REST endpoint had been placed
behind a login, but `/ws/live` and `/ws/telemetry` had not. They push the whole
snapshot — plates, sessions, penalties — so anyone who could reach the port got
everything the REST gates refused, by opening a socket. The auth was a front
door on a building with no back wall.
**Fix:** both sockets require a staff session (browsers send cookies on the
handshake) and close `1008` without one. The page treats `1008` as "session
expired" and reloads rather than reconnecting in a loop.

**b. A duplicate dict key silently dropped the CO data.** The branch added
`"zones": self.zone_occupancy()` to a snapshot literal that already had a
`"zones"` key holding the CO/danger-level `Zone` objects. Python keeps the
last one, so the air-quality data vanished from the snapshot with no error.
Nothing consumed it yet, which is exactly why it would have been found late.
**Fix:** the occupancy list is `zone_occupancy`; `zones` still carries CO.

**c. Operator controls failed as a bare `500`.** The manual endpoints called
the simulator directly, so an unreachable simulator surfaced as
`Internal Server Error` with no reason — indistinguishable from "my click did
nothing". Barrier names were not validated either, so a typo was posted to the
simulator rather than refused.
**Fix:** `_operator_command()` wraps them, logs the failure to the activity
feed and returns `502` with the reason; unknown names are a `404`. It
deliberately ignores `AUTOPILOT` — a manual override is explicit human intent,
and dry-run must not silently swallow it.

**d. `scripts/replay.py` polled an endpoint that never existed.** It read
`/cars` to find the invoice; there is no such route and never was, so the check
always failed and silently fell back to `1.0` — making the fraud-detection test
weaker than it looked.
**Fix:** it logs in as staff and reads `/api/state`, which now exposes
`expected_amount` on live sessions (the dashboard needs that too — history only
has the figure after the session is archived).

### 4.12 Roles: four of them, and why they are permissions not role checks

The brief asks for Admin and Operator; we were then asked for Accountant and
Engineer as well, "and be ready to expand" for level 2.

The branch's original shape was `if info.role != ROLE_ADMIN`. That works for
two roles and quietly breaks for four: every such comparison has to be found
and revisited each time a role is added, and the failure mode is a new role
silently inheriting access nobody granted it.

**What we did instead:** endpoints ask for a *permission*, never a role.

| Permission | Admin | Operator | Accountant | Engineer |
|---|---|---|---|---|
| `lot.view` | yes | yes | — | yes |
| `lot.control` | yes | yes | — | — |
| `maintenance` | yes | — | — | yes |
| `finance.view` | yes | — | yes | — |
| `diagnostics.view` | yes | — | — | yes |
| `history.view` | yes | yes | yes | yes |
| `staff.manage` | yes | — | — | — |

Admin is defined as `ALL_PERMISSIONS`, not a hand-listed set — so a permission
added for level 2 is granted to Admin automatically instead of going stale.

Adding a role is one row in `ROLE_PERMISSIONS` plus credentials in `.env`. No
endpoint changes.

**Three details worth keeping:**

- **The snapshot is filtered too.** Denying an Accountant `/api/spots` means
  nothing if `/api/state` and the WebSocket hand them the same spots.
  `filter_snapshot()` drops keys the role's permissions do not cover, and the
  broadcaster groups sockets by permission set so the filter runs once per
  distinct role on screen, not once per connection.
- **One console, panes gated in the template.** All four roles open `/admin`
  and see only their panes. Four separate templates would drift apart.
- **Login lands where the role can read.** An Accountant sent to the lot canvas
  would bounce straight off a 403, so each role has a `home`.

`authenticate()` compares every account even after a match, and encodes both
sides before `compare_digest` — its `str` form raises `TypeError` on non-ASCII
input, which would turn a junk username into a 500 instead of a failed login.

### 4.13 Removing the driver-facing bay picker

Two surfaces let a driver pick their own bay: `/gate` (a mobile grid) and a
phone-frame pane on `/dashboard` backed by `static/js/user_waze_gps.js`.

**Both are now removed**, at the team's call, and the reasoning is worth
keeping: a driver choosing their own spot *competes with the router that is
being scored*. `dispatch_entry()` ranks bays by real driving distance;
`assign_specific_spot()` threw that away and honoured whatever the human
tapped. Two dispatch paths into the same reservation lock is also twice the
surface for a race to hide in, for a feature the brief never asks for.

**Removed:** `templates/gate.html`, `static/js/lot_picker.js`,
`static/js/user_waze_gps.js`, the `/gate` page route, `POST /api/gate/checkin`,
`POST /api/dispatch`, `assign_specific_spot()` (dead once both callers went),
the `.phone-*`/`.gps-*` CSS block (236 lines), and the orphaned `.checkin-bar`
rule.

**One thing that needed care:** `dashboard.html` went back to
`operator-pane-full`, but that class only set `width: 100%` — the flex column
layout lived on `.operator-pane`, the class for when there were two panes.
Swapping without folding those properties across would have collapsed the
header and canvas. This is the sort of breakage that renders rather than
errors, so it would have been found by eye or not at all.

**Side effect worth having:** with the public portal gone, *every* page and API
needs a staff session. The only unauthenticated surfaces left are `/healthz`
and `/webhooks/simulator`. The "which parts are public and why" carve-out in
`app/auth.py` is gone with it.

**Verified:** `/gate`, `/api/gate/checkin`, `/api/dispatch` and both deleted
scripts return `404`; `/dashboard` renders full width loading only
`operator_canvas.js`; no `href="/gate"` survives in any rendered page; the full
lifecycle replay still closes with invoice `2.00`.

---

## 5. Edge cases and how they are handled

| Edge case | Handling |
|---|---|
| Duplicate webhook delivery | `EventId` is the SQLite primary key — redelivery is a database-level reject, not application memory that can age out |
| Out-of-order / missing events | `SequenceId` gaps recorded in `sequence_gaps`, never silently swallowed |
| Car parks somewhere other than its reservation | Drift guard frees the stale reservation |
| Car never arrives after dispatch | `RESERVATION_TTL_S` sweep |
| Car already parked at startup | Adopted at `Park/CarIn` |
| Unknown car at the exit | Adopted and charged an estimate |
| Fake payment | Compared against our own computed figure; car held at exit, never released |
| Lot full | Car sent to `leavepark` immediately rather than left to trigger `CarLeftFromEntryBecauseNeglected` |
| Broken spot with a car in it | Repair deferred until `Park/CarOut` |
| Simulator not running at startup | Listener still comes up; seeds from `lvl1.json` and logs loudly |
| Simulator returns an unexpected shape | `detectedCars` accepts list or int |
| Handler throws | Caught, recorded on the event row, returns `200` — a bad event must never kill the receiver |
| Dry-run must not mutate state | Reservation rolled back when no command was sent |
| Simulator unreachable during a manual click | `502` with the reason, logged to the activity feed — never a bare `500` |
| Unknown component name from an operator | `404` before anything is sent to the simulator |
| Staff session expires while a dashboard is open | WebSocket closes `1008`; the page reloads to the login instead of reconnect-looping |
| A role reads a broad endpoint to get around a narrow one | `filter_snapshot()` strips keys their permissions do not cover, on REST and WebSocket alike |
| Simulator bills a duration we did not measure | `should be: (X)` parsed out of the penalty and re-billed |

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
| `BILLING_BASIS` | `planned` | The simulator bills the booked duration (§4.9) |
| `SEED_FROM_LEVEL` | `lvl1` | **Set empty for a scored run** |
| `ADMIN_PASSWORD` etc. | `admin123` … | Demo defaults — change before a real deploy |
| `SESSION_TTL_S` | `28800` | Staff session lifetime (8h) |

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

| Surface | URL | Who |
|---|---|---|
| Split dashboard | `http://127.0.0.1:8080/dashboard` | `lot.view` |
| Operator HUD | `http://127.0.0.1:8080/` | `lot.view` |
| Staff console | `http://127.0.0.1:8080/admin` | any staff — panes by permission |
| Sign-in | `http://127.0.0.1:8080/login` | — |
| Health + counters | `http://127.0.0.1:8080/healthz` | public |
| Session history | `http://127.0.0.1:8080/api/history` | `history.view` |
| Signature calibration | `http://127.0.0.1:8080/api/signature-report` | `diagnostics.view` |
| Earnings + penalties | `http://127.0.0.1:8080/api/finance` | `finance.view` |

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

**Staff auth is demo-grade.** Sessions live in process memory, so a restart
logs everyone out; passwords sit in `.env` rather than hashed in a database.
That is the right trade for a single-process dispatcher and the wrong one for
anything real. Replacing `staff_accounts()` is the whole job — every check
downstream already goes through permissions, not usernames.

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
