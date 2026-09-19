# Requirements audit - 19 September 2026

## Verdict

The incoming version covered most requested features, but did **not** fully satisfy
the reliability requirements. Its baseline suite was **135 passed, 1 failed**.
This audit fixes the reproducible defects listed below and adds regression coverage.
Software behavior is now covered by automated and browser checks. This is **not a
claim of a penalty-free live Level 2 run**: simulator billing, physical gate mapping
and component wear limits still require live validation.

Sources: the supplied `Pasted text.txt` checklist and `LEVEL 2 _ Notion.pdf` (both
pages inspected). The Level 2 PDF takes precedence, as confirmed by the user.
Simulator API, Components, Webhooks and Settings PDFs were consulted to resolve
API details. Execution directives inside the pasted attachment were not adopted:
work stayed in this workspace on `codex/level2-requirements-audit`, with no commit,
push, production database change or `.env` edit.

## Level 2 PDF coverage

| Requirement | Result and evidence |
|---|---|
| Continue operations while handling failures | Fixed recovery bugs: live occupied bays survive session restoration/expiry; dispatch retries stop when a bay is no longer reserved safely; paid vehicles resume failed release on recovery or another validated payment without another charge. Confirmed exits are excluded from the missing-exit orphan reaper. `tests/test_recovery_regressions.py`. |
| Track usage of spots, gates, lights and fans | `ParkingState.wear_snapshot()` and SQLite `component_wear`; fixed double-counted gate movements and runtime to use simulated seconds. Live dashboard now exposes all component types, cycles, runtime, wear and health. |
| Preventive maintenance before failure | 85% configured threshold, queue deduplication, occupied-bay deferral, pending-bay exclusion, fan stop-before-repair and gate-motion guard. Failed retries no longer block unrelated repairs or permanently suppress future attempts. **Conditional:** configured limits must match actual simulator limits; discovery does not supply a rated lifetime. |
| Detect, record, track and display unavailable components | Broken/maintenance state, component events, HUD alerts, usage table and `/api/broken`. Added missing light fault tracking and guards against operating unavailable fans/lights. Broken components discovered by synchronization are queued even without a new broken webhook. |
| Monitor CO and operate exhaust fans | Per-zone readings and >50/<30 hysteresis in `_handle_carbon_monoxide_event`; broken, queued and repairing fans are excluded. Repaired fans reconsider the most recent zone reading. Safe readings below the off threshold must actually arrive; no REST polling is introduced to synthesize them. |
| Estimate stays and charge vehicle types | Planned duration by default, measured fallback, explicit class normalization/multipliers, EV split, rounding and effective tariff at charge time. Existing billing tests retained. **Live EV tariff/split confirmation remains required.** |
| Store events and important activities | SQLite events, sessions, payments, penalties, rejected webhooks, login attempts, repair lifecycle and administrative audit records. Accepted webhook EventId deduplication remains durable. |
| RBAC for repairs and financial reports | Explicit four-role capability sets. Only admin holds `maint:control`; admin/auditor see financial reports; auditor alone among nonadmins may edit tariffs. Runtime route coverage, negative request pairs and HTTP/WS projection tests. |
| Record successes/failures and show last three after login | Login success displays the previous three attempts before continuing to dashboard. Fixed whitespace usernames splitting authentication from history. Tests cover canonical history and successful/failed attempts. |
| Signed webhooks only; log unsigned calls | Fixed-protocol MD5 sorted-value signature enforcement; missing/invalid signature returns 401 and stores rejection. Automated signature tests pass. Complete real simulator payloads remain an integration check. |
| Audit important changes, repairs and events | Administrative before/after audit, durable operational webhooks and component events. Repair start/failure records added. Failed user creation now rolls back instead of poisoning later SQLite transactions. |
| Penalties on dedicated page | `/penalties`, authorized financial API and reason filter already existed; retained and browser/page-route checked. |
| Dynamic daily report | Existing zone aggregation retained; report now refreshes through dashboard WebSocket ticks and a manual button. Penalties use webhook UTC receipt date consistently with recorded operational activity, with simulator-date fallback for legacy rows lacking the source event. Date basis is explicit. |
| Unregistered car parked without either sensor, then exits | Durable ghost alert, mapped exit hold, median/default estimate, staff override and validated payment before release. Existing ghost/fake-payment regressions retained. |

Lights expose on/off endpoints but **no repair endpoint** in the supplied API
document. Their usage and any reported fault are tracked and displayed, and faulted
lights are not operated. A light fault requires external attention; no fictional
repair command is sent.

## Pasted checklist coverage

