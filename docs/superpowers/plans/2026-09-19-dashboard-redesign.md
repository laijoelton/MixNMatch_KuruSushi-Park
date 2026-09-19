# Dashboard Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One role-aware, level-agnostic operator console (`/`) plus History, Payments, Admin, Login and a refreshed Gate kiosk, backed by auth and dashboard APIs, satisfying every Level 1 dashboard requirement.

**Architecture:** Two new backend modules (`app/auth.py` — users, sessions, path policy middleware, audit; `app/dashboard_api.py` — pages + dashboard JSON APIs) plus an extended `app/layout.py` (level auto-detect). Frontend is Jinja templates sharing `base.html`, one stylesheet, and vanilla ES modules split into `core/`, `components/`, `pages/`. Live data still arrives via the existing `/ws/live` snapshot.

**Tech Stack:** Python 3.11+, FastAPI/Starlette, sqlite3 (stdlib), Jinja2, vanilla JS ES modules, SVG, pytest + FastAPI TestClient, Playwright (verification only).

**Spec:** `docs/superpowers/specs/2026-09-19-dashboard-redesign-design.md`

## Global Constraints

- Do not modify simulator logic: `app/state.py`, `app/db.py`, `app/client.py`, webhook handlers and dispatch/billing in `app/main.py`.
- `app/main.py` changes limited to: `auth.install(app)`, `app.include_router(dashboard_api.router)`, `/api/layout` → `current_layout()`, `/dashboard` → redirect `/`.
- No new Python dependencies. Password hashing: PBKDF2-HMAC-SHA256, 200 000 iterations, 16-byte random salt.
- Session cookie `pg_session`: `HttpOnly`, `SameSite=Lax`, path `/`, 8 h TTL, server-side row.
- Seed users `admin`/`operator`; passwords from `DASHBOARD_ADMIN_PASSWORD` / `DASHBOARD_OPERATOR_PASSWORD`, defaults `admin123` / `operator123`.
- Data text reaches the DOM only through `textContent` / attribute setters — never `innerHTML` with data.
- Product name constant: `ParkGuardian`. UI language: English.
- **Do not commit or push.** Every "commit" step in the standard template is replaced by "leave changes in the working tree".
- Nothing level-specific hard-coded in the frontend (no `ZONE1`, `gateA`, `ENTRY1`).

## File Structure

| File | Responsibility |
|---|---|
| `app/auth.py` (new) | password hashing, `dashboard_users`/`dashboard_sessions`/`audit_log` tables, seed users, `required_role(path)`, `AuthMiddleware`, `install(app)` |
| `app/dashboard_api.py` (new) | page routes `/login /history /payments /admin`; `/api/auth/*`, `/api/me`, `/api/history/search`, `/api/history/timeline`, `/api/stats`, `/api/admin/*` |
| `app/layout.py` (modify) | level files → list-based geometry incl. fans/lights/zone type; `detect_level(names)`; `current_layout()` |
| `app/main.py` (modify, 4 spots) | hooks listed in Global Constraints |
| `tests/conftest.py`, `tests/test_auth.py`, `tests/test_layout.py`, `tests/test_dashboard_api.py` (new) | backend tests |
| `templates/base.html` (new) | shell: sidebar, top bar, banners, toast + drawer hosts |
| `templates/index.html` (rewrite) | Live page |
| `templates/{login,history,payments,admin}.html` (new), `templates/gate.html` (rewrite) | other pages |
| `static/css/app.css` (new) | tokens + all components |
| `static/js/core/{dom,api,live,toast,drawer,format,palette,shell}.js` (new) | shared runtime |
| `static/js/components/{twin,zones,alerts,vehicles,feed,kpis}.js` (new) | Live page widgets |
| `static/js/pages/{live,login,history,payments,admin,gate}.js` (new) | page entry points |
| removed | `templates/dashboard.html`, `static/css/dashboard.css`, `static/css/split_dashboard.css`, `static/js/canvas_twin.js`, `static/js/lot_picker.js`, `static/js/operator_canvas.js`, `static/js/user_waze_gps.js` |

---

### Task 1: Auth core (hashing, users, sessions, audit)

**Files:**
- Create: `app/auth.py`
- Create: `tests/conftest.py`, `tests/test_auth.py`

**Interfaces:**
- Produces: `hash_password(pw, salt_hex=None) -> (hash_hex, salt_hex)`, `verify_password(pw, hash_hex, salt_hex) -> bool`, `create_user(username, password, role) -> dict`, `list_users() -> list[dict]`, `delete_user(user_id, acting_user_id) -> None` (raises `ValueError`), `authenticate(username, password) -> dict|None`, `create_session(user_id) -> str`, `user_for_token(token) -> dict|None`, `revoke_session(token) -> None`, `record_audit(username, method, path, status) -> None`, `recent_audit(limit) -> list[dict]`, `ensure_schema_and_seed() -> None`. User dicts: `{"id","username","role","created_at"}`.

- [ ] **Step 1: Write `tests/conftest.py`** (isolated DB before any app import)

```python
"""Test setup: point the app at a throwaway database before anything imports it."""
import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="pg-tests-"))
os.environ["DATABASE_PATH"] = str(_TMP / "test.db")
os.environ["AUTOPILOT"] = "false"
os.environ["SEED_FROM_LEVEL"] = ""
os.environ["DASHBOARD_ADMIN_PASSWORD"] = "admin123"
os.environ["DASHBOARD_OPERATOR_PASSWORD"] = "operator123"
```

- [ ] **Step 2: Write failing tests `tests/test_auth.py`**

