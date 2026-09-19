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
| `app/zones.py` | Zone ratio, zone choice, zone entry-gate lookup (pure functions) |
| `app/simlog.py` | Follows the simulator console log for `Load Game` lines (level-load detection) |
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

1. `car_spot_action` / `EntrySpot` / `CarIn` — pick the lowest-ratio zone and a
   spot in it, **reserve it**. A ZONE1 car: open gate1, wait for `Open`, `goto <spot>`.
   A ZONE2/3 car: `goto ENTRY2/ENTRY3` first; when it is waiting there, open that
   zone's gate, wait for `Open`, `goto <spot>` (4.20)
   The zone gate closes `ENTRY_GATE_CLOSE_DELAY_S` after the car leaves the sensor
   box in front of it, unless the next car already holds it (4.21)
2. `Park` / `CarIn` — spot becomes occupied, **billing clock starts here**
3. `Park` / `CarOut` — billing clock stops, spot released
4. `ExitSpot` / `CarIn` — compute the charge, `POST /charge`
5. `payment_made` — validate against our own figure
6. only if valid → open the exit gate, wait for `Open`, `goto leavepark` (4.20)
7. `ExitSpot` / `CarOut` — session archived to SQLite; the exit gate closes
   `GATE_CLOSE_DELAY_S` later

Gates start closed at level start. The main gate (gate7) is opened by staff only (4.18).

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

### 4.16 Level 2 requirements audit and reliability corrections (19 September 2026)

The supplied Level 2 PDF takes precedence over the pasted sprint checklist. Full
traceability is in `docs/LEVEL2_REQUIREMENTS_AUDIT.md`. The actual baseline here was
135 passed / 1 failed: failed username insertion left SQLite inside a transaction.
Authentication writes now roll back, and login history uses the canonical username.

Recovery preserves live occupied/broken/maintenance bays and dispatch retries
recheck reservations before sending. Known exits are not reaped as missing exits.
Paid-but-unreleased vehicles can resume departure on recovery or another valid
payment without a second invoice; concurrent/recursive release is guarded.

Wear runtime now uses simulated seconds; movement transitions count once. Repair
queue entries exclude new bay reservations and controls, deduplicate work, stop
running fans before preventive repair, and wait for moving gates. Failed retries
release their suppression flags and do not block unrelated tasks during backoff.
Broken components discovered by sync are also scheduled. Lights retain real fault
state but have no repair command in the documented simulator API. Repaired fans
reconsider the last known CO reading. Runtime thresholds remain configurable and
must be calibrated against the simulator; no rated thresholds were available in
discovery responses or level JSON.

When a barrier reports `component_fixed`, pending maintenance state is cleared and
the recovery coordinator immediately resumes both assigned entrance dispatches and
paid departures that were paused while the gate was unavailable.

The live dashboard shows wear and health, gate holds work even when already closed,
and reports refresh from local WebSocket ticks. Daily fines use UTC event receipt
date like other operational totals (legacy rows without events fall back to server
date). Narrow layouts keep controls accessible. Simulator request timeout and
empty-level recovery cooldown now scale with game speed.

Validation: compileall, **160 passing pytest cases** (2 dependency deprecation
warnings), all 24 JavaScript modules parsed with
Node, and browser checks against a separate dry-run Level 2 preview. Browser checks
covered login, component data, admin reports, manual refresh, technician capability
visibility and 390px report layout. No live simulator mutation, production database
change, `.env` edit or application dependency added. Pytest was missing from this
checkout and was installed in its existing Python 3.13 development environment.

### 4.17 Final Level 2 submission hardening (19 September 2026)

**Symptoms:** malformed or non-finite planned durations could reach billing;
startup synchronization flattened broken/maintenance bays to available and ignored
simulator usage counters; a restart lost sequence/penalty observability and an
unattempted exit invoice; failed accepted webhooks were acknowledged as processed.
Unserved arrivals were inconsistently retained, queued preventive work could
interrupt equipment still needed for operations, and queued repairs could appear
healthy in the live UI.

**Root causes:** input coercion accepted any `float`; discovery loaders derived bay
state only from occupancy; several important counters existed only in memory; the
webhook error path used the same processed marker as success; and repair queue state
was enforced in control paths without being projected into snapshots. Reporting also
mixed booked and observed duration, treated every unpaid archive as suspect, omitted
repair costs from net revenue, and dated revenue by the event receive date instead
of the verified payment receipt.

