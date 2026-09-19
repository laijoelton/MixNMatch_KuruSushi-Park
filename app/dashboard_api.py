"""Dashboard-only HTTP surface: pages, login, history search, stats, admin.

Read-only over the dispatcher's data (``app.db``, ``app.state``); it never
calls the simulator. Who may reach each route is decided by
``app.auth.AuthMiddleware`` before these handlers run, and
``request.state.user`` holds the signed-in user.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import auth, db, tariffs
from app.policy import capabilities, has, project_events, redact
from app.layout import current_geometry
from app.state import state

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


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
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


@router.get("/penalties", include_in_schema=False)
async def penalties_page(request: Request):
    return _page(request, "penalties.html", "penalties")


@router.get("/reports", include_in_schema=False)
async def reports_page(request: Request):
    return _page(request, "reports.html", "reports")


@router.get("/ml-insights", include_in_schema=False)
async def ml_insights_page(request: Request):
    return _page(request, "ml_insights.html", "ml-insights")


# --------------------------------------------------------------------------- #
# Sign in / out
# --------------------------------------------------------------------------- #
class LoginIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


@router.post("/api/auth/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
    ip = request.client.host if request.client else None
    username = body.username.strip()
    user = auth.authenticate(username, body.password)

    prior = db.prior_login_attempts(username, limit=3)
    db.record_login_attempt(username, ip, user is not None)

    if user is None:
        raise HTTPException(401, "Invalid username or password")
    token = auth.SessionStore.create(user["id"])
    response.set_cookie(auth.COOKIE_NAME, token, max_age=auth.SESSION_TTL_S,
                        httponly=True, samesite="lax", path="/")
    return {"username": user["username"], "role": user["role"], "prior_attempts": prior}


@router.post("/api/auth/logout")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    auth.SessionStore.revoke(request.cookies.get(auth.COOKIE_NAME))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/api/me")
async def me(request: Request) -> dict[str, Any]:
    user = _user(request)
    return {"username": user["username"], "role": user["role"], "capabilities": sorted(capabilities(user))}


# --------------------------------------------------------------------------- #
# Geometry for the console twin, and the public bay list for the gate kiosk
# --------------------------------------------------------------------------- #
@router.get("/api/twin")
async def twin_geometry() -> dict[str, Any]:
    """Detected level + list-based world geometry (see ``app.layout.current_geometry``)."""
    return current_geometry()


def _natural_key(name: str) -> tuple:
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name))


@router.get("/api/gate/bays")
async def gate_bays() -> list[dict[str, Any]]:
    """Park bays for the anonymous driver kiosk - no plates, no internals."""
    snapshot = state.snapshot()
    bays = [
        {"name": s["name"], "zone": s["zone"], "car_type": s["car_type"],
         "available": s["status"] == "AVAILABLE" and not s["broken"] and not s["under_maintenance"]
                      and not s["repair_pending"]}
        for s in snapshot["spots"] if s["purpose"] == "Park"
    ]
    return sorted(bays, key=lambda b: (b["zone"], _natural_key(b["name"])))


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
_STATUS_SQL = {"paid": "payment_ok = 1", "suspect": "payment_ok = 0", "unpaid": "payment_ok IS NULL"}


@router.get("/api/history/search")
async def history_search(request: Request, plate: Optional[str] = None, spot: Optional[str] = None,
                         status: Optional[str] = None,
                         date_from: Optional[str] = Query(None, alias="from"),
                         date_to: Optional[str] = Query(None, alias="to"),
                         page: int = 1, size: int = 25) -> dict[str, Any]:
    """Completed sessions, newest first, filtered and paginated in SQL."""
    where: list[str] = []
    params: list[Any] = []
    if plate:
        # Drivers and operators type plates with or without the space.
        where.append("REPLACE(UPPER(plate), ' ', '') LIKE ?")
        params.append(f"%{plate.upper().replace(' ', '')}%")
    if spot:
        where.append("spot = ?")
        params.append(spot.strip())
    if status and not has(request.state.user, "fin:view"):
        raise HTTPException(403, "Financial status filters require fin:view")
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
    items = db.query(
        f"SELECT * FROM sessions {clause} ORDER BY completed_at DESC, id DESC LIMIT ? OFFSET ?",
        tuple(params) + (size, (page - 1) * size))
    return {"items": redact(items, has(request.state.user, "fin:view")), "total": total, "page": page, "size": size}


@router.get("/api/history/timeline")
async def history_timeline(request: Request, plate: str = Query(..., min_length=1, max_length=16)) -> list[dict[str, Any]]:
    """Every stored webhook about one plate, in the simulator's own order."""
    rows = db.query(
        "SELECT sequence_id, event_class, server_datetime, received_at, payload FROM events "
        "WHERE json_extract(payload, '$.CarPlateNumber') = ? ORDER BY sequence_id LIMIT 200",
        (plate,))
    for row in rows:
        row["payload"] = json.loads(row["payload"])
    return project_events(rows, request.state.user)


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
@router.get("/api/stats")
async def stats(request: Request) -> dict[str, Any]:
    """Operational counters for everyone; money figures for admins only."""
    user = _user(request)
    out = db.query("""SELECT
        (SELECT COUNT(*) FROM sessions)                       AS completed_sessions,
        (SELECT COALESCE(AVG(minutes), 0) FROM sessions)      AS avg_minutes,
        (SELECT COUNT(*) FROM payments WHERE valid = 0)       AS suspect_payments,
        (SELECT COUNT(*) FROM penalties)                      AS penalty_count,
        (SELECT COALESCE(SUM(fine_amount), 0) FROM penalties) AS total_fines,
        (SELECT COUNT(*) FROM sequence_gaps)                  AS sequence_gaps,
        (SELECT COUNT(*) FROM events WHERE processed = 0)     AS unprocessed_events""")[0]
    if has(user, "fin:view"):
        revenue = db.query("SELECT COALESCE(SUM(amount), 0) AS r FROM payments WHERE valid = 1")[0]["r"]
        repair_costs = db.query(
            "SELECT COALESCE(SUM(amount), 0) AS r FROM component_events "
            "WHERE event IN ('fixed_proactive', 'fixed_reactive')")[0]["r"]
        out["revenue"] = round(revenue, 2)
        out["repair_costs"] = round(repair_costs, 2)
        out["net"] = round(revenue - out["total_fines"] - repair_costs, 2)
        out["fines_by_reason"] = db.query(
            "SELECT reason, COUNT(*) AS count, SUM(fine_amount) AS total FROM penalties "
            "GROUP BY reason ORDER BY total DESC LIMIT 10")
    return redact(out, has(user, "fin:view"))


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
class NewUserIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)
    role: str