```python
import pytest
from app import auth


def setup_module():
    auth.ensure_schema_and_seed()


def test_hash_roundtrip_and_salt_differs():
    h1, s1 = auth.hash_password("secret")
    h2, s2 = auth.hash_password("secret")
    assert s1 != s2 and h1 != h2
    assert auth.verify_password("secret", h1, s1)
    assert not auth.verify_password("wrong", h1, s1)


def test_seed_users_exist_and_authenticate():
    assert auth.authenticate("admin", "admin123")["role"] == "admin"
    assert auth.authenticate("operator", "operator123")["role"] == "operator"
    assert auth.authenticate("admin", "nope") is None
    assert auth.authenticate("ghost", "x") is None


def test_session_lifecycle():
    user = auth.authenticate("operator", "operator123")
    token = auth.create_session(user["id"])
    assert auth.user_for_token(token)["username"] == "operator"
    auth.revoke_session(token)
    assert auth.user_for_token(token) is None
    assert auth.user_for_token(None) is None
    assert auth.user_for_token("garbage") is None


def test_create_and_delete_user_rules():
    admin = auth.authenticate("admin", "admin123")
    u = auth.create_user("night-shift", "pw12345", "operator")
    assert u["role"] == "operator"
    with pytest.raises(ValueError):
        auth.create_user("night-shift", "pw12345", "operator")   # duplicate
    with pytest.raises(ValueError):
        auth.create_user("x", "pw12345", "superuser")             # bad role
    with pytest.raises(ValueError):
        auth.delete_user(admin["id"], acting_user_id=admin["id"])  # self
    auth.delete_user(u["id"], acting_user_id=admin["id"])
    assert all(x["username"] != "night-shift" for x in auth.list_users())


def test_cannot_delete_last_admin():
    admin = auth.authenticate("admin", "admin123")
    op = auth.authenticate("operator", "operator123")
    with pytest.raises(ValueError):
        auth.delete_user(admin["id"], acting_user_id=op["id"])


def test_audit_roundtrip():
    auth.record_audit("admin", "POST", "/api/manual/sync", 200)
    row = auth.recent_audit(1)[0]
    assert row["path"] == "/api/manual/sync" and row["status"] == 200
```

- [ ] **Step 3: Run — expect FAIL** (`ModuleNotFoundError: app.auth`)

Run: `python -m pytest tests/test_auth.py -q`

- [ ] **Step 4: Implement `app/auth.py` (core part)**

```python
"""Dashboard authentication: users, sessions, role policy and audit log.

Separate from the simulator login in ``app/client.py`` - this is who may use
*our* dashboard. Everything is stdlib: PBKDF2 for passwords, random tokens for
sessions, and the same SQLite file the rest of the service uses.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import threading
import time
from typing import Any, Optional

from app import db  # noqa: F401 - importing db creates the data directory
from app.config import settings

log = logging.getLogger("dashboard.auth")

COOKIE_NAME = "pg_session"
SESSION_TTL_S = 8 * 3600
PBKDF2_ITERATIONS = 200_000
ROLES = ("operator", "admin")

_conn = sqlite3.connect(settings.database_path, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_lock = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dashboard_users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT UNIQUE NOT NULL,
    pw_hash    TEXT NOT NULL,
    salt       TEXT NOT NULL,
    role       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dashboard_sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       REAL NOT NULL,
    username TEXT,
    method   TEXT,
    path     TEXT,
    status   INTEGER
);
"""

_PUBLIC_USER_FIELDS = "id, username, role, created_at"


def hash_password(password: str, salt_hex: Optional[str] = None) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return digest.hex(), salt.hex()


def verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    candidate, _ = hash_password(password, salt_hex)
    return hmac.compare_digest(candidate, hash_hex)


def _one(sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    with _lock:
        row = _conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _all(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


def _write(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur


def create_user(username: str, password: str, role: str) -> dict[str, Any]:
    username = username.strip()
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if not username or len(password) < 6:
        raise ValueError("username required and password must be at least 6 characters")
    pw_hash, salt = hash_password(password)
    try:
        cur = _write(
            "INSERT INTO dashboard_users (username, pw_hash, salt, role, created_at) VALUES (?,?,?,?,?)",
            (username, pw_hash, salt, role, time.time()))
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"user {username!r} already exists") from exc
    return _one(f"SELECT {_PUBLIC_USER_FIELDS} FROM dashboard_users WHERE id = ?", (cur.lastrowid,))


def list_users() -> list[dict[str, Any]]:
    return _all(f"SELECT {_PUBLIC_USER_FIELDS} FROM dashboard_users ORDER BY id")


def delete_user(user_id: int, acting_user_id: int) -> None:
    if user_id == acting_user_id:
        raise ValueError("you cannot delete your own account")
    target = _one("SELECT role FROM dashboard_users WHERE id = ?", (user_id,))
    if target is None:
        raise ValueError("no such user")
    if target["role"] == "admin":
        admins = _one("SELECT COUNT(*) AS n FROM dashboard_users WHERE role = 'admin'")["n"]
        if admins <= 1:
            raise ValueError("cannot delete the last admin")
    _write("DELETE FROM dashboard_sessions WHERE user_id = ?", (user_id,))
    _write("DELETE FROM dashboard_users WHERE id = ?", (user_id,))


def authenticate(username: str, password: str) -> Optional[dict[str, Any]]:
    row = _one("SELECT * FROM dashboard_users WHERE username = ?", (username.strip(),))
    if row is None:
        hash_password(password)  # same cost either way, so timing does not reveal valid usernames
        return None
    if not verify_password(password, row["pw_hash"], row["salt"]):
        return None
    return {k: row[k] for k in ("id", "username", "role", "created_at")}


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    _write("INSERT INTO dashboard_sessions (token, user_id, expires_at) VALUES (?,?,?)",
           (token, user_id, time.time() + SESSION_TTL_S))
    return token


def user_for_token(token: Optional[str]) -> Optional[dict[str, Any]]:
    if not token:
        return None
    row = _one(
        f"SELECT s.expires_at, u.id, u.username, u.role, u.created_at "
        f"FROM dashboard_sessions s JOIN dashboard_users u ON u.id = s.user_id WHERE s.token = ?",
        (token,))
    if row is None:
        return None
    if row["expires_at"] < time.time():
        revoke_session(token)
        return None
    return {k: row[k] for k in ("id", "username", "role", "created_at")}


def revoke_session(token: Optional[str]) -> None:
    if token:
        _write("DELETE FROM dashboard_sessions WHERE token = ?", (token,))


def record_audit(username: str, method: str, path: str, status: Optional[int]) -> None:
    _write("INSERT INTO audit_log (at, username, method, path, status) VALUES (?,?,?,?,?)",
           (time.time(), username, method, path, status))


def recent_audit(limit: int = 100) -> list[dict[str, Any]]:
    return _all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))


def ensure_schema_and_seed() -> None:
    with _lock:
        _conn.executescript(_SCHEMA)
        _conn.commit()
    if _one("SELECT COUNT(*) AS n FROM dashboard_users")["n"] == 0:
        create_user("admin", os.environ.get("DASHBOARD_ADMIN_PASSWORD", "admin123"), "admin")
        create_user("operator", os.environ.get("DASHBOARD_OPERATOR_PASSWORD", "operator123"), "operator")
        log.info("dashboard: seeded default admin and operator accounts")
```

- [ ] **Step 5: Run — expect PASS** — `python -m pytest tests/test_auth.py -q`
- [ ] **Step 6: Leave changes uncommitted.**

---