**Fix:** planned minutes now require a finite positive value. Live discovery imports
fault state, risk and usage counters, while startup restores sequence and penalty
totals. Exit-confirmed invoices resume after restart. Handler failures stay durable,
return `500`, appear in health/alerts, and retry on a signed redelivery with the same
EventId. Arrival failures remain trackable, successful turn-aways become neglect
records instead of completed sessions, and unknown entrance departures no longer
leak memory. Preventive repairs wait for held/moving gates and safety-critical fans;
pending work is presented as unavailable in snapshots, alerts, drawers and
`/api/broken`. History separates actual simulated parked time from booked time and
distinguishes unpaid from suspect payments. Daily/all-time net revenue deducts repair
costs and daily paid revenue uses the payment receipt date. The Level 2 fallback seed
now includes all lights and usage counters.

**Verification:** focused submission-hardening regressions: 25 passed. Complete
suite: **185 passed** with two dependency deprecation warnings. Compileall, Ruff
fatal/error/late-binding checks, dependency consistency, JavaScript parsing and
`git diff --check` passed. Browser QA used `AUTOPILOT=false`, an unreachable
simulator URL and a disposable Level 2 database. It confirmed 90 parking bays,
7 gates, 12 fans, 30 lights, the previous-three-login display, the unprocessed
webhook alert, repair-cost net revenue (20.00 - 7.50 = 12.50), and distinct actual
4.3-minute versus booked 10-minute history values. Browser console errors: none.

### 4.18 Every Level 2 car opened gate1, and gates were never closed (19 September 2026)

**Symptoms:** gates stayed open for the rest of a run after the first car. A car
bound for ZONE3 still caused `open gate1`, and staff holding gate1 closed stopped
*all* dispatch, not just ZONE1 (the "stuck at ENTRY1, no bay yet" incident).

**Root cause:** Level 2 has one road in, behind the operator's main gate (gate7).
Every car trips ENTRY1, then ENTRY2/ENTRY3 on its way down to its zone. Each zone
has its own entry gate (gate1/3/5, left side) and exit gate (gate2/4/6, right
side). The dispatcher mapped the *first sensor* to the nearest gate (ENTRY1 →
gate1) and opened that for every car, whatever zone the car was going to. It only
worked because nothing ever closed a gate. The only automatic closes were exit
holds for suspect payments and unknown cars.

**Fix** (`app/zones.py`, `app/main.py`):
- **Level start:** on the first sync that finds live bays, every gate is closed,
  main gate included, once per level (`_close_gates_for_level_start`). A level
  load seen in the simulator console resets this (4.19).
  This is a plain close, not the persisted operator hold, so it cannot block
  dispatch the way a stale hold did.
- **Zone choice:** the car goes to the zone with the lowest
  `(occupied + reserved + broken + under repair) / bays` ratio, counting each bay
  once. A tie goes to the nearer zone. Within that zone it takes the nearest
  suitable bay, ranked as before. Only zones with a suitable bay *and* a usable
  entry gate take part, so a held zone gate diverts the car to another zone.
- **Entry gates:** the gate opened is the target zone's entry gate: the
  zone-tagged barrier nearest an entry sensor. Level 1 falls back to the old
  nearest-gate rule. *Superseded in part by 4.20:* the gate now opens only when
  the car is waiting at the zone's own sensor. It closes on `Park/CarIn` once no other car is still heading
  to that zone, and also when a reservation expires.
- **Exit gates:** still opened only after a verified payment. They close
  `GATE_CLOSE_DELAY_S` after `ExitSpot/CarOut`, because no sensor reports the car
  clearing the gate. They stay open while another paid car is waiting there.
- **Main gate** (`MAIN_GATE`, default `gate7`): never operated automatically.
  Staff open it from the dashboard. While it is shut, Live shows a red alert,
  because cars queued in front of it send no event.

**Verification:** `tests/test_zone_gates.py`, 10 cases written before the code:
ratio counting, tie-break, entry-gate lookup, emptiest-zone dispatch through
gate5, held-gate diversion, all-held waits rather than turning the car away,
close-after-last-park, idle close skips the main gate and unavailable gates,
exit gate held for a paid car, and a once-only level-start close. Full suite:
**203 passed**. Dry-run server seeded with Level 2: the snapshot flags gate7 as
the main gate, and an empty-lot dispatch picks ZONE1/S1 on the tie-break. Not
yet exercised against the live simulator.

**Watch on the first live run:**
- Gates now cost two cycles per car, so they reach the 85% preventive-repair
  point about twice as fast.
- A restart of the dispatcher, or a new level load, closes gate7 again.
- It is unknown whether the simulator fines cars queued behind a closed main
  gate for neglect.

### 4.19 Recycled plates and level reloads: no invoice, never dispatched, ghost cars (20 September 2026)

**Symptoms:**
- Cars left without paying.
- Some arrivals sat at ENTRY1 and were never sent to a bay.
- Repeat plates showed as "Paid".
- gate2 and gate7 were open when Level 2 was clicked.
- The dashboard showed a Level 1 map until the first car arrived.
- 23 sessions sat "charged" at exits. Six of them (FLL 178, FKX 546, BXT 906,
  QNL 430, CLK 433, TRB 130) had no events at all in the current run.

