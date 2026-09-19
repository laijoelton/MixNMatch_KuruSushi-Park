# Dashboard Redesign — Design Spec

Date: 2026-09-19 · Owner: dashboard (Jun + Claude) · Status: approved direction, pending spec review

## 1. Goal

Replace the current operator HUD with a production-grade, level-agnostic
operations console that satisfies every Level 1 dashboard requirement and
extends to Levels 2–3 without restructuring.

Judging (Participant Handbook) rewards correctness, edge-case handling,
simplicity and explainable code over feature count. Every design choice below
is filtered through that.

## 2. Constraints

- **Do not touch simulator logic.** Dispatch, billing, webhook handling,
  `state.py`, `db.py`, `client.py` stay Sebastian's. Dashboard code lives in
  new files; `app/main.py` only gets dashboard-owned hooks (install auth,
  include router, point `/api/layout` at level detection, redirect the
  superseded `/dashboard` page to `/`).
- **One operator dashboard.** `/` replaces both the old HUD and Joelton's
  `/dashboard`; `/gate` stays as the driver kiosk. Superseded JS/CSS/templates
  are removed (history keeps them).
- **Stack stays**: FastAPI + Jinja templates + vanilla JS (ES modules, no
  build step). CDN libraries only if they earn their place.
- **No new Python dependencies.** Password hashing via `hashlib.pbkdf2_hmac`.
- **Every line explainable.** Small modules, no clever abstractions.
- **Do not commit or push.** Work stays in the working tree for review.
- UI language: English (judges, existing codebase).
- Product name in the UI: **ParkGuardian** (from the team's concept art),
  one constant, easy to change.

## 3. Level 1 requirements covered

| Requirement (LEVEL 1 brief) | Where |
|---|---|
| Auth + Admin / Operator roles | §5 Auth, `/login`, role-gated UI and API |
| Operator manually controls components, sees gates/spots | Twin → detail drawer with Open/Close/Repair |
| Occupied/free by zone + gate status; lot-full handling | Zone panel, gate list, FULL banner |
| Log arrivals/parking/departure/charges, show and search efficiently | `/history` with server-side filters + pagination |
| Keep data and dashboard up to date | Existing `/ws/live` snapshot, keyed DOM updates |

## 4. Architecture

```
app/auth.py            users + sessions tables, password hashing,
                       path-based access middleware, audit log
app/dashboard_api.py   APIRouter: pages (/login /history /payments /admin),
                       /api/me, /api/auth/*, /api/layout, /api/history*,
                       /api/stats, /api/admin/*
app/layout.py          (Joelton's, merged) extended: lists, fans/lights,
                       level auto-detect (`current_layout()`)
app/main.py            minimal: auth.install(app); include_router(...);
                       /api/layout -> current_layout(); /dashboard -> 302 /

templates/base.html    app shell: sidebar nav, top bar, toast host, drawer host
templates/{login,live,history,payments,admin,gate}.html
                       (index.html is replaced by live.html content)
static/css/app.css     design tokens + components (replaces dashboard.css)
static/js/core/        api.js  live.js  dom.js  toast.js  drawer.js
                       format.js  palette.js
static/js/components/  twin.js  zones.js  alerts.js  vehicles.js  feed.js  kpis.js
static/js/pages/       live.js  history.js  payments.js  admin.js  login.js  gate.js
```

Data flow is unchanged: simulator → webhook → `state` → `/ws/live` snapshot
(1 s) → browser. The dashboard additionally reads its own REST endpoints
(`/api/stats` every 5 s, `/api/history` on demand). Polling *our own* API is
fine; the no-polling rule applies only to the simulator's `list-*` calls.

## 5. Auth and roles

- **Storage**: `dashboard_users(id, username UNIQUE, pw_hash, salt, role, created_at)`,
  `dashboard_sessions(token PK, user_id, expires_at)`,
  `audit_log(id, at, username, method, path, status)` — in the same SQLite
  file (`settings.database_path`), created by `auth.py` on its own connection.
- **Hashing**: PBKDF2-HMAC-SHA256, 200k iterations, per-user random salt,
  constant-time compare.
- **Session**: random 32-byte token in an `HttpOnly; SameSite=Lax` cookie,
  8 h expiry, server-side row so logout truly revokes.
- **Seed users** on first start: `admin` / `operator`, passwords from
  `DASHBOARD_ADMIN_PASSWORD` / `DASHBOARD_OPERATOR_PASSWORD`
  (defaults `admin123` / `operator123`, documented in README).
- **Enforcement is server-side**, one policy table in `auth.py`:

| Path | Access |
|---|---|
| `/webhooks/simulator`, `/healthz`, `/static/*`, `/login`, `/api/auth/login` | public |
| `/gate`, `/api/gate/checkin`, `/api/gate/bays` | public (driver kiosk, by design; the kiosk reads bays over REST because `/ws/live` requires sign-in) |
| `/api/manual/sync`, `/api/manual/arrival`, `/admin`, `/api/admin/*`, `/payments`, `/api/payments` | admin |
| every other page, `/api/*` (incl. `/api/stats`, finance fields stripped for operators), `/ws/live` | operator or admin |

  Unauthenticated page request → 302 `/login?next=…`; unauthenticated API →
  401; wrong role → 403. The UI hides what a role cannot do, but the server
  is the authority.
- **Audit**: every authenticated non-GET request is recorded (who, what,
  result). Admin can read it.

| Capability | Operator | Admin |
|---|---|---|
| Live twin, zones, gates, alerts, vehicles, feed | ✅ | ✅ |
| Open/close barriers, queue repairs | ✅ | ✅ |
| History search | ✅ | ✅ |
| Revenue / P&L, payments & fraud view | — | ✅ |
| Manual sync, simulate arrival (debug) | — | ✅ |
| User management, audit log | — | ✅ |

## 6. Dashboard API

| Endpoint | Purpose |
|---|---|
| `POST /api/auth/login` `{username,password}` | sets cookie, returns `{username, role}` |
| `POST /api/auth/logout` | revokes session |
| `GET /api/me` | current user |
| `GET /api/layout` | detected level + world-coordinate geometry (§8); existing route in `main.py`, now backed by `app/layout.py::current_layout()` |
| `GET /api/history/search?plate=&spot=&status=&from=&to=&page=&size=` | filtered, paginated `sessions`; returns `{items,total,page,size}` |
| `GET /api/history/timeline?plate=` | that plate's raw events (`json_extract` on `events.payload`), ordered by `sequence_id` |
| `GET /api/stats` | counters + revenue (valid payments), fines by reason, suspect payments; finance fields only for admin |
| `GET/POST/DELETE /api/admin/users` | list / create / delete (cannot delete self or last admin) |
| `GET /api/admin/audit` | recent audit rows |

All SQL is parameterised. `size` is clamped (1–100). Filters use existing
indexes (`plate`, `completed_at`).

## 7. UI design

References: Samsara (map + click-to-detail drawer, alerts inbox), ParkHelp
(zone views, act on any element from the map), Grafana/Datadog (uniform panel
anatomy, colour only for state), Linear/Vercel (slim nav, restrained palette,
Ctrl+K), Stripe (chip filters, row → detail drawer, designed empty states).

**Shell**: slim left nav (Live · History · Payments* · Admin*), top bar with
connection state, AUTOPILOT pill, detected level, Ctrl+K search, user menu.
(*admin only)

**Live page**
```
KPI strip: Free x/y (sparkline) · On site · Revenue* · Fines
┌ Twin (real layout, zoom/pan, zone focus) ┐┌ Needs attention (n) ┐
│ click spot/gate/fan → right drawer       ││ Vehicles on site     │
└──────────────────────────────────────────┘│ Event feed [filter]  │
Zones: occupancy bar + per-type free (Any/EV/Acc) · CO · gates by zone
```
- **Drawer** shows status, occupant, zone, type, and role-permitted actions.
  Actions show a pending state ("Opening…") and resolve via toast.
- **Needs attention** is derived client-side: broken/maintenance components,
  deferred repairs, suspect payments, CO Mid+, lot/zone full, autopilot off,
  sequence gaps. Each item links to its element.
- **Event feed** humanises activity entries (icon + category), filterable.

**Visual system**: dark control-room theme; neutral surfaces, one accent;
state colours fixed everywhere — free `green`, occupied `slate`, reserved
`amber`, broken/maintenance `red`, EV `⚡`, accessible `♿`; tabular numerals;
8 px spacing grid; focus-visible outlines; works down to 1280 px (gate kiosk
down to 360 px).

**Other pages**: History (chip filters, table, pagination, row → timeline
drawer), Payments (valid vs suspect, reason, expected vs paid), Admin (users,
audit log, debug tools behind confirm dialogs), Login, Gate kiosk (natural
spot order, calmer palette, EV/♿ marked, type-compatible spots only).

## 8. Multi-level support

Level data (from the level files): L1 30 bays/1 zone; L2 90 bays/3 closed
zones/EV+accessible/12 fans/30 lights; L3 250 bays/7 zones (3 closed, 4 open)/
8 entries/10 exits/20 gates.

- **Nothing hard-coded** — zones, gates, entries, types all come from data.
- `/api/layout` picks the level file whose spot names best match the live
  `state.spots` (≥ 80 % overlap), returns zones (rect), spots (x, y,
  rotation), gates, fans, lights, entry/exit points. No match → `level:
  null` and the twin falls back to an auto grid grouped by zone.
- Twin renders SVG in world coordinates via `viewBox`: stalls drawn at a
  uniform 80×170 footprint (the level files' width/height are inconsistent),
  rotated by `Rotation` (radians). Wheel/drag zoom-pan, "fit", and click-a-
  zone to focus — required for L3's 250 bays.
- Fans, lights and CO appear only when the level has them.

## 9. Edge cases and degraded states

| Situation | Behaviour |
|---|---|
| WebSocket drops | top bar "Reconnecting…", data dimmed with "last update Ns ago", exponential backoff |
| API error / timeout | toast with message; control returns to idle; no silent failure |
| 401 mid-session | redirect to `/login?next=` |
| 403 | toast "Requires admin" |
| Empty data | designed empty states (no cars, no history, no alerts) |
| Layout unmatched | auto grid fallback + notice |
| Autopilot off | persistent amber banner "Dry-run: no commands sent" |
| Lot / zone full | red FULL banner and zone badge |
| Hostile text (plates, reasons) | only `textContent`, never `innerHTML` with data |
| Duplicate component names (L3 `gate7`) | rendered by index, flagged in attention list |
| Rapid 1 s updates | keyed DOM updates — scroll, hover and selection survive |

## 10. Testing

- `pytest` + FastAPI `TestClient` for `auth.py` and `dashboard_api.py`:
  hashing, login/logout, role matrix (401/403/200 per path), history filters
  and pagination, layout detection per level, last-admin protection.
- Manual/visual: offline run (`SEED_FROM_LEVEL`), `scripts/replay.py` to
  generate traffic, Playwright screenshots at 1440 px and 390 px; repeat with
  `lvl2`/`lvl3` seeds to prove the twin scales.

## 11. Out of scope

AI copilot, replay scrubber, predictive maintenance, green-mode automation,
QR tickets, runtime autopilot toggle (config is frozen — display only).

## 12. Backend issues noticed (for Sebastian, not fixed here)

1. `app/main.py:633-634` logs `minutes/parking_cost/charging_cost` which no
   longer exist in that scope → `NameError` on every ExitSpot/CarIn (the
   charge task is already scheduled, so billing still runs, but every exit
   event is recorded as a handler error).
2. Re-charging after 25 s without payment risks
   `Penalty_ChargeCarForParkingTwice`, e.g. when the car's payment was fake.
3. Single entry assumed (`ENTRY_GATE=gateA`); L2 has 3, L3 has 8.
4. Accessible cars are routed to `Any` spots.
5. L3 contains two barriers named `gate7`; name-keyed state collapses them.
6. Lights are never controlled. Fans switch on at CO ≥ 50 and off below it,
   but CO events are only sent at Mid and above, so the "below 50" event may
   never arrive and fans can stay on indefinitely.
7. The entry barrier is opened without checking broken/maintenance →
   `Penalty_OperateElementWhileUnderRepair`.