### Task 2: Access policy middleware + wiring into `main.py`

**Files:**
- Modify: `app/auth.py` (append policy + middleware + `install`)
- Modify: `app/main.py` (after `app = FastAPI(...)` block: `auth.install(app)`)
- Create: `tests/test_access.py`

**Interfaces:**
- Consumes: Task 1 functions.
- Produces: `required_role(path) -> "public"|"operator"|"admin"`, `AuthMiddleware`, `install(app)`. Downstream handlers read the user via `request.state.user` (dict or `None`).

- [ ] **Step 1: Failing tests `tests/test_access.py`**

```python
from fastapi.testclient import TestClient

from app import auth
from app.main import app


def _client(user=None, pw=None):
    c = TestClient(app, follow_redirects=False)
    if user:
        r = c.post("/api/auth/login", json={"username": user, "password": pw})
        assert r.status_code == 200, r.text
    return c


def test_policy_table():
    assert auth.required_role("/webhooks/simulator") == "public"
    assert auth.required_role("/static/css/app.css") == "public"
    assert auth.required_role("/gate") == "public"
    assert auth.required_role("/api/state") == "operator"
    assert auth.required_role("/api/manual/barrier/g1/open") == "operator"
    assert auth.required_role("/api/manual/sync") == "admin"
    assert auth.required_role("/api/admin/users") == "admin"


def test_anonymous_is_blocked():
    c = _client()
    assert c.get("/api/state").status_code == 401
    page = c.get("/")
    assert page.status_code == 302 and page.headers["location"].startswith("/login")
    assert c.get("/healthz").status_code == 200


def test_operator_vs_admin():
    op = _client("operator", "operator123")
    assert op.get("/api/state").status_code == 200
    assert op.post("/api/manual/sync").status_code == 403
    assert op.get("/api/admin/users").status_code == 403
    admin = _client("admin", "admin123")
    assert admin.get("/api/admin/users").status_code == 200


def test_mutations_are_audited():
    admin = _client("admin", "admin123")
    admin.post("/api/auth/logout")
    assert auth.recent_audit(1)[0]["path"] == "/api/auth/logout"
```

(`/api/auth/login` and `/api/admin/users` arrive in Task 4; these tests go green at the end of Task 4. Run `test_policy_table` and `test_anonymous_is_blocked` now.)

- [ ] **Step 2: Run** `python -m pytest tests/test_access.py -q -k "policy or anonymous"` — expect FAIL.

- [ ] **Step 3: Append to `app/auth.py`**

```python
# --------------------------------------------------------------------------- #
# Access policy - the single place that decides who may call what
# --------------------------------------------------------------------------- #
from http.cookies import SimpleCookie  # noqa: E402

from starlette.responses import JSONResponse, RedirectResponse  # noqa: E402

_PUBLIC_EXACT = {"/login", "/api/auth/login", "/healthz", "/webhooks/simulator",
                 "/gate", "/api/gate/checkin", "/favicon.ico"}
_PUBLIC_PREFIX = ("/static/",)
_ADMIN_EXACT = {"/admin", "/payments", "/api/payments", "/api/manual/sync",
                "/api/manual/arrival", "/api/dispatch", "/api/signature-report"}
_ADMIN_PREFIX = ("/api/admin/",)


def required_role(path: str) -> str:
    if path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIX):
        return "public"
    if path in _ADMIN_EXACT or path.startswith(_ADMIN_PREFIX):
        return "admin"
    return "operator"


def _cookie(scope: dict, name: str) -> Optional[str]:
    for key, value in scope.get("headers", []):
        if key == b"cookie":
            jar = SimpleCookie()
            jar.load(value.decode("latin-1"))
            if name in jar:
                return jar[name].value
    return None


class AuthMiddleware:
    """Pure ASGI middleware so it covers HTTP *and* the /ws/live WebSocket."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        user = user_for_token(_cookie(scope, COOKIE_NAME))
        scope.setdefault("state", {})["user"] = user

        need = required_role(path)
        denied = None
        if need != "public":
            if user is None:
                denied = 401
            elif need == "admin" and user["role"] != "admin":
                denied = 403

        if denied and scope["type"] == "websocket":
            await receive()  # the websocket.connect message
            await send({"type": "websocket.close", "code": 4401 if denied == 401 else 4403})
            return
        if denied:
            is_page = not path.startswith("/api/") and scope.get("method") == "GET"
            if is_page and denied == 401:
                response = RedirectResponse(f"/login?next={path}", status_code=302)
            elif is_page:
                response = RedirectResponse("/?denied=1", status_code=302)
            else:
                detail = "Sign in required" if denied == 401 else "Requires admin role"
                response = JSONResponse({"detail": detail}, status_code=denied)
            await response(scope, receive, send)
            return

        if scope["type"] == "http" and user and scope["method"] not in ("GET", "HEAD", "OPTIONS"):
            status: dict[str, int] = {}

            async def send_and_capture(message):
                if message["type"] == "http.response.start":
                    status["code"] = message["status"]
                await send(message)

            await self.app(scope, receive, send_and_capture)
            record_audit(user["username"], scope["method"], path, status.get("code"))
            return

        await self.app(scope, receive, send)


def install(app) -> None:
    ensure_schema_and_seed()
    app.add_middleware(AuthMiddleware)
```

- [ ] **Step 4: Wire into `app/main.py`** — add import `from app import auth` next to `from app import db`, and directly after the `app = FastAPI(...)` call:

```python
auth.install(app)
```

- [ ] **Step 5: Run** `python -m pytest tests/test_access.py -q -k "policy or anonymous"` — expect PASS.
- [ ] **Step 6: Leave uncommitted.**

---

### Task 3: Level-aware layout

**Files:**
- Modify: `app/layout.py` (rewrite body; keep module purpose/docstring)
- Modify: `app/main.py` — `/api/layout` returns `current_layout()`
- Create: `tests/test_layout.py`

**Interfaces:**
- Produces: `load_layout(level) -> dict` returning `{"level", "zones":[{name,x,y,w,h,type}], "spots":[{name,x,y,rotation,purpose,car_type,zone}], "gates":[{name,x,y,rotation,zone}], "fans":[{name,x,y,zone}], "lights":[{name,x,y,zone,group}], "bounds":{min_x,max_x,min_y,max_y}}` (bounds over zones + Park spots + entry/exit spots; escape points excluded); `detect_level(names) -> str|None` (Jaccard ≥ 0.8); `current_layout() -> dict` (detected level, else `SEED_FROM_LEVEL`, else `{"level": None, ...empty lists, "bounds": None}`).