**Root causes:**
1. **Plates are recycled.** The simulator draws plates from a fixed list
   (`settings/plates.txt`), so every level load replays the same plates.
   `start_session` reused the in-memory session of a returning plate, so the new
   car inherited `charged`/`paid` and its old bay. The entrance short-circuit
   (`active_dispatches`) then skipped dispatch: VBK 204, LMD 406 and FRA 222 were
   never sent. The exit handler's `if session.charged: return` skipped the invoice:
   KTR 684 and WRW 914 got none and would escape.
2. **Nothing marked a level reload.** Loading a level sends no webhook; the first
   event is the first car (seq 22195). The previous level's sessions stayed in
   memory. Sessions already at an exit are exempt from the orphan sweep, so they
   stayed forever, kept exit gates "in use" (gate4/gate6 never closed), and drew
   `[ERROR] Car not found` from the simulator.
3. **Stale holds.** gate2 was open at load. An operator pressed Close, which is a
   *persistent hold*, and every paid release at EXIT_EXIT was blocked from then on
   (RKP 735, KRK 711 and TQM 683 paid and sat there).
4. `SEED_FROM_LEVEL=lvl1` made `running_level()` report Level 1 whenever no bays
   were loaded.

**Fix:**
- **One session per visit** (`_retire_previous_visit`). An EntrySpot `CarIn` for a
  plate whose session already parked, reached an exit, or was billed archives that
  visit and starts a fresh one. A car still driving to its bay keeps its session
  across ENTRY2/ENTRY3, as in 4.12.
- **Level-load detection from the simulator console** (`app/simlog.py`). The
  simulator is a console program (PE subsystem 3). START.bat now launches it through
  PowerShell `Tee-Object`, so its window still shows output and `data/simulator.log`
  receives a copy (UTF-16LE with BOM, decoded incrementally). The dispatcher follows
  that file. On `Load Game./settings/lvlN.json` it:
  - drops the previous level's live sessions (memory and `active_sessions`),
    operator holds and ghost holds (held by key pattern since 4.20 - the first
    version keyed them on in-memory gate names and missed them);
  - clears the gate/bay model;
  - announces the level so the map appears at once;
  - syncs, retrying for up to 15 s while the simulator loads. That is one sync per
    level load, as the docs require.

  The sync closes every gate, gate7 included. Reading a local file is not an API
  call, so the docs' "list-* ONLY once per level loading" rule holds. Content
  already in the file at dispatcher start is skipped, and a shrunken file (a
  relaunched simulator) is read from the top. If the captured launch dies within
  4 s, START.bat relaunches the simulator plainly; level loads are then noticed
  only at the first car.
- `SEED_FROM_LEVEL` is empty in `.env`/`.env.example`, so the map is blank until a
  level is known. The Live alert now reads "No level running".

**Verification:** `tests/test_level_reload.py`, 5 cases written before the code:
- a returning plate gets a fresh session and a goto, and the old visit is archived;
- a car still in flight keeps its session at ENTRY2;
- UTF-16 decoding across a split write, skipping pre-existing lines;
- re-reading after the simulator relaunches;
- level load clears sessions, holds and DB rows, and closes gate2 and gate7.

Full suite: **208 passed**.
- **Pipeline check:** a stand-in console command piped through the same
  `Tee-Object` line produced a UTF-16 file from which the follower read `lvl2`.
- **Dry-run server:** `/api/twin` was empty before, and showed 101 bays, 7 gates
  and 3 zones about 1 s after a `Load Game` line was appended.

**Live-verified on 20 September:** the real simulator ran normally under `Tee-Object`, and its
`Load Game./settings/lvl2.json` line triggered the reset, sync and gate closes (4.20).

### 4.20 Goto sent through a closed gate: paid cars stuck at exits, zone gate open too early (20 September 2026)

**Symptoms:**
- Paid cars sat at exits.
- A car heading for ZONE3 had gate5 opened while it was still at ENTRY1, far
  from the gate.
- gate2 was still on hold after a level load, so AVF 707 paid at EXIT_EXIT and was
  never released.

**Evidence:** the simulator console, now captured to `data/simulator.log`.
- `POST barrier-gates/gate4/open`, then `POST car/RAK 680/goto/leavepark` 10 ms
  later, then `[ERROR] No valid escape spot found for car: RAK 680`. The same
  happened for AKC 169; gate6 reported `Open` only after the goto. RAK 680 then
  "is bored" and left a minute later.
- `open gate3`, then `goto bay60`, then `Paths found: 0`; gate3 `Open` came
  afterwards (WRC 747).
- `[ERROR] Won't spawn car No path from A to P2` while gate7 was closed.
- `[ERROR] Car (X) is not waiting at the entrance or exit`, 24 times: every one a
  *resent* goto after the first had been accepted. This is noise, but it shows the
  simulator only accepts a goto from a car waiting at an entry or exit sensor.