@router.get("/api/admin/users")
async def admin_list_users() -> list[dict[str, Any]]:
    return auth.list_users()


@router.post("/api/admin/users", status_code=201)
async def admin_create_user(body: NewUserIn, request: Request) -> dict[str, Any]:
    try:
        created = auth.create_user(body.username, body.password, body.role)
        auth.record_audit(request.state.user["username"], "POST", "/api/admin/users", 201,
                          created["username"], {"before": None, "after": created})
        return created
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: int, request: Request) -> dict[str, bool]:
    before = next((u for u in auth.list_users() if u["id"] == user_id), None)
    try:
        auth.delete_user(user_id, acting_user_id=_user(request)["id"])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    auth.record_audit(request.state.user["username"], "DELETE", f"/api/admin/users/{user_id}", 200,
                      str(user_id), {"before": before, "after": None})
    return {"ok": True}


@router.get("/api/admin/audit")
async def admin_audit(limit: int = 100) -> list[dict[str, Any]]:
    return auth.recent_audit(limit)


class RoleIn(BaseModel):
    role: str


@router.patch("/api/admin/users/{user_id}")
async def admin_update_user(user_id: int, body: RoleIn, request: Request):
    before = next((u for u in auth.list_users() if u["id"] == user_id), None)
    try:
        after = auth.update_role(user_id, body.role)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    auth.record_audit(request.state.user["username"], "PATCH", f"/api/admin/users/{user_id}", 200,
                      str(user_id), {"before": before, "after": after})
    return after


@router.get("/tariffs", include_in_schema=False)
async def tariffs_page(request: Request):
    return _page(request, "tariffs.html", "tariffs")


@router.get("/api/tariffs")
async def read_tariffs():
    return {"settings": tariffs.effective(), "history": db.query("SELECT * FROM tariff_settings ORDER BY key LIMIT 100")}


@router.put("/api/tariffs")
async def write_tariffs(request: Request, changes: dict):
    try:
        before, after = tariffs.update(changes, request.state.user["username"])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    auth.record_audit(request.state.user["username"], "PUT", "/api/tariffs", 200, "tariff_settings",
                      {"before": before, "after": after})
    return {"settings": after}


@router.get("/logs", include_in_schema=False)
async def logs_page(request: Request):
    return _page(request, "logs.html", "logs")


LOG_TABS = {"operations": "logs:view_ops", "maintenance": "logs:view_maint",
            "financial": "logs:view_fin", "audit": "logs:view_audit", "logins": "logs:view_audit",
            "unsigned": "logs:view_audit"}