- [ ] **Step 1: Failing tests `tests/test_layout.py`**

```python
from app import layout


def _names(level):
    return [s["name"] for s in layout.load_layout(level)["spots"]]


def test_each_level_detects_itself():
    for level in ("lvl1", "lvl2", "lvl3"):
        assert layout.detect_level(_names(level)) == level


def test_superset_does_not_fool_detection():
    # lvl1's names are a subset of lvl2's; Jaccard keeps them apart.
    assert layout.detect_level(_names("lvl1")) == "lvl1"


def test_unknown_names_detect_nothing():
    assert layout.detect_level(["X1", "X2"]) is None
    assert layout.detect_level([]) is None


def test_geometry_shape():
    lv2 = layout.load_layout("lvl2")
    assert len([s for s in lv2["spots"] if s["purpose"] == "Park"]) == 90
    assert {s["car_type"] for s in lv2["spots"]} >= {"Any", "Electric", "Accessible"}
    assert len(lv2["fans"]) == 12 and len(lv2["zones"]) == 3
    assert all(z["type"] == "Closed" for z in lv2["zones"])


def test_duplicate_gate_names_survive():
    names = [g["name"] for g in layout.load_layout("lvl3")["gates"]]
    assert names.count("gate7") == 2
```

- [ ] **Step 2: Run** `python -m pytest tests/test_layout.py -q` — expect FAIL.

- [ ] **Step 3: Rewrite `app/layout.py`**

```python
"""Real simulator geometry for the operator twin, with level auto-detection.

The REST API exposes only names. The simulator's own level files
(``settings/lvl1.json`` ...) hold the X/Y of every spot, gate, zone, fan and
light. This module reads them once, and works out which level is running by
comparing the live spot names from ``ParkingState`` against each file.

Geometry only - never status. Live status always comes from ParkingState.
Lists (not dicts) are used because level 3 has two gates named ``gate7``.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

from app.config import settings

log = logging.getLogger("dispatcher.layout")

LEVELS = ("lvl1", "lvl2", "lvl3")
MATCH_THRESHOLD = 0.8
_CANDIDATE_DIRS = [
    Path("ParkingSimulator-win-x64/ParkingSimulator-win-x64/settings"),
    Path("settings"),
    Path("."),
]
_EMPTY = {"level": None, "zones": [], "spots": [], "gates": [], "fans": [], "lights": [], "bounds": None}

_lock = threading.Lock()
_cache: dict[str, dict[str, Any]] = {}


def _find_level_file(level: str) -> Optional[Path]:
    for directory in _CANDIDATE_DIRS:
        candidate = directory / f"{level}.json"
        if candidate.exists():
            return candidate
    return None


def _bounds(zones: list[dict], spots: list[dict]) -> Optional[dict[str, float]]:
    xs, ys = [], []
    for z in zones:
        xs += [z["x"] - z["w"] / 2, z["x"] + z["w"] / 2]
        ys += [z["y"] - z["h"] / 2, z["y"] + z["h"] / 2]
    for s in spots:
        if s["purpose"] in ("Park", "EntrySpot", "ExitSpot"):
            xs.append(s["x"])
            ys.append(s["y"])
    if not xs:
        return None
    return {"min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)}


def load_layout(level: str) -> dict[str, Any]:
    with _lock:
        if level in _cache:
            return _cache[level]
        source = _find_level_file(level)
        if source is None:
            log.warning("layout: no %s.json found", level)
            _cache[level] = dict(_EMPTY)
            return _cache[level]
        raw = json.loads(source.read_text(encoding="utf-8-sig"))

        zones = [{"name": z["Name"], "x": float(z["X"]), "y": float(z["Y"]),
                  "w": float(z.get("Width", 0)), "h": float(z.get("Height", 0)),
                  "type": z.get("ZoneType", "Open")} for z in raw.get("Zones", [])]
        spots = [{"name": s["Name"], "x": float(s["X"]), "y": float(s["Y"]),
                  "rotation": float(s.get("Rotation", 0)), "purpose": s.get("Purpose", "Park"),
                  "car_type": s.get("CarType", "Any"), "zone": s.get("ZoneParent", "")}
                 for s in raw.get("ParkingSpots", [])]
        gates = [{"name": g["Name"], "x": float(g["X"]), "y": float(g["Y"]),
                  "rotation": float(g.get("Rotation", 0)), "zone": g.get("ZoneParent", "")}
                 for g in raw.get("Gates", [])]
        fans = [{"name": f["Name"], "x": float(f["X"]), "y": float(f["Y"]),
                 "zone": f.get("ZoneParent", "")} for f in raw.get("Exhausts", [])]
        lights = [{"name": l["Name"], "x": float(l["X"]), "y": float(l["Y"]),
                   "zone": l.get("ZoneParent", ""), "group": l.get("Group", "")}
                  for l in raw.get("Lights", [])]

        result = {"level": level, "zones": zones, "spots": spots, "gates": gates,
                  "fans": fans, "lights": lights, "bounds": _bounds(zones, spots)}
        log.info("layout: loaded %s (%d spots, %d gates, %d zones)", source, len(spots), len(gates), len(zones))
        _cache[level] = result
        return result


def detect_level(names: Iterable[str]) -> Optional[str]:
    """Level whose spot names best match ``names`` (Jaccard), if good enough."""
    live = set(names)
    if not live:
        return None
    best, best_score = None, 0.0
    for level in LEVELS:
        known = {s["name"] for s in load_layout(level)["spots"]}
        if not known:
            continue
        score = len(live & known) / len(live | known)
        if score > best_score:
            best, best_score = level, score
    return best if best_score >= MATCH_THRESHOLD else None


def current_layout() -> dict[str, Any]:
    from app.state import state  # local import: layout must not depend on state at import time
    level = detect_level(list(state.spots.keys())) or (settings.seed_from_level or None)
    return load_layout(level) if level else dict(_EMPTY)
```

- [ ] **Step 4: `app/main.py`** — change `from app.layout import load_layout` to `from app.layout import current_layout`, and the body of `get_layout` to `return current_layout()` (update its docstring: "detected level; see app/layout.py").
- [ ] **Step 5: Run** `python -m pytest tests/test_layout.py -q` — expect PASS.
- [ ] **Step 6: Leave uncommitted.**

---

### Task 4: Dashboard API + page routes

**Files:**
- Create: `app/dashboard_api.py`
- Modify: `app/main.py` — `from app import dashboard_api`; `app.include_router(dashboard_api.router)` right after `auth.install(app)`; `/dashboard` handler body → `return RedirectResponse("/", status_code=302)` (import `RedirectResponse`).
- Create: `tests/test_dashboard_api.py`