- `gate_override:gate2 = 1` survived the level load. The dispatcher started before
  Level 2 was clicked, so no gates were in memory, and 4.19's reset looped over
  in-memory gate names.

**Root causes:**
- The simulator plans the route *at goto time* and treats a closed or rising
  barrier as a wall. `_wait_for_barrier_open` existed but was no longer called
  anywhere, so both entry and exit sent the goto while the gate was still rising.
- The level reset cleared holds by gate name instead of by key.

**Fix:**
- `_open_gate_and_wait` precedes every entry goto, and `_send_paid_release` waits
  for `Open` before `leavepark`.
- **Two-hop entry** (`ZONE_GATE_AT_SENSOR`, default on). A car whose zone gate is
  not in front of its current sensor is first sent to that zone's own sensor
  (`_zone_sensor`: ENTRY3 for gate5), with the gate still closed. Its EntrySpot
  `CarIn` there (`staged_via`) starts leg 2: open the gate, wait for `Open`, goto
  the bay. Each leg stops itself once the car waits at another sensor
  (`_waiting_at`), so the ENTRY1 leg cannot keep resending in parallel.
- If the car is still at ENTRY1 after `HOP_ATTEMPTS` (5) hop sends, the simulator
  is taken to have refused an entry sensor as a destination. The zone gate is then
  opened from ENTRY1, as before. Cars were seen to leave ENTRY1 1.1-2.9 s after an
  accepted goto, so 5 sends at 1 s cover it.
- `db.reset_live_level()` clears every `gate_override:%` / `gate_holds:%` key.

**Verification:** 6 tests written first:
- ZONE1 goto only after gate1 `Open`;
- ZONE3 car sent to ENTRY3 with no gate opened, gate5 opened on arrival, and the
  bay goto made with gate5 `Open`; exactly one hop, and the ENTRY1 leg ends;
- a refused hop falls back to opening gate5;
- `leavepark` only after gate6 `Open`;
- holds cleared with no gates in memory.

The ENTRY3 test caught a real race: leg 1 kept sending in parallel with leg 2.
That is what `_waiting_at` fixes. Full suite: **213 passed**.

*Update (4.27): the two-hop is now off by default. The simulator treats
`goto ENTRY3` as parking at ENTRY3, which led to fines and escapes.*

**Not yet verified live:**
- whether the simulator accepts `goto ENTRY3`. If it does not, cars wait about 5 s
  at ENTRY1 and then enter the old way; set `ZONE_GATE_AT_SENSOR=false` to skip
  the wait.
- whether a car waiting at ENTRY3 blocks the road for cars behind it bound for
  other zones.

### 4.21 Zone gate stayed open until the car parked (20 September 2026)

**Symptom:** a zone entry gate opened for a car and stayed open for the car's
whole drive to its bay. It closed only on `Park/CarIn`.

**Root cause:** 4.18 counted an entry gate "in use" for any car that was assigned
but not yet parked.

**Fix:** a gate is held *per car*, from the moment it is opened for that car
(`_holding_gate`). The car releases it when it leaves the sensor box in front of
that gate: an EntrySpot `CarOut` at the sensor whose nearest barrier is the held
gate. `_close_idle_gates` then runs `ENTRY_GATE_CLOSE_DELAY_S` (1.5 simulated s)
later and closes the gate unless the next car already holds it. The next car
repeats the same cycle.

A ZONE3 car leaving ENTRY1 does not release gate5 (fallback mode opens gate5
from ENTRY1). Only leaving ENTRY3 does. `Park/CarIn` still releases the hold as a
backstop.

The 1.5 s delay covers the ~140 px from the sensor centre to the gate. Cars were
measured at 230-870 px/s (ENTRY1-out to ENTRY2-in: 1.5-5.6 s over about 1,300 px).

**Verification:** 3 tests written first:
- gate5 closes after its car leaves ENTRY3, not before the delay and before the
  car parks;
- a second car holding the gate keeps it open, and exactly one close follows the
  second car;
- leaving ENTRY1 does not close gate5, and leaving ENTRY3 does.

Full suite: **215 passed**.

### 4.22 Predictive ML layer (`app/ml_agent.py`)

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
  *Superseded by 4.23: fans now use fixed edges, ON above 50 and OFF below 15.
  `co_ventilation_analysis` / `co_off_threshold` are no longer called.*
  *The predictive repair sweep is off by default since 4.31: it queued every
  bay and fan at once.*
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

*Numbered 4.16 on `main`; renumbered to 4.22 when merged with the
`codex/level2-requirements-audit` branch, which already had a 4.16.*

*START.bat previously ran `pip install -r requirements.txt` only when `.venv` was
missing, so existing installs never received numpy/scikit-learn and this layer ran
on its fallbacks without saying so. START.bat now re-checks dependencies on every start.*