| Checklist area | Implementation / verification |
|---|---|
| No simulator polling | All `list-*` calls are inside one-shot `sync_from_simulator`; startup, empty-level recovery, 401 recovery and explicit administrative sync invoke it. Dashboard requests read local state, not simulator discovery. |
| AUTOPILOT dry-run | `act()` gates mutations including queued maintenance and manual controls; dry-run regression retained. |
| Cookie auth and SessionStore | Existing SQLite cookie sessions, PBKDF2 passwords and fresh role lookup preserved. |
| Time scaling | Existing dispatch, billing delay, orphan and worker cadence scaling preserved; corrected simulator request timeout and empty-level recovery cooldown. Runtime wear now scales too. |
| Planned billing; once-only invoice | Effective tariff / `session.billable_minutes`; durable charge claim and `charge_attempted`; charge transport never automatically retries. Recovery retries departure only. |
| `.env` / dependency constraints | `.env` untouched. `.env.example` documents simulated runtime and threshold assumptions. No application Python dependency added. Installed missing pytest in the existing development virtual environment. |
| 1.1 Entrance settling | Background bounded retry at `max(0.4, 1/game_speed)`, stop on entrance departure/parking/hold/invalid reservation; neglect stored and surfaced. |
| 1.2 Cascaded entrances | `active_dispatches` short-circuit before reallocating or opening another gate. |
| 1.3 Route-crossing exits | Parking/SpotLeft evidence plus scaled physical dwell; S15/S30/S150/S151 regression cases. |
| 1.4 No second charge | Atomic SQLite claim, in-memory flag, concurrent/restart/transport-failure tests. |
| 1.5 Operator hold | Persistent close override, open clears it, automated dispatch respects hold. Fixed UI so an already closed gate can be held. |
| 1.6 Restart / 401 recovery | Token refresh and recovery callback, durable sessions, authoritative occupancy preservation, exit orphan guard and safe paid-release recovery. |
| 2.1 Signatures / fake payments | MD5 values sorted by key; invalid calls logged/401; fake payments hold vehicle and generate alerts. |
| 3.1 Day/night | ServerDateTime-based 07:00 off / 19:00 on; dynamic light IDs; unavailable lights excluded. |
| 3.2 CO hysteresis | >50 on / <30 off, independent per-zone fan mapping. |
| 3.3 Wear / 85% repair | Counters persisted; scheduling repaired as detailed above. Actual rated thresholds remain configurable assumptions. |
| 4.1 Class billing / accessible bays | Normal/Sedan 1x, SUV/Van 1.25x, configurable EV multiplier, accessible preference; existing tests. |
| 4.2 Ghost car | Hold, durable alert, historical median/default fee, authenticated override then payment. |
| 4.3 Zone throughput | Persisted `sessions.zone`; daily SQL groups by zone rather than bay-name guesses. |
| Four independent roles | Exact capability matrix retained; no ordinal role ladder. |
| Path policy and redaction | Deny unmapped paths except admin; event-class filtering, recursive signature stripping, financial keys absent from nonfinancial views. |
| Required security tests | Runtime route enumeration, matrix parametrization, negative pairs, tariff isolation, mid-session billing, immediate role change and WS filtering all run. |
| WebSockets | One snapshot per broadcast; capability projection for each fresh session; concurrent send via gather. |
| Tariff settings | SQLite seeded once from configured defaults, call-time values, validation and before/after audit. |
| Logs and sign-in | Capability-filtered pagination with LIMIT; audit target/details; login success displays previous attempts. |
| DOM safety / cleanup | Data rendered with textContent/attribute helpers; obsolete files named by checklist are absent. |
| Migration and last admin | Legacy operator migration, four seed accounts, protected last-admin deletion/demotion covered by tests. |

## Defects repaired during this pass

1. Duplicate usernames left an open transaction and broke later admin writes.
2. Whitespace around a username hid matching failed login attempts.
3. Live occupied bays could become expired reservations after recovery.
4. Retried entrance commands did not recheck bay safety.
5. Paid cars could be stranded after a failed release; concurrent recovery needed a release guard.
6. The orphan reaper archived known exit sessions still awaiting payment/staff action.
7. Gate movements were counted twice and external movement counts were not immediately persisted.
8. Fan/light runtime used wall time instead of simulated time.
9. Queued repairs did not exclude bays or gate controls, and duplicate work could be queued.
10. Preventive fan repairs did not stop the fan first.
11. Exhausted maintenance retries left proactive flags set permanently.
12. Worker backoff blocked unrelated queued repairs.
13. Already-broken components loaded during sync were never queued without another webhook.
14. Manual fan controls did not reject broken/repairing fans; lights lacked fault tracking.
15. Gate UI could not establish an operator hold on an already closed gate.
16. Daily reports were loaded once, mixed clocks for fines and clipped controls at narrow widths.
17. Simulator HTTP timeouts and empty-level recovery cooldown were not scaled.
18. Barrier repair completion did not resume paused assigned-entry or paid-exit flows.

## Verification and limits

Baseline: 136 cases, 135 passed and 1 failed. Final suite: **160 passed**, with two
dependency deprecation warnings. New regression tests were observed failing before
the corresponding backend fixes. `python -m compileall -q app tests`, all 24 Node
JavaScript syntax checks and `git diff --check` passed. Browser checks are recorded
in `ENGINEERING_LOG.md` section 4.16. The existing entrance retry fixture now creates
the actual bay reservation required by the strengthened dispatch safety guard.

Browser verification used a separate SQLite file under `tmp/`, AUTOPILOT=false,
an unreachable simulator endpoint, and the Level 2 seed. Verified login history,
component table, dynamic report/refresh, technician financial-navigation exclusion,
and narrow-width report layout. No commands were sent to the real simulator.

Before claiming scored-run compliance, validate complete signed live webhooks,
normal/EV charges, entrance/exit geometry and sustained 8x traffic, and calibrate
component wear thresholds. The energy-savings API field is only an explicitly
labeled action-count estimate, not a meter or a measured energy saving. Public
driver check-in remains a deliberate unauthenticated kiosk workflow; it does not
grant staff repair, tariff or financial permissions.