**Interfaces:**
- Consumes: `auth.*` (Tasks 1–2), `db.query` (existing, read-only).
- Produces JSON: `/api/me -> {username, role}`; `/api/history/search -> {items, total, page, size}`; `/api/history/timeline?plate= -> [{sequence_id, event_class, server_datetime, payload}]`; `/api/stats -> {completed_sessions, avg_minutes, suspect_payments, penalty_count, total_fines, sequence_gaps, [revenue, net, fines_by_reason]}`; `/api/admin/users` GET list / POST `{username,password,role}` / DELETE `/api/admin/users/{id}`; `/api/admin/audit`.
- Pages render templates with context `{"active": <nav key>, "asset_version": str}`.

- [ ] **Step 1: Failing tests `tests/test_dashboard_api.py`**

```python
from fastapi.testclient import TestClient

from app import db
from app.main import app


def _login(user, pw):
    c = TestClient(app, follow_redirects=False)
    assert c.post("/api/auth/login", json={"username": user, "password": pw}).status_code == 200
    return c


def _seed_sessions():
    for i, (plate, ok) in enumerate([("WCT 759", 1), ("ABC 123", 0), ("XYZ 999", None)]):
        db.record_session({"plate": plate, "car_type": "Normal", "spot": f"S{i+1}",
                           "entry_gate": "ENTRY1", "exit_gate": "EXIT_EXIT",
                           "arrived_at": f"2026-09-19T0{i}:00:00+00:00",
                           "parked_at": None, "left_spot_at": None, "minutes": 2.0,
                           "planned_minutes": 2.0, "parking_cost": 2.0, "charging_cost": 0.0,
                           "paid_amount": 2.0 if ok is not None else None, "payment_ok": ok,
                           "completed_at": f"2026-09-19T0{i}:30:00+00:00"})


def test_login_bad_password():
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": "admin", "password": "x"}).status_code == 401


def test_me():
    assert _login("operator", "operator123").get("/api/me").json() == {"username": "operator", "role": "operator"}


def test_history_filters_and_paging():
    _seed_sessions()
    c = _login("operator", "operator123")
    everything = c.get("/api/history/search", params={"size": 2}).json()
    assert everything["total"] >= 3 and len(everything["items"]) == 2
    assert c.get("/api/history/search", params={"plate": "wct759"}).json()["items"][0]["plate"] == "WCT 759"
    assert all(r["payment_ok"] == 0 for r in c.get("/api/history/search", params={"status": "suspect"}).json()["items"])
    assert c.get("/api/history/search", params={"size": 999}).json()["size"] == 100


def test_stats_hides_finance_from_operator():
    assert "revenue" not in _login("operator", "operator123").get("/api/stats").json()
    assert "revenue" in _login("admin", "admin123").get("/api/stats").json()


def test_admin_user_management():
    admin = _login("admin", "admin123")
    created = admin.post("/api/admin/users", json={"username": "temp", "password": "temp123", "role": "operator"})
    assert created.status_code == 201
    assert admin.post("/api/admin/users", json={"username": "temp", "password": "temp123", "role": "operator"}).status_code == 400
    assert admin.delete(f"/api/admin/users/{created.json()['id']}").status_code == 200


def test_pages_render_for_logged_in_user():
    c = _login("admin", "admin123")
    for path in ("/", "/history", "/payments", "/admin"):
        assert c.get(path).status_code == 200, path
    assert TestClient(app).get("/login").status_code == 200
```

- [ ] **Step 2: Run** `python -m pytest tests/test_dashboard_api.py -q` — expect FAIL.

- [ ] **Step 3: Implement `app/dashboard_api.py`**

```python
"""Dashboard-only HTTP surface: pages, login, history search, stats, admin.

Read-only over the simulator's data (``app.db``); never calls the simulator.
Access control is enforced by ``app.auth.AuthMiddleware`` before these run;
``request.state.user`` is the signed-in user.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import auth, db

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
_ASSET_VERSION = str(int(time.time()))


def _page(request: Request, template: str, active: str):
    return _TEMPLATES.TemplateResponse(request, template, {"active": active, "asset_version": _ASSET_VERSION})


def _user(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "Sign in required")
    return user


# ------------------------------------------------------------------ pages
@router.get("/login", include_in_schema=False)
async def login_page(request: Request):
    return _page(request, "login.html", "login")


@router.get("/history", include_in_schema=False)
async def history_page(request: Request):
    return _page(request, "history.html", "history")


@router.get("/payments", include_in_schema=False)
async def payments_page(request: Request):
    return _page(request, "payments.html", "payments")


@router.get("/admin", include_in_schema=False)
async def admin_page(request: Request):
    return _page(request, "admin.html", "admin")


# ------------------------------------------------------------------ auth
class LoginIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


@router.post("/api/auth/login")
async def login(body: LoginIn, response: Response) -> dict[str, str]:
    user = auth.authenticate(body.username, body.password)
    if user is None:
        raise HTTPException(401, "Invalid username or password")
    token = auth.create_session(user["id"])
    response.set_cookie(auth.COOKIE_NAME, token, max_age=auth.SESSION_TTL_S,
                        httponly=True, samesite="lax", path="/")
    return {"username": user["username"], "role": user["role"]}


@router.post("/api/auth/logout")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    auth.revoke_session(request.cookies.get(auth.COOKIE_NAME))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/api/me")
async def me(request: Request) -> dict[str, str]:
    user = _user(request)
    return {"username": user["username"], "role": user["role"]}


# ------------------------------------------------------------------ history
_STATUS_SQL = {"paid": "payment_ok = 1", "suspect": "payment_ok = 0", "unpaid": "payment_ok IS NULL"}


@router.get("/api/history/search")
async def history_search(plate: Optional[str] = None, spot: Optional[str] = None,
                         status: Optional[str] = None, date_from: Optional[str] = Query(None, alias="from"),
                         date_to: Optional[str] = Query(None, alias="to"),
                         page: int = 1, size: int = 25) -> dict[str, Any]:
    where, params = [], []
    if plate:
        where.append("REPLACE(UPPER(plate), ' ', '') LIKE ?")
        params.append(f"%{plate.upper().replace(' ', '')}%")
    if spot:
        where.append("spot = ?")
        params.append(spot.strip())
    if status in _STATUS_SQL:
        where.append(_STATUS_SQL[status])
    if date_from:
        where.append("completed_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("completed_at <= ?")
        params.append(date_to)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    size = max(1, min(size, 100))
    page = max(1, page)
    total = db.query(f"SELECT COUNT(*) AS n FROM sessions {clause}", tuple(params))[0]["n"]
    items = db.query(f"SELECT * FROM sessions {clause} ORDER BY completed_at DESC, id DESC LIMIT ? OFFSET ?",
                     tuple(params) + (size, (page - 1) * size))
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/api/history/timeline")
async def history_timeline(plate: str = Query(..., min_length=1, max_length=16)) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT sequence_id, event_class, server_datetime, received_at, payload FROM events "
        "WHERE json_extract(payload, '$.CarPlateNumber') = ? ORDER BY sequence_id LIMIT 200",
        (plate,))
    for row in rows:
        row["payload"] = json.loads(row["payload"])
    return rows


# ------------------------------------------------------------------ stats
@router.get("/api/stats")
async def stats(request: Request) -> dict[str, Any]:
    user = _user(request)
    out = db.query("""SELECT
        (SELECT COUNT(*) FROM sessions)                       AS completed_sessions,
        (SELECT COALESCE(AVG(minutes), 0) FROM sessions)      AS avg_minutes,
        (SELECT COUNT(*) FROM payments WHERE valid = 0)       AS suspect_payments,
        (SELECT COUNT(*) FROM penalties)                      AS penalty_count,
        (SELECT COALESCE(SUM(fine_amount), 0) FROM penalties) AS total_fines,
        (SELECT COUNT(*) FROM sequence_gaps)                  AS sequence_gaps""")[0]
    if user["role"] == "admin":
        revenue = db.query("SELECT COALESCE(SUM(amount), 0) AS r FROM payments WHERE valid = 1")[0]["r"]
        out["revenue"] = round(revenue, 2)
        out["net"] = round(revenue - out["total_fines"], 2)
        out["fines_by_reason"] = db.query(
            "SELECT reason, COUNT(*) AS count, SUM(fine_amount) AS total FROM penalties "
            "GROUP BY reason ORDER BY total DESC LIMIT 10")
    return out


# ------------------------------------------------------------------ admin
class NewUserIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)
    role: str


@router.get("/api/admin/users")
async def admin_list_users() -> list[dict[str, Any]]:
    return auth.list_users()


@router.post("/api/admin/users", status_code=201)
async def admin_create_user(body: NewUserIn) -> dict[str, Any]:
    try:
        return auth.create_user(body.username, body.password, body.role)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: int, request: Request) -> dict[str, bool]:
    try:
        auth.delete_user(user_id, acting_user_id=_user(request)["id"])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@router.get("/api/admin/audit")
async def admin_audit(limit: int = 100) -> list[dict[str, Any]]:
    return auth.recent_audit(limit)
```