### 4.23 Fans: on above CO 50, ventilate down to 15 (20 September 2026)

**Requirement:** once CO passes 50, run the zone's fans until CO is back below 15.
The wide band leaves room for a broken fan to be repaired before CO climbs back.

**What was there:** 4.22's occupancy-dependent edges. The ON edge fell to 30 in a
full zone (a test with a full zone switched the fan on at 45), and the OFF edge
was 15-30.

**Fix:** `_handle_carbon_monoxide_event` uses `CO_FAN_ON_THRESHOLD` (50, strictly
above) and `CO_FAN_OFF_THRESHOLD` (default changed from 30 to 15, strictly below).
Preventive fan repair is still postponed while CO is at or above the OFF edge,
which now means 15.

**Verification:** a readings sequence 45, 50, 51, 40, 16, 15, 14 gives exactly
one ON (at 51) and one OFF (at 14), in a full zone. `test_sprint_backend`'s
hysteresis test was updated from the old 30 edge to 16/14.

### 4.24 Preventive bay repair after a number of parks (20 September 2026)

**Evidence:** 18 bay breakdowns happened after 13-19 parks (15 most often).

**What was there:** bays already counted parks (`mark_parked`, once per visit),
but the only preventive rule was 85% of `WEAR_CYCLE_THRESHOLD=500`, which a bay
never reaches.

**Fix:**
- `check_wear` repairs a bay once its parks since the last repair reach
  `SPOT_PREVENTIVE_PARKS` (9, a margin below the earliest breakdown seen).
- An occupied bay is not repaired: the existing deferral queues the repair for
  the moment the car leaves. A bay awaiting repair is never dispatched to, and it
  counts in the zone ratio.
- **Found on the way:** saved wear counters survived level loads (`max(saved,
  live)` on restore), so every bay used in the previous level would have been due
  at once. `db.reset_live_level()` now zeroes `component_wear` and clears
  `pending_proactive_repair:*`.

**Verification:**
- three parks make an empty bay due;
- an occupied worn bay waits, and is queued the instant it empties and taken out
  of dispatch;
- below the threshold nothing happens;
- a level load zeroes saved wear.

Initially I also added a second counter, which double-counted every park. The
first test run caught it.

### 4.25 Ghost cars: billed automatically, released only by staff (20 September 2026)

**Definition:** a car that reaches an exit without ever being seen at an entrance.
Either it is unknown here, or it appeared straight into a bay. The second case
was previously "adopted" and billed like a normal car.

**Fix:**
- `_handle_ghost_car` now covers both cases. It holds the exit barrier and
  **invoices automatically** with the ML fee (`ml_agent.ghost_car_anomaly_imputation`).
  `POST /car/{plate}/charge` is the simulator's "ask car for payment", sent after
  the usual `EXIT_CHARGE_DELAY_S` settling wait, once only (`claim_charge`).
- A valid payment marks the car paid but **does not release it**.
  `_release_paid` refuses a ghost unless `release_authorized`, so neither the
  payment path nor the resume path (`_ensure_barriers_open`) lets it go.
- `POST /api/ghost-cars/{id}/release`, gate staff and admin only
  (`ops:control_gates`): opens the gate, waits for `Open`, sends `leavepark`, and
  resolves and audits the event. An unpaid car needs `confirm_unpaid` (the UI
  asks first), because it would leave unpaid and draw CarEscapedWithoutPaying.
- `GET /api/ghost-cars` adds `invoiced` / `payment_received`. These are status
  flags, not amounts, so operators see them; `paid` and `fallback_charge` stay
  financial-only.
- **Dashboard:** one red banner per ghost car, showing the bill status and
  **Open gate & release** (or **Release unpaid…** with a confirmation) and **✕**.
  There is no timeout. ✕ hides the banner in that browser only (localStorage),
  and a release removes it everywhere.

**Verification:** tests cover:
- automatic invoice and hold for an unknown car, and for a car that appeared in a bay;
- payment never releases the car;
- a staff release opens the gate before `leavepark` and resolves the event;
- an unpaid release is refused without confirmation;
- only operators and admins may release.

The old sprint test encoded "payment releases a ghost" and was rewritten to the
new rule. Not browser-tested: the banner itself.

### 4.26 Zone maintenance: close, drain, repair both gates, reopen (20 September 2026)

**Requirement:** when a zone gate needs repair, send cars elsewhere, repair the
zone's entry and exit gates, and let parked cars leave first. Keep the zone shut
until both gates are fixed.

**Fix** (`_start_zone_maintenance`, `_advance_zone_maintenance`):
- A `BarrierGate` repair request, whether from a breakdown or a preventive
  trigger, for a gate that belongs to a zone closes that zone
  (`state.zone_maintenance`). `_zone_gates` gives each zone's entry gate (by
  zone tag) and exit gate (by geometry from the zone's exit sensor, because
  gate2 carries no zone tag).
