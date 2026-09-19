# park-control

Control centre for the **Grand Park Auto** parking simulator (HackMyIoT Track 2).

Receives simulator webhooks, verifies signatures, logs every event durably, and
drives the park through the REST API.

## Why it is shaped this way

The simulator is a real-time game. Three constraints drove the design:

1. **Never block the simulator.** The webhook route persists the event in one
   synchronous SQLite write, returns `200`, and only then processes it. If we
   thought before acknowledging, we would fall behind and cars would be
   neglected.
2. **Never poll.** The docs state the `list-*` endpoints carry a simulated
   operational cost and should be used "ONLY once per level loading, or after
   crash to do sync." Live state is therefore rebuilt from the webhook stream,
   which is exactly what `SequenceId` is for.
3. **Never trust a payment.** The docs warn that "some cars will tweak the
   system and send fake payment." Every `payment_made` is checked against what
   we actually billed before the car is released.

## Setup

```bash
npm install
cp .env.example .env      # then edit
npm start
```

Point the simulator at the listener — in
`ParkingSimulator-win-x64/settings/settings.json`:

```json
"WebhookUrl": "http://<your-lan-ip>:4000/webhook"
```

Use the machine's actual LAN IP, not `localhost`, unless the simulator runs on
the same host. Confirm the round trip with `GET /api/v1/test` on the simulator,
which fires a `test_webhook` at you.

### Develop without the simulator

```bash
node scripts/seed-from-level.js        # seed 30 spots / 3 gates from lvl1.json
node scripts/replay.js                 # drive a full car lifecycle
curl http://127.0.0.1:4000/health
```

`replay.js` also exercises duplicate delivery, sequence gaps, penalties and CO
events. Set `AMOUNT=0.01` to watch fraud detection trip.

## Configuration

| Variable | Purpose |
|---|---|
| `SIM_BASE_URL` | Simulator API, from `ListenAddress` (default `:9898`) |
| `SIM_EMAIL` / `SIM_PASSWORD` | Credentials from `settings.json` |
| `PORT` | Listener port |
| `AUTOPILOT` | `false` logs intended commands without sending them |
| `SIGNATURE_MODE` | `observe` (log only) or `enforce` (reject bad signatures) |
| `SIGNATURE_RECIPE` | Pinned algorithm, once calibration identifies it |
| `RATE_PER_MINUTE` | Billing rate, default `1` |
| `ELECTRIC_MULTIPLIER` | Electric surcharge, default `2` |
| `ELECTRIC_SPLIT_CHARGING` | Split the 2x across `parkingCost` + `chargingCost` |

**Start with `AUTOPILOT=false`.** Every command is logged as `[dry-run]` so you
can confirm the decisions look right before the system touches the simulator.

## The signature problem

The docs describe the signature as: drop `Signature`, sort remaining field
*names* alphabetically, join their *values* with `|`, hash, compare.

**That recipe does not reproduce the signatures printed in the same document.**
Tested against the `component_broken` sample across MD5/SHA1/SHA256, four
separators, three key orderings and several secret guesses — no match. The
doc's worked example includes a `RealDateTime` field that the sample payloads
omit, so the published samples are almost certainly incomplete.

So the verifier runs in **calibration mode**: it evaluates 36 candidate recipes
against every live event and tallies which one matches.

```bash
npm start          # collect a few minutes of real traffic
npm run sig        # see which recipe wins
```

A recipe at 100% over a few hundred events is the answer — pin it:

```
SIGNATURE_RECIPE=md5:pipe:alpha
SIGNATURE_MODE=enforce
```

If nothing matches, the signature includes a field or shared secret not in the
docs. **Keep `SIGNATURE_MODE=observe` until it is resolved** — dropping real
events costs far more than accepting unverified ones.

## Billing is ambiguous — verify it early

The documentation contradicts itself:

- *"Charging cost: 1 per each minute, multiply by 2 if electric"*
- *"parking cost = total minutes spent parking, multiplied by 2 if car is electric"*

Yet the API takes `parkingCost` **and** `chargingCost` separately, and there is
a `Penalty_ChargeCarForNoElectricityUsed` for billing electricity to a car that
used none.

`computeCharge()` encodes this reading: an electric car pays `minutes` parking
plus `minutes` electricity (2x total); everything else pays `minutes` with
`chargingCost = 0`. Toggle `ELECTRIC_SPLIT_CHARGING=false` to bill the 2x
entirely as `parkingCost` instead.

Confirm against a real car in the first hour.
`Penalty_CarChargedIncorrectParkingAmount` compounds fast.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/webhook` | Simulator event intake |
| `GET` | `/health` | Counters: events, cars, penalties, fines, gaps, bad payments |
| `GET` | `/events?limit=50&class=penalty` | Raw event log |
| `GET` | `/cars` | Live car state |
| `GET` | `/components` | Mirrored component state |
| `GET` | `/penalties` | Penalty log |
| `GET` | `/signature-report` | Calibration tally |
| `POST` | `/resync` | One-shot re-sync after a crash — **never on a timer** |

## Car lifecycle

```
ARRIVED → ASSIGNED → PARKED → LEAVING → AT_EXIT → CHARGED → PAID → RELEASED → GONE
```

A spot is **reserved at assignment time**, not on `CarIn`. Two cars can arrive
back to back and the sensor confirmation for the first lands well after we must
answer the second — reserving early is what prevents
`Penalty_SendCarToOccupiedSpot`.

## Schema

| Table | Holds |
|---|---|
| `events` | Append-only raw log, `event_id` PK gives idempotency |
| `cars` | Live sessions |
| `sessions` | Completed sessions, for dashboard history |
| `components` | Mirrored spots, gates, lights, fans + usage counters |
| `zones` | CO level and danger per zone |
| `penalties` / `payments` | Scoring and fraud audit |
| `sequence_gaps` | Detected `SequenceId` discontinuities |
| `signature_trials` | Calibration tally |
| `meta` | `last_sequence_id`, etc. |

## Not built yet

- Dashboard UI (`/health`, `/cars`, `/components` return JSON ready to render)
- Admin / Operator auth and roles
- Predictive maintenance scheduler — `components.usage_count` is tracked, the
  thresholds still need measuring
- Night detection for lights
- Multi-gate routing (levels 2–3 have up to 20 gates; `DEFAULT_ENTRY_GATE` is
  a level-1 simplification)