- [ ] **Step 4: Wire `app/main.py`** (as listed in Files). Create minimal placeholder templates `login.html`, `history.html`, `payments.html`, `admin.html` that extend `base.html` (Task 5 creates `base.html`; do Task 5 Step 1 first if running this test now).
- [ ] **Step 5: Run full suite** `python -m pytest tests -q` — expect all PASS (including `test_access.py`).
- [ ] **Step 6: Leave uncommitted.**

---

### Task 5: Frontend foundation (tokens, shell, core modules)

**Files:** Create `static/css/app.css`, `templates/base.html`, `static/js/core/{dom,api,live,toast,drawer,format,palette,shell}.js`.

**Interfaces (produced, used by every later task):**
- `dom.js`: `h(tag, attrs?, ...children) -> Element` (attrs: `class`, `dataset`, `on*` handlers, others via `setAttribute`; string children become text nodes); `keyedList(container, items, key(item), render(item) -> Element, update(el, item))` — reuses elements by key, reorders, removes stale; `clear(el)`.
- `api.js`: `api(path, {method, body, quiet}) -> Promise<json>`; on 401 → `location = /login?next=`; on non-2xx throws `Error(detail)` and toasts unless `quiet`.
- `live.js`: `subscribe(fn)`; `connectionState()`; emits `{type:"conn", state:"live"|"reconnecting"}` to `onConnection(fn)`; single WebSocket to `/ws/live`, backoff 1 s→10 s; keeps `lastSnapshot`, `lastUpdateAt`.
- `toast.js`: `toast(message, kind="info"|"ok"|"warn"|"error")`.
- `drawer.js`: `openDrawer({title, subtitle, body: Element, actions: Element[]})`, `closeDrawer()`; Esc closes; focus moves into drawer.
- `format.js`: `money(n)`, `minutes(n)`, `timeAgo(epochSec|iso)`, `clock(epochSec|iso)`, `statusLabel(spot)`.
- `palette.js`: Ctrl/⌘+K opens search over plates, spots, gates, pages; Enter → callback `onPick(item)` registered by page.
- `shell.js`: `initShell({active}) -> Promise<me>`: fetches `/api/me`, fills user menu, hides `[data-role="admin"]` for operators, wires logout, top-bar connection + autopilot pill (from `/healthz` every 10 s: `autopilot`, `signature_mode`), `?denied=1` toast.

- [ ] **Step 1: `templates/base.html`** — structure:

```html
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}ParkGuardian{% endblock %} · ParkGuardian</title>
<link rel="stylesheet" href="/static/css/app.css?v={{ asset_version }}">
</head><body data-page="{{ active|default('live') }}">
<div class="app">
  <nav class="rail" aria-label="Primary">
    <a class="brand" href="/" aria-label="ParkGuardian home">PG</a>
    <a href="/" class="rail-link {% if active|default('live')=='live' %}is-active{% endif %}">Live</a>
    <a href="/history" class="rail-link {% if active=='history' %}is-active{% endif %}">History</a>
    <a href="/payments" data-role="admin" class="rail-link {% if active=='payments' %}is-active{% endif %}">Payments</a>
    <a href="/admin" data-role="admin" class="rail-link {% if active=='admin' %}is-active{% endif %}">Admin</a>
  </nav>
  <div class="main">
    <header class="topbar">
      <div class="topbar-title">{% block heading %}{% endblock %}</div>
      <div class="topbar-status">
        <span id="conn-pill" class="pill">Connecting…</span>
        <span id="autopilot-pill" class="pill" hidden></span>
        <span id="level-pill" class="pill" hidden></span>
      </div>
      <button id="palette-btn" class="search-btn" type="button">Search <kbd>Ctrl K</kbd></button>
      <div class="user-menu"><span id="user-name"></span><span id="user-role" class="role"></span>
        <button id="logout-btn" class="btn ghost small" type="button">Sign out</button></div>
    </header>
    <div id="banners"></div>
    <main class="content">{% block content %}{% endblock %}</main>
  </div>
</div>
<aside id="drawer" class="drawer" aria-hidden="true" tabindex="-1"></aside>
<div id="toasts" class="toasts" role="status" aria-live="polite"></div>
<div id="palette" class="palette" hidden></div>
{% block scripts %}{% endblock %}
</body></html>
```