- Dispatch skips closed zones, so the ratio routes cars to the others. If every
  suitable zone is closed, the car waits ("Zone closed for maintenance") and is
  not turned away.
- **Repair order:**
  - a broken gate is repaired at once;
  - the entry gate once no car is still driving into the zone;
  - the exit gate once the zone is empty: no occupied or reserved bay, and no
    session parked there or still leaving. Parked cars leave at the end of their
    booked stay, and are not forced out.
- `component_fixed` ticks a gate off. The zone reopens only when both gates are
  done, so the entry gate stays shut in between.
- *Since 4.28 the two gates are repaired one after the other, never together:
  only one gate in the park may be in repair at a time.*
- The main gate never triggers zone maintenance.
- Progress is re-checked on park, on exit, on repair completion and on every
  wear sweep. Level load clears it.
- The Live page lists a closed zone under "Needs attention".

**Found on the way:** the first exit-gate lookup reused `_barrier_for_sensor`,
whose "the only barrier" fallback made any lone gate look like a zone's exit
gate. Two existing maintenance tests caught it; the lookup now uses geometry only.

**Verification:** tests cover:
- zone → gates mapping, including gate2 via EXIT_EXIT;
- a broken exit gate closes its zone, is repaired at once, and the next car goes
  elsewhere;
- a preventive entry repair goes now while the exit waits for the last car;
- the zone stays closed after one fix and reopens after both;
- the entry repair waits for an inbound car;
- gate7 is excluded.

Full suite: **230 passed**.

### 4.27 The two-hop to ENTRY3 parked cars on the sensor: fines and escapes (20 September 2026)

**Symptom:** one run (01:00–01:58) drew **44 × "Car X attempted to park in an
occupied spot: (ENTRY3)"** at 50 each, and **322 escapes**. Of the escapes,
213 cars never paid, and most never reached any exit sensor.

**Root cause:** 4.20's two-hop sent ZONE2/3 cars to `goto ENTRY2/ENTRY3`. The
simulator treats a goto to a spot name as *parking* there:
- if another car already stood on the sensor, it fined an occupied-spot parking;
- otherwise the car "parked" on the sensor, waited out its booked time, and left
  by the escape route without passing an exit. That was fined as an escape.

**Fix:** `ZONE_GATE_AT_SENSOR` now defaults to **false**. The zone gate opens
from ENTRY1, waits for `Open`, then the car is sent to its bay (4.20's wait-for-
Open is kept). The two-hop code remains behind the flag and is tested only with
the flag on explicitly. Do not turn it on.

**Consequence:** a ZONE2/3 gate is again open while its car drives down from
ENTRY1. It still closes 1.5 s after the car leaves the sensor in front of it
(4.21). Opening it only on arrival would need a way to hold a car on the road
without a goto to a spot. That has not been found.

**Also found:** "no payments / 0 completed stays" on the dashboard was the
dustbin (History cleared once, Payments twice), not a billing failure. The
Penalties total was not cleared because the bin on the Payments page only clears
payments; fines are cleared from the Penalties page. That bin was verified to
remove all 385 on a copy of the database.

### 4.28 Balanced gate repairs: one gate in repair at a time, most-worn first (20 September 2026)

**Requirement:** the ML layer is weak, so prioritise balancing. Gates must never be
repaired simultaneously; only one gate may be under repair at any time.

**What was there:**
- Several independent triggers could each queue a gate repair at the same moment:
  a breakdown, zone maintenance (4.26), the 85%-of-500 wear rule (never reached by
  gates), and the ML sweep (4.22).
- There was no count of gate *opens*: `cycle_count` counts every movement.

**Fix:**
- `Barrier.opens_since_repair` counts one per open: starting to open, or a webhook
  reporting `Open` straight from shut. It resets on `component_fixed` and is shown
  in the gate drawer.
- **One repair slot** (`_gate_repair_slot_holder`). `_queue_repair` refuses a gate
  while another gate is under maintenance or queued for it.
- **`_schedule_gate_repairs`** runs when the slot is free: on every wear sweep,
  after every repair, on park and on exit. In order:
  1. a broken gate, most worn first;
  2. the next step of an open zone maintenance: entry, then exit;
  3. preventively, the most-worn idle gate with at least `GATE_REPAIR_MIN_OPENS`
     (5) opens since repair. Only one zone is in maintenance at a time.

  Ranking by wear staggers repairs instead of letting gates come due together.
- The main gate is never repaired preventively.
- The generic wear rule and the ML sweep now skip gates, so nothing bypasses the
  slot.
- A healthy gate repaired this way is flagged preventive (`fixed_proactive`),
  keeping the repair history honest for the ML layer.

