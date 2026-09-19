# Track 2 — Requirement Checklist

Every capability the brief asks for, where it lives, and how it was checked.

**Verification legend**
- **live** — confirmed against a running `ParkingSimulator.exe`
- **offline** — confirmed against the dispatcher with synthetic webhooks
  (`scripts/replay.py`), which exercises the same handlers the simulator hits
- **code** — present and reviewed, not exercised in the last verification pass

---

## 1. Call the simulator API — login, inspect, control

- [x] **Done** — `app/client.py`

Bearer-token login, then `list-parking-spots`, `list-barriers`, `list-zones`,
`list-exhaust-fans`, `barrier-open` / `barrier-close`, `car-goto`,
`charge-car`, `repair`.

Startup calls `sync_from_simulator()` once and never polls again — the
organisers' rule is event-driven only. `/api/manual/sync` re-runs it on demand.

*Verified live* — `startup sync complete: 36 spots, 3 barriers, 1 zones, 0 fans`.
*Verified offline* — with the simulator down, every control path now fails as
`502` with the reason rather than a bare `500` (§4.11c).

## 2. Receive and handle simulator events

- [x] **Done** — `app/main.py` `/webhooks/simulator`

Handles `car_spot_action`, `payment_made`, `penalty`, `component_broken`,
`component_repaired`, `gate_action`, `carbon_monoxide_event`, `test_webhook`.

Persist-before-acknowledge; `EventId` is the SQLite primary key so redelivery
is rejected by the database rather than by memory that can age out;
`SequenceId` gaps are recorded, never swallowed; a handler that throws is
caught, recorded on the event row, and still returns `200` — a bad event must
not kill the receiver.

*Verified offline* — full replay: every class `200`, duplicate `EventId`
returned `{"status":"duplicate"}`, a 25-wide sequence gap recorded.

## 3. Allow cars to enter and guide them to a spot

- [x] **Done** — `dispatch_entry()`

Reserves the best-ranked bay, opens the entry barrier **only if it is not
already open** (per-car opening was a wasted round trip on the critical path
and burned finite gate cycles), then `car-goto`. Stale reservations expire via
`RESERVATION_TTL_S`, and a reservation is rolled back if the command is not
actually sent.

*Verified offline* — arrival → dispatch → park, with the reservation released
when the command is skipped.

## 4. Handle exits, charge correctly, then `leavepark`

- [x] **Done** — `_charge_at_exit()`, `compute_charge()`

Charges on `PlannedParkingDurationInMinutes` — the booked duration, which is
what the simulator actually bills (§4.9). Waits `EXIT_CHARGE_DELAY_S` for the
car to settle first, because the `CarIn` sensor fires before the car is ready
and the simulator rejects an early charge *while still returning 201*. Waits
for `payment_made`, validates the amount against our own figure, then releases
the car with `leavepark`. A wrong charge is re-billed from the `should be: (X)`
in the penalty text rather than left to escape.

*Verified offline* — invoice `2.00` for a 2-minute booking, payment accepted,
car released.
*Verified live* — a full run finished with **0 penalties, 0 fines**.

## 5. Website with a dashboard

- [x] **Done** — three surfaces

| Page | Purpose |
|---|---|
| `/dashboard` | Full-width operator canvas over the real simulator geometry |
| `/` | Operator HUD — live lot, manual controls |
| `/admin` | Staff console — panes gated by role |

Every page requires a staff sign-in. A driver-facing bay picker existed
(`/gate`, plus a phone pane on `/dashboard`) and was removed: letting a driver
choose their own bay competes with the router that is actually being scored,
and nothing in the brief asks for it.

## 6. Keep parking data and the dashboard up to date

- [x] **Done** — `app/ws_manager.py`

One background broadcaster pushes a snapshot per tick to every connected
client over `/ws/live` and `/ws/telemetry`; gate and spot state update from the
webhook stream, not from polling. A dead socket is dropped without disturbing
the others.

*Verified offline* — snapshot received on connect and on every tick.

## 7. Occupied/free by zone, gate status, and turn cars away when full

- [x] **Done** — `state.zone_occupancy()`, `/api/zones`

Per zone: total, available, occupied, reserved, broken, that zone's gates with
their positions, and a `full` flag. When no bay can be reserved, the car is
sent straight to `leavepark` rather than left idling at the entry to earn
`Penalty_CarLeftFromEntryBecauseNeglected`.

*Verified offline* — occupied all 30 bays, `free_spots: 0`, zone reported
`"full": true`, and the 31st car produced
`car OVR 999 -> leavepark (lot full)`.

## 8. Authentication and roles

- [x] **Done** — `app/auth.py`, four roles

| Permission | Admin | Operator | Accountant | Engineer |
|---|---|---|---|---|
| `lot.view` | yes | yes | — | yes |
| `lot.control` | yes | yes | — | — |
| `maintenance` | yes | — | — | yes |
| `finance.view` | yes | — | yes | — |
| `diagnostics.view` | yes | — | — | yes |
| `history.view` | yes | yes | yes | yes |
| `staff.manage` | yes | — | — | — |

Endpoints ask for a **permission**, never a role, so adding a role for level 2
is one row in `ROLE_PERMISSIONS` with no endpoint changes. Admin is defined as
`ALL_PERMISSIONS`, so it picks up anything added later automatically.

The only unauthenticated surfaces are `/healthz` and `/webhooks/simulator` —
the simulator has no cookie to send us.

*Verified offline* — the full access matrix (12 read endpoints × 4 roles, plus
the control endpoints) behaves exactly as the table says; the live WebSocket
refuses an anonymous client with `1008` and filters the snapshot per role, so
a broad endpoint cannot be used to read around a narrow one.

## 9. Log arrivals, parking time, departure, charges — searchable

- [x] **Done** — `app/db.py`, `/api/history`

`sessions` records plate, car type, spot, both gates, arrival/park/leave
stamps, measured and planned minutes, parking and charging cost, amount paid
and whether it validated. Indexed on `plate` and `completed_at`; plate search
is a `LIKE` against the indexed column with a capped `LIMIT`.

`/api/finance` aggregates it for the Accountant: collected vs invoiced, the
shortfall between them, rejected payments, fines by reason, and every unpaid
session.

*Verified offline* — search returns `200`; finance figures reconcile
(revenue 158.00 = invoiced 158.00, shortfall 0.00, fines 10.00, net 148.00).

## 10. Presentation for the final judging stage

- [ ] **Not started** — your call

`docs/ENGINEERING_LOG.md` §9 has the talking points: treating the docs as a
hypothesis, building for failure modes, letting live evidence settle
ambiguities, and verifying every fix.

---

## Before a scored run

- [ ] `SEED_FROM_LEVEL=` (empty) — driving a real simulator from a fake layout
      is worse than not running at all
- [ ] `AUTOPILOT=true`
- [ ] Change the four default staff passwords in `.env`
- [ ] Confirm `settings.json` `WebhookUrl` is
      `http://127.0.0.1:8080/webhooks/simulator`
- [ ] Startup log says `startup sync complete`, **not** `running on SEEDED layout`