- [ ] **Step 2: `static/css/app.css`** — tokens on `:root` (dark control-room): `--bg #0b0f14`, `--surface #11161d`, `--surface-2 #161c25`, `--line #232b37`, `--text #e7ecf3`, `--muted #8b97a8`, `--accent #5b9dff`; state: `--free #3fb97f`, `--occupied #64748b`, `--reserved #e0a43a`, `--fault #e5566b`, `--ev #5b9dff`, `--acc #b58cff`; spacing `--s1..--s6` (4/8/12/16/24/32 px); radius 8 px; font stack `Inter, "Segoe UI", system-ui`; `font-variant-numeric: tabular-nums` on `.num`. Components: `.app` grid (rail 72 px + main), `.topbar`, `.pill` (+ `.is-live .is-warn .is-bad`), `.panel` (title/value/trend anatomy), `.kpi`, `.btn` (`primary ghost danger small`, `[aria-busy]` spinner), `.chip`, `.table`, `.empty`, `.banner` (`warn bad`), `.drawer` (right slide-in 380 px, `.is-open`), `.toasts`, `.palette`, `.bar` (occupancy bar), `.dot` status dots, focus-visible outline `2px solid var(--accent)`; `@media (max-width: 1280px)` stacks the Live side column below the twin.

- [ ] **Step 3: core modules.** `dom.js` (key reference implementation):

```js
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) { while (el.firstChild) el.firstChild.remove(); }

// Reuse DOM nodes across 1 s snapshots so scroll, hover and focus survive.
export function keyedList(container, items, key, render, update) {
  const existing = new Map([...container.children].filter((c) => c.dataset.key).map((c) => [c.dataset.key, c]));
  let prev = null;
  for (const item of items) {
    const k = String(key(item));
    let el = existing.get(k);
    if (el) { existing.delete(k); update?.(el, item); }
    else { el = render(item); el.dataset.key = k; }
    const next = prev ? prev.nextSibling : container.firstChild;
    if (el !== next) container.insertBefore(el, next);
    prev = el;
  }
  for (const stale of existing.values()) stale.remove();
}
```

`api.js`:

```js
import { toast } from "./toast.js";

export async function api(path, { method = "GET", body, quiet = false } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method, credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    if (!quiet) toast("Server unreachable — check the dispatcher is running", "error");
    throw new Error("network");
  }
  if (res.status === 401 && !path.startsWith("/api/auth/")) {
    location.href = `/login?next=${encodeURIComponent(location.pathname)}`;
    throw new Error("unauthenticated");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = typeof data.detail === "string" ? data.detail : `Request failed (${res.status})`;
    if (!quiet) toast(msg, res.status === 403 ? "warn" : "error");
    throw new Error(msg);
  }
  return data;
}
```

`live.js`: one socket; on `message` parse, store, call subscribers inside `try/catch` (one bad widget must not stop the others); on `close` → state `reconnecting`, retry with `Math.min(10000, 1000 * 2 ** attempts)`; close codes 4401 → login redirect.
- [ ] **Step 4: Verify** — start server offline (see Task 11), load `/login`, confirm no console errors and `/static/css/app.css` 200.
- [ ] **Step 5: Leave uncommitted.**

---

### Task 6: Login page

**Files:** Create `templates/login.html` (standalone, not extending the shell), `static/js/pages/login.js`.

- Centered card: product name, username, password, submit, inline error region (`role="alert"`).
- Submit → `api("/api/auth/login", {method:"POST", body, quiet:true})`; success → `location = next || "/"` (only accept `next` values starting with `/` and not `//` — open-redirect guard); failure → inline "Invalid username or password"; button `aria-busy` while pending; Enter submits.
- Footer hint: "Default accounts are listed in the README".
- [ ] Verify: wrong password shows inline error; right password lands on `/`; `/login?next=//evil.com` lands on `/`.

---

### Task 7: Live page — twin

**Files:** Create `static/js/components/twin.js`; Live template skeleton in `templates/index.html`.

**Interface:** `createTwin(container, { onSelect(kind, item) }) -> { setLayout(layout), update(snapshot), focusZone(name|null), fit(), select(kind, name) }`.

- SVG with `viewBox` from `layout.bounds` + 150 px padding; layers: zones (rect, label, `Closed` zones get a subtle hatch + "Indoor" tag), stalls, entry/exit markers, gates, fans, lights.
- Stall: `<g transform="translate(x y) rotate(deg)">` with rect 80×170 centred, fill by status class `st-free|st-occupied|st-reserved|st-fault`, EV ⚡ / accessible ♿ glyph, name label (hidden below zoom 0.5 via class on root), occupant plate as `<title>` for hover.
- Gates: short bar, colour by `Open/Closed/Opening/Closing` + fault; duplicate names keyed by index.
- Zoom: wheel (cursor-anchored), drag to pan, buttons `+ − Fit`, zone chips "All · ZONE1 · …" call `focusZone`. Clamp zoom 0.2–6.
- Status comes only from `update(snapshot)`: map `snapshot.spots` by name, `snapshot.barriers` by name, `snapshot.fans` by name; unknown names are ignored.
- Selection: clicking a stall/gate/fan calls `onSelect`; selected element gets `.is-selected` outline; the selected vehicle's route (entry → aisle → spot, orthogonal, as in Joelton's canvas) drawn as a dashed path when the selected spot is RESERVED.
- Fallback: when `layout.level` is null, lay out stalls in a grid grouped by zone from `snapshot.spots` and show notice "Layout not recognised — showing schematic view".
- [ ] Verify with seeds `lvl1`, `lvl2`, `lvl3` (Task 11): all stalls visible after Fit, no overlap at rest, zone focus works on L3.

---

### Task 8: Live page — side panels, KPIs, drawer actions, banners

**Files:** Create `static/js/components/{zones,alerts,vehicles,feed,kpis}.js`, `static/js/pages/live.js`; finish `templates/index.html`.