**Verification:** tests cover:
- the opens counter and its reset;
- two breakdowns → one repair, then the other as soon as the slot frees;
- the most-worn gate chosen and its zone's entry repaired before its exit;
- nothing below 5 opens;
- gate7 excluded.

Three older tests encoded the replaced rules (85% for gates; both zone gates
queued at once) and were updated. Full suite: **240 passed**.

### 4.29 Night lighting follows car movement per zone (20 September 2026)

**Requirement:** use the simulation time from the webhooks. At night, switch a
zone's lights off when no car is moving there (all parked, or none), and keep them
on where cars are moving.

**Fix:**
- `_refresh_lights` reads the time from `ServerDateTime`. It uses the event's own
  value on every `car_spot_action`, and the latest stored value from the
  environment loop.
- Night is the existing `NIGHT_START_HOUR`–`DAY_START_HOUR` window (19:00–07:00).
- By day every light is off. At night a zone's lights are on only while
  `_moving_zones()` includes it: a car driving to a bay there, or out of its bay
  there and not yet gone.
- A zone stays lit `LIGHT_HOLD_S` (10 simulated s) after its last movement, so
  lights do not switch between consecutive cars.
- Unzoned lights follow any movement. `schedule_lights` without `lit_zones` keeps
  the old all-night behaviour, for its existing test.
- Lights are refreshed on every car event, not just every 15 s.

**Not done, by decision:** "predictive maintenance for lights". The API has no
light repair command, and no light has broken in 107 recorded breakdowns (62
bays, 33 gates, 12 fans).

**Note:** `ServerDateTime` follows the host's clock (02:44 local in the events),
so "night" is real night on the demo machine.

**Verification:** tests cover:
- only the zone with a car driving in is lit;
- all off once every car is parked;
- a leaving car lights its zone until it has gone;
- the hold keeps lights on between cars;
- all off by day.

Full suite: **240 passed**.

### 4.30 Zone lights stayed on after the last car parked (20 September 2026)

**Symptom:** at night a zone's lights did not go off when its cars parked.

**Evidence** (ZONE1, `component_events` against webhooks):
- CKC 119 parked at 18:57:58 and ZONE1 went dark at 18:58:07.
- HVR 690 parked at 18:58:19 and ZONE1 went dark at 18:58:28.

The lights were back on 6–7 s later for the next car sent to ZONE1.

**Root cause:** two delays stacked.
- 4.29's 10 s `LIGHT_HOLD_S`.
- The off-switch only ran on the *next* webhook or the 15 s environment tick. With
  no further event a zone could stay lit for about 25 s after its last car parked.

**Fix:**
- `LIGHT_HOLD_S` defaults to **0**, so the refresh on the parking webhook turns
  the zone off at once.
- For a non-zero hold, `_refresh_lights` schedules one timer (`_relight_after`)
  for the earliest hold expiry, so held zones go dark on time without waiting for
  an event.

**Verification:**
- with the default, a zone goes dark on its last car's park;
- with a hold, lights switch off when the hold ends with no further event.

Full suite: **242 passed**.

### 4.31 Every bay and fan repaired at once; zone lit from ENTRY1 (20 September 2026)

**Symptoms:**
- Nearly every bay showed a repair wrench, including bays that had never had a
  car.
- All fans were repaired at the same moment, just after a CO spike cleared.
- ZONE3's lights came on while its car was still at ENTRY1.

**Evidence:**
- In three minutes `component_events` recorded **270 `repair_triggered_predictive`
  for bays and 36 for fans**. Every one came from the ML sweep (4.22).
- Its logistic model, trained on **107 `broken` vs 1 `fixed_proactive`**, gave
  **P(fail) ≈ 0.99 at every wear ratio from 0 to 1**. That is above its 0.90
  trigger, so every sweep queued everything.
- It had been dormant until START.bat started installing scikit-learn.
- Fans only *looked* simultaneous. Their preventive repair is postponed while
  CO ≥ the OFF edge, so all queued fan repairs fired together the moment CO fell
  below 15.

**Fix:**
- `ml_agent.predictive_sweep_once` queues nothing unless `ML_PREDICTIVE_REPAIRS`
  is on. It defaults to **false**. The ML ghost-car fee estimate is unaffected.
- Bays are repaired only by 4.24's rule (9 parks), so a never-used bay is never
  repaired.
- **One fan per zone in repair at a time** (`_queue_repair`), so a zone always
  keeps ventilating.
- Night lighting (4.29) counts a car as moving in its zone only once it has
  reached *that zone's* entry sensor (`VehicleSession.reached_zone`). That is set
  at dispatch for a ZONE1 car (ENTRY1 is its sensor), and at ENTRY2/ENTRY3 for the
  others.

**Verification:**
- the sweep queues nothing with a 0.99 prediction;
- a never-used bay is left alone;
- two fans in one zone are repaired one at a time, while another zone's fan
  proceeds;
