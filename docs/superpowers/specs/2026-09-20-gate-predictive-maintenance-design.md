# Gate predictive maintenance: design

**Status:** proposed, awaiting approval
**Branch:** `feature/gate-predictive-maintenance`, cut from `codex/level2-requirements-audit` @ `6c1da62` (PR #4)
**Date:** 20 September 2026

## 1. Problem

Barrier gates break during a run. The dispatcher has no scheme that repairs them
before they fail, and a breakdown is expensive:

| | Preventive repair | Breakdown |
|---|---|---|
| Downtime | ~50 s (team observation) | 160 s (15 of 15 observed) |
| Fine | none | 20.00 per `component_broken` |
| Traffic | chosen moment, idle gate | arbitrary moment, possibly with cars queued |

Gate failure cannot be assumed to follow a fixed rule. The history shows breaks
at 10 opens after a repair in 12 of 12 clean cases after 00:59 on 20 September,
but at 13–43 opens before that. So maintenance must **learn** when a gate is
likely to fail, from this park's own history, and keep learning as it runs.

Three current behaviours make it worse:

1. `check_wear` repairs at 85% of `WEAR_CYCLE_THRESHOLD=500`. Gates break long
   before that, so it never fires for gates.
2. `ml_agent.run_predictive_loop` (merged from `main`) trains on two fixed
   anchor points: `broken` → 1.0, and `fixed_proactive` → 0.85 "did not fail". Once
   preventive repair works, nearly every sample becomes "did not fail", and the
   model learns that gates never break.
3. Wear counters are saved and restored as `max(saved, live)`. The simulator
   resets its counters on every level load, so after a reload we think gates are
   far more worn than they are.

## 2. Goals and non-goals

**Goals**
- Predict, per gate, the probability that it breaks within the next few opens.
- Repair preventively when that risk outweighs the cost of a repair now.
- Stagger repairs so gates are not all out at once, and use the zone ratio to
  choose which gate goes first.
- Keep working from a cold start with no history.
- Make each decision visible: risk on the dashboard, reason in the activity feed.

**Non-goals**
- Fans and parking bays. They keep the existing sweep; this design only removes
  gates from it.
- Polling the simulator. Every input is our own webhook history.
- Changing how exits are routed. The simulator chooses the exit.

## 3. Approach: survival analysis over "opens since repair"

Each time a gate is repaired a new **lifetime** starts. A lifetime ends one of
three ways:

| End | Meaning | Survival term |
|---|---|---|
| `broken` | failed at N opens | **event** at N |
| `fixed_proactive` | we repaired it at N opens; it was still working | **censored** at N |
| level load | the simulator reset it at N opens | **censored** at N |

The estimator is **Kaplan–Meier over opens**. For each open count k it gives the
hazard h(k) = P(break on open k+1 | survived k opens). Censored lifetimes count
as "survived to N" and are never counted as failures. That is what keeps the model
correct once preventive repair has removed most breakdowns from the data.

Why this rather than main's logistic regression:
- It handles censoring, which logistic regression does not.
- It is exact whatever the data turns out to be. If gates always break at 10,
  h(9) → 1 and h(<9) → 0. If failures are spread out, the hazard rises gradually.
- It is transparent: the risk shown on the dashboard is a count anyone can check.
- It needs only numpy, no scikit-learn, and there is a pure-Python fallback.

**Pooling:** all gates share one curve by default, because gates appear identical
and pooling multiplies the data. A per-gate curve is used once that gate has at
least 5 events of its own; this covers a gate that is genuinely different.

**Smoothing:** with few samples the raw hazard jumps between 0 and 1. A small
prior is blended in and shrinks as data accumulates: 2 pseudo-lifetimes failing
at the cold-start point, see §5.

**Seconds as a second axis:** opens are the primary clock. Time since repair is
recorded on every lifetime so a later version can check whether time matters as
well. The first version does not model it.

## 4. Decision rule

For gate g with k opens since its last repair, with m = 2 the look-ahead (the
next opens before we can realistically act):

```
risk(g) = 1 − ∏_{j=k}^{k+m−1} (1 − h(j))     # P(breaks within the next m opens)
```

Repair when the expected cost of waiting exceeds the cost of repairing now:

```
risk(g) · C_break  ≥  C_prevent
C_break   = fine (20.00, learned from component_broken.FineAmount)
            + 160 s × DOWNTIME_COST_PER_S
C_prevent = RepairCost (learned from fixed_proactive events; 0 until seen)
            + 50 s × DOWNTIME_COST_PER_S
```

This gives a **risk threshold** p* = C_prevent / C_break. With the defaults
(`DOWNTIME_COST_PER_S = 0.1`, no repair cost seen yet), p* = 5 / 36 ≈ 0.14. The
threshold moves automatically as real fine and repair-cost figures arrive.

Two bands, to stagger repairs:

| Band | Condition | Action |
|---|---|---|
| **Opportunistic** | risk ≥ p*/2 and the gate is idle | repair if it is this gate's turn (§6) |
| **Required** | risk ≥ p* | repair as soon as the gate is idle; never wait for a turn |
| **Critical** | risk ≥ 0.9 | entry gate: stop dispatching into its zone (the ratio routes cars elsewhere), then repair. Exit gate: repair at the first idle moment, ignoring the one-exit limit |

"Idle" means: the gate is closed, no car holds it (`_holding_gate`), no paid car
is waiting at it, and it is not already under repair. A repair is never sent to a
moving or held gate, so it cannot draw `Penalty_OperateElementWhileUnderRepair`
or catch a car.

## 5. Cold start

With fewer than **3 recorded breaks** there is no curve worth trusting, so the
dispatcher uses a conservative rule:

- Before any break has been seen: treat a gate as **Required** at 6 opens.
- Once at least one break exists: Required at **60% of the smallest observed
  failure count**, with a floor of 3 opens.

Both are configurable (`GATE_COLD_START_OPENS`, `GATE_COLD_START_FRACTION`). The
same number feeds the smoothing prior in §3, so the switch from rule to model is
gradual rather than a jump.

## 6. Staggering and zone ratio

At most **one entry gate and one exit gate** may be in preventive repair at the
same time. This keeps at least two of the three zones reachable and at least two
exits open. When several gates are in the Opportunistic band, the sweep picks one:

- **Entry gates (1/3/5):** the gate whose zone has the **highest** ratio,
  `(occupied + reserved + broken + under repair) / bays`. That zone receives the
  fewest new cars, and while its gate is down dispatch already skips it. The ratio
  counts an under-repair gate's zone as blocked, so traffic moves to the other zones.
- **Exit gates (2/4/6):** the gate whose zone has the **lowest** ratio: fewest
  parked cars, so fewest cars about to leave through it.
- **Tie-break:** higher risk first, then natural gate order.

Gates in the Required band skip the queue but still respect the one-entry/one-exit
limit, unless the risk is Critical.

**The main gate (gate7)** is operator-only and is never repaired automatically.
When its risk reaches Required, the dashboard raises an alert for staff to repair it.

## 7. Data

**New table `gate_lifetimes`:**

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `gate` | TEXT | |
| `started_at` | TEXT | ISO time of the repair or level load that began it |
| `ended_at` | TEXT NULL | NULL while open |
| `opens` | INTEGER | opens counted during this lifetime |
| `seconds` | REAL | wall seconds × game speed |
| `outcome` | TEXT NULL | `broken`, `preventive`, `level_reset`, or NULL while open |

- An open (sent by us, automatic or manual) increments `opens` on the gate's open
  lifetime.
- `component_broken` closes the lifetime as `broken`. `component_fixed` starts a
  new one; an interrupted open lifetime is closed as `preventive`.
- A level load (`_on_level_loaded`) closes every open lifetime as `level_reset`
  and starts fresh ones. On a level load the saved gate wear counters are **reset
  to 0**, not max-merged, to match the simulator.
- **Backfill:** a one-off migration rebuilds past lifetimes from `events`
  (`gate_action` Open, `component_broken`, `component_fixed`), so the model starts
  from today's 18 samples rather than from nothing. The backfill counts Opens the
  simulator *reported*, while live counting uses opens we *sent*. These match
  unless a webhook was lost. The backfill cannot see past level loads, so its
  pre-00:59 samples (13–43 opens) are marked `level_reset`, which is censored,
  rather than trusted as failures.

The History dustbin does **not** clear `gate_lifetimes`. It is the model's memory,
not operational history.

## 8. Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `app/gate_health.py` (new, pure) | Kaplan–Meier hazard with censoring and prior; `risk(k, m)`; decision band; the staggering picker | numpy optional |
| `app/db.py` | `gate_lifetimes` table, open/close/increment helpers, backfill | sqlite |
| `app/main.py` | count opens; close lifetimes on broken/fixed/level load; `_gate_maintenance_sweep()` run from the existing wear loop and after each gate close; Critical gates excluded from `_gate_usable` | `gate_health`, `_queue_repair` |
| `app/ml_agent.py` | skip `BarrierGate` in `run_predictive_loop`; gates are owned by `gate_health` | — |
| `app/state.py` | snapshot adds `risk`, `opens_since_repair`, `band` per barrier | — |
| `static/js/components/alerts.js` + gate drawer | show risk %, band and why; alert for gate7 at Required | snapshot |
| `app/config.py`, `.env.example` | `DOWNTIME_COST_PER_S`, `GATE_LOOKAHEAD_OPENS`, `GATE_COLD_START_OPENS`, `GATE_COLD_START_FRACTION`, `GATE_MIN_EVENTS_PER_GATE` | — |

`gate_health` holds no state and makes no calls to the simulator. It turns
lifetimes and counts into numbers, so it can be tested in isolation.

## 9. Error handling

- **Model failure** (bad data, numpy missing): fall back to the cold-start rule.
  Log it once, and never block dispatch.
- **Repair command fails:** the existing queue drop path clears `pending_repairs`.
  The gate stays in its band and the next sweep retries.
- **Missed webhooks:** an Open that never came back as a `gate_action` still
  counts, because we count the opens we *send*. A `component_fixed` without a
  preceding broken closes the lifetime as `preventive`.
- **Dispatcher restart mid-lifetime:** open lifetimes persist in SQLite and resume.

## 10. Testing

`gate_health`:
- all-fail-at-10 data gives h(9) ≈ 1 and near-zero hazard below;
- censored lifetimes lower the hazard and are never counted as failures;
- a spread of failures gives a gradually rising hazard;
- the prior dominates with 1 sample and fades by about 10;
- band thresholds follow the cost inputs;
- the picker chooses the highest-ratio zone for entry gates and the lowest for
  exit gates, and respects the one-entry/one-exit limit.

Integration:
- opens increment the open lifetime;
- broken, fixed and level load close lifetimes with the right outcome;
- the sweep never repairs a held, open or moving gate, or gate7;
- a Critical gate's zone is skipped by dispatch;
- the ML sweep skips gates;
- backfill on today's database reproduces the 18 break samples.

Live, on the first run:
- every preventive repair logs its predicted risk and its real duration;
- count breakdowns versus preventive repairs over one level, compared with the
  previous run's 18 breaks.

## 11. Rollout

1. Merge PR #4 first. This branch builds on `ml_agent` and on the gate holds.
2. Ship with `GATE_MAINTENANCE=predictive` (default) and allow `=off` to disable
   it for a demo if needed.
3. After one live level, check the logged risks against actual breaks and tune
   `DOWNTIME_COST_PER_S` if repairs look too eager or too late.

## 12. Open questions

- The ~50 s preventive repair time is a team estimate. The first run will measure it.
- Does the simulator charge for a preventive repair (`RepairCost` on
  `component_fixed`)? If it does, p* rises automatically.
- Whether time since repair matters as well as opens. The first version only
  records it (§3).