- **KPIs** (`kpis.js`): Free `x / total` + 60-sample sparkline (SVG polyline), On site (sessions), Revenue (admin, from `/api/stats` every 5 s), Penalties (`count · −fines`, from `/api/stats`).
- **Zones** (`zones.js`): one card per zone from snapshot (spots grouped by `zone`, Park only): bar free/occupied/reserved/fault, per-type free counts (only types present), CO value + danger when zone reports > 0, fans on/total when fans exist, FULL badge when free = 0. Gates grouped by zone with state dot.
- **Needs attention** (`alerts.js`): derived list, severity-sorted, each with an action link — broken/maintenance components (from `snapshot.spots/barriers/fans`), `deferred_repairs`, suspect payments (`/api/stats.suspect_payments` delta), zones CO Mid/High/Critical, lot or zone FULL, `sequence_gaps > 0`. Empty state: "All clear".
- **Vehicles on site** (`vehicles.js`): `snapshot.sessions` via `keyedList` by plate; phase chip (ARRIVED/ASSIGNED/PARKED/LEAVING/AT_EXIT/CHARGED/PAID), spot, click → twin `select("spot", assigned_spot)`.
- **Event feed** (`feed.js`): `snapshot.activity` humanised: icon + category inferred from message prefix (Dispatched/parked/vacated/Charged/Payment/SUSPECT/PENALTY/broken/fixed/CO/barrier); filter chips All · Cars · Money · Faults; keyed by `at + message`.
- **Drawer** (`live.js` onSelect): spot → status, type, zone, occupant, actions `Repair` (disabled with reason when occupied or already broken/under repair — prevents `RepairAnOccupiedSpot`); gate → state, zone, actions `Open`, `Close`, `Repair` (Open/Close disabled when broken or under maintenance — prevents `OperateElementWhileUnderRepair`). Buttons call existing `/api/manual/barrier/{name}/open|close` and `/api/manual/repair/{name}`; `aria-busy` while pending; toast result. Operators and admins both see these.
- **Banners**: Autopilot off (amber, "Dry-run — no commands are sent to the simulator"); lot FULL (red); disconnected (grey, "Reconnecting… showing data from Ns ago" and `.is-stale` dims content).
- **Palette**: items = plates in sessions, spot names, gate names, pages; pick → select on twin / navigate.
- [ ] Verify with replay traffic: vehicle appears → parks → leaves; scroll the feed while updates arrive (position kept); open drawer for broken gate: Open disabled with reason.

---

### Task 9: History page

**Files:** `templates/history.html`, `static/js/pages/history.js`.

- Filter bar: plate text (debounced 300 ms), spot, status chips (All/Paid/Suspect/Unpaid), date from/to (`<input type=date>` → ISO start/end of local day via `toISOString()`), Reset. Filters mirrored in the URL query so a view is shareable.
- Table columns: Plate, Type, Spot, Arrived, Parked, Left, Minutes (planned), Charged, Paid, Status chip. Sticky header, 25 rows, pagination "1–25 of N" with Prev/Next.
- Row click → drawer with `/api/history/timeline?plate=` rendered as a vertical timeline (event class icon, time, key fields).
- States: loading skeleton rows, empty ("No sessions match these filters"), error (toast + retry button).
- [ ] Verify: seed via replay; filter by partial plate without spaces; paging; timeline opens.

---

### Task 10: Payments, Admin, Gate kiosk

**Files:** `templates/payments.html`, `templates/admin.html`, `templates/gate.html` (rewrite), `static/js/pages/{payments,admin,gate}.js`; delete superseded files listed in File Structure.

- **Payments** (admin): KPI row (revenue, net, suspect count, fines); table from `/api/payments?limit=200`: time, plate, expected, paid, verdict chip (Verified / Suspect with reason "Amount mismatch" or "Not billed yet" when `expected` is null); filter chips All/Verified/Suspect; fines-by-reason list from `/api/stats`.
- **Admin**: Users table (username, role, created) + "Add user" form (username, password ≥ 6, role select) + Delete (confirm dialog; server errors like "cannot delete the last admin" shown via toast); Audit log table (time, user, method, path, status); Debug tools card: Manual sync (confirm: "Calls the simulator's list APIs, which carry an operating cost") and Simulate arrival (plate, entry select from snapshot `EntrySpot`s, car type, dry-run) posting to existing endpoints.
- **Gate kiosk** (public, standalone layout, max-width 480 px): entry selector, plate, car type (Normal/Electric/Accessible); bays in natural order (`S2` before `S10`), only type-compatible bays enabled, EV/♿ marked; calm palette; big confirm button; result card; uses `keyedList` so taps are never lost on refresh; WebSocket closes with 4401 for anonymous users, so the kiosk reads `/api/gate/checkin` only — **the kiosk must work anonymously**: it renders bays from a public read. Add public `GET /api/gate/bays` to `dashboard_api.py` returning `[{name, zone, car_type, available}]` for Park spots (add it to `_PUBLIC_EXACT` in `auth.py`), polled every 2 s by the kiosk.
- [ ] Verify: operator hitting `/payments` is redirected to `/?denied=1` with toast; kiosk works in a private window with no login.

---

### Task 11: Verification and docs

**Files:** Modify `README.md` (dashboard section, accounts, pages, role matrix); `.env.example` (dashboard password vars).

- [ ] **Step 1:** `python -m pytest tests -q` — all pass.
- [ ] **Step 2:** Offline runs for each level:

```bash
SEED_FROM_LEVEL=lvl1 AUTOPILOT=false DATABASE_PATH=<scratch>/l1.db python -m uvicorn app.main:app --port 8080
python -m scripts.replay
```
  repeat with `lvl2`, `lvl3` (fresh DB each).
- [ ] **Step 3:** Playwright screenshots (login, live 1440×900, live 1280×800, history, payments, admin, gate 390×844) for each level; check console has no errors; compare against spec §7.
- [ ] **Step 4:** Edge checks: stop server while Live is open → reconnect banner, then restart → recovers; operator on admin API → 403 toast; `<script>` as plate via replay `PLATE` env → rendered as text.
- [ ] **Step 5:** Leave everything uncommitted; report changed files to the user.

---

## Self-review notes

- Spec §3 rows → Tasks 2/4 (auth), 8 (manual control), 8 (zones/FULL), 4/9 (history), 5/8 (live updates). §8 multi-level → Tasks 3, 7. §9 edge cases → Tasks 5 (api/live), 7 (fallback), 8 (banners, disabled actions), 9 (states), 11 (checks).
- Added during planning: public `GET /api/gate/bays` (kiosk can't use the authenticated WebSocket) — recorded in Task 10 and must be added to `_PUBLIC_EXACT`.
- Route name `/api/history/search` avoids shadowing `main.py`'s existing `/api/history`.