- ZONE3 stays dark at ENTRY1 and ENTRY2 and lights at ENTRY3.

Four lighting tests were updated to mark their car as already in its zone. Full
suite: **246 passed**.

**After this deploy:** the sweep left `pending_proactive_repair:*` flags on the
bays it queued, and `check_wear` skips flagged components. A level load clears
them (4.24).

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
| Handler throws | Caught and retained as unprocessed; returns `500` so a signed redelivery retries the same durable EventId |
| Dry-run must not mutate state | Reservation rolled back when no command was sent |
| A zone's entry gate is held, broken or under repair | That zone is skipped and the car goes to the next-lowest-ratio zone. If every suitable zone is blocked, the car waits (`Held closed by operator` / `Barrier unavailable`) and is not turned away (4.18) |
| A plate returns (plates are recycled from `settings/plates.txt`) | EntrySpot `CarIn` for a session that already parked/exited/was billed archives it and starts a fresh visit; a car still driving to its bay keeps its session (4.19) |
| A level is (re)loaded in the simulator | Console `Load Game` line → previous level's live sessions, holds and model dropped, one sync, all gates closed (4.19) |
| A zone gate needs repair (broken or preventive) | Zone closed to new cars; broken gate repaired now, entry gate when nobody is driving in, exit gate when the zone is empty; reopens when both are fixed (4.26) |
| A car reaches an exit without an entry scan (unknown, or appeared in a bay) | Ghost car: exit held, billed automatically with the ML fee, released only by operator/admin from the banner (4.25) |
| Staff open a zone gate by hand | It is closed again at the next idle check (a car parks, leaves, or a reservation expires). Only `MAIN_GATE` is manual (4.18) |
| Admin clears a page's records (red dustbin on History / Payments / Penalties) | `DELETE /api/admin/data/{history,payments,penalties}`, gated by the admin-only `admin:reset` capability (other roles get `403`; the icon is hidden via `data-cap`). History deletes `sessions` + `neglected_vehicles`; Payments deletes `payments`; Penalties deletes `penalties` and zeroes the in-memory fine counters. `active_sessions`/live sessions are never touched — clearing them mid-run would lose billing state and cause `CarEscapedWithoutPaying`. `events` is never touched — it is the `EventId` de-duplication record. Every clear is written to the audit log with the row count |
| `numpy`/`scikit-learn` missing or import fails | Every `app/ml_agent.py` function falls back to its documented deterministic rule instead of raising (4.22) |
| Ghost car fee imputation | Estimates the invoice shown to staff only; the exit barrier still requires operator override, never auto-released (4.22) |

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
| `SEED_FROM_LEVEL` | `lvl1` in code, empty in `.env`/`.env.example` | **Keep empty.** When set, the dashboard shows that level's map while no level is running, which is how a Level 1 map appeared before Level 2 was clicked (4.19) |
| `ENTRY_GATE_CLOSE_DELAY_S` | `1.5` | Simulated seconds after a car leaves its zone sensor before the zone gate closes behind it (4.21) |
| `CO_FAN_OFF_THRESHOLD` | `15` | Fans run from above `CO_FAN_ON_THRESHOLD` (50) until below this (4.23) |
| `ML_PREDICTIVE_REPAIRS` | `false` | **Keep false.** The ML repair sweep predicted ~99% failure for everything and queued every bay and fan (4.31) |
| `GATE_REPAIR_MIN_OPENS` | `5` | One gate in repair at a time; the most-worn idle gate goes once it reaches this many opens since repair (4.28) |
| `LIGHT_HOLD_S` | `0` | At night a zone stays lit this many simulated seconds after its last moving car; 0 = dark as soon as it parks (4.29, 4.30) |
| `SPOT_PREVENTIVE_PARKS` | `9` | Preventive bay repair after this many parks since the last repair; bays broke after 13-19 (4.24) |
| `ZONE_GATE_AT_SENSOR` | `false` | **Keep false.** When true, ZONE2/3 cars are sent to their zone sensor first, which the simulator treats as parking there (4.20, 4.27) |
| `SIMULATOR_LOG` | `data/simulator.log` | Simulator console captured by START.bat; its `Load Game` line triggers the level reset (4.19). Empty disables it |
| `MAIN_GATE` | `gate7` | Operator-only gate; never opened or closed automatically (4.18) |
| `GATE_CLOSE_DELAY_S` | `3.0` | Simulated seconds after `ExitSpot/CarOut` before the exit gate closes (4.18) |

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

**The predictive repair model has no real time series to learn from (4.22).**
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
`app/auth.py` unmodified; not something 4.22 touched or caused. Worth a
follow-up fix.
*Update 20 September 2026: on the merged `codex/level2-requirements-audit` branch this
test passes (215/215), so the failure did not reproduce there.*

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