@router.get("/api/logs")
async def logs(request: Request, tab: str = "operations", page: int = 1, size: int = 50):
    if tab not in LOG_TABS or not has(request.state.user, LOG_TABS[tab]):
        raise HTTPException(403, "Log capability required")
    size, page = max(1, min(size, 100)), max(1, page)
    params = (size, (page - 1) * size)
    if tab == "audit":
        rows = auth._all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?", params)
    elif tab in ("logins", "unsigned"):
        table = "login_attempts" if tab == "logins" else "unsigned_webhook_logs"
        rows = db.query(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ? OFFSET ?", params)
        for row in rows:
            if "payload" in row:
                row["payload"] = json.loads(row["payload"])
        rows = redact(rows, True)
    else:
        from app.policy import EVENT_CAPABILITY
        classes = [c for c, capability in EVENT_CAPABILITY.items() if capability == LOG_TABS[tab]]
        placeholders = ",".join("?" for _ in classes)
        rows = db.query(f"SELECT * FROM events WHERE event_class IN ({placeholders}) ORDER BY received_at DESC LIMIT ? OFFSET ?",
                        tuple(classes) + params)
        rows = project_events(rows, request.state.user)
    return {"items": rows, "page": page, "size": size,
            "tabs": [tab for tab, cap in LOG_TABS.items() if has(request.state.user, cap)]}


@router.delete("/api/logs")
async def flush_logs(request: Request, tab: str):
    if tab not in LOG_TABS:
        raise HTTPException(400, "Unknown log tab")
    if tab == "audit":
        auth._write("DELETE FROM audit_log")
    else:
        with db._lock, db._conn:
            if tab in ("logins", "unsigned"):
                table = "login_attempts" if tab == "logins" else "unsigned_webhook_logs"
                db._conn.execute(f"DELETE FROM {table}")
            else:
                # Keep EventId deduplication intact; flush the detailed payload only.
                from app.policy import EVENT_CAPABILITY
                for event_class, cap in EVENT_CAPABILITY.items():
                    if cap == LOG_TABS[tab]:
                        db._conn.execute("UPDATE events SET payload = '{}', signature = NULL, process_error = NULL WHERE event_class = ?", (event_class,))
    auth.record_audit(request.state.user["username"], "DELETE", "/api/logs", 200, tab,
                      {"before": "retained", "after": "flushed"})
    return {"ok": True}


# Admin-only "clear this page". Only finished records go: cars still on site
# (active_sessions and in-memory sessions) must keep their billing state, and
# the events table stays because it is the EventId de-duplication record.
_CLEARABLE = {
    "history": ("sessions", "neglected_vehicles"),
    "payments": ("payments",),
    "penalties": ("penalties",),
}


@router.delete("/api/admin/data/{section}")
async def clear_section(request: Request, section: str):
    tables = _CLEARABLE.get(section)
    if tables is None:
        raise HTTPException(404, "Unknown section")
    with db._lock, db._conn:
        removed = sum(db._conn.execute(f"DELETE FROM {table}").rowcount for table in tables)
    if section == "history":
        state.clear_neglected()
    elif section == "penalties":
        state.clear_penalties()
    auth.record_audit(request.state.user["username"], "DELETE", f"/api/admin/data/{section}", 200, section,
                      {"before": {"rows": removed}, "after": {"rows": 0}})
    return {"ok": True, "section": section, "removed": removed}


@router.get("/api/admin/schema")
async def schema():
    return db.query("SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name LIMIT 200")


class SchemaColumnIn(BaseModel):
    table: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    column: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    type: str = Field(pattern=r"^(TEXT|INTEGER|REAL|BLOB)$")


@router.post("/api/admin/schema")
async def add_schema_column(body: SchemaColumnIn, request: Request):
    """Add a nullable reporting column without dropping existing data or constraints."""
    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type = 'table' LIMIT 200")}
    if body.table not in tables or body.table.startswith("sqlite_"):
        raise HTTPException(400, "Unknown application table")
    before = db.query(f'PRAGMA table_info("{body.table}")')
    try:
        with db._lock, db._conn:
            db._conn.execute(f'ALTER TABLE "{body.table}" ADD COLUMN "{body.column}" {body.type}')
    except sqlite3.OperationalError as exc:
        raise HTTPException(400, str(exc)) from exc
    after = db.query(f'PRAGMA table_info("{body.table}")')
    auth.record_audit(request.state.user["username"], "POST", "/api/admin/schema", 200, body.table,
                      {"before": before, "after": after})
    return {"table": body.table, "columns": after}
