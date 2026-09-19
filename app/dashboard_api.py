"""Dashboard-only HTTP surface: pages, login, history search, stats, admin.

Read-only over the dispatcher's data (``app.db``, ``app.state``); it never
calls the simulator. Who may reach each route is decided by
``app.auth.AuthMiddleware`` before these handlers run, and
``request.state.user`` holds the signed-in user.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app import auth, db
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


# --------------------------------------------------------------------------- #
# Sign in / out
# --------------------------------------------------------------------------- #
class LoginIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


@router.post("/api/auth/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
    ip = request.client.host if request.client else None
    user = auth.authenticate(body.username, body.password)

    prior = db.prior_login_attempts(body.username, limit=3)
    db.record_login_attempt(body.username, ip, user is not None)

    if user is None:
        raise HTTPException(401, "Invalid username or password")
    token = auth.create_session(user["id"])
    response.set_cookie(auth.COOKIE_NAME, token, max_age=auth.SESSION_TTL_S,
                        httponly=True, samesite="lax", path="/")
    return {"username": user["username"], "role": user["role"], "prior_attempts": prior}


@router.post("/api/auth/logout")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    auth.revoke_session(request.cookies.get(auth.COOKIE_NAME))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/api/me")
async def me(request: Request) -> dict[str, str]:
    user = _user(request)
    return {"username": user["username"], "role": user["role"]}


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
         "available": s["status"] == "AVAILABLE" and not s["broken"] and not s["under_maintenance"]}
        for s in snapshot["spots"] if s["purpose"] == "Park"
    ]
    return sorted(bays, key=lambda b: (b["zone"], _natural_key(b["name"])))


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
_STATUS_SQL = {"paid": "payment_ok = 1", "suspect": "payment_ok = 0", "unpaid": "payment_ok IS NULL"}


@router.get("/api/history/search")
async def history_search(plate: Optional[str] = None, spot: Optional[str] = None,
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
    return {"items": items, "total": total, "page": page, "size": size}


@router.get("/api/history/timeline")
async def history_timeline(plate: str = Query(..., min_length=1, max_length=16)) -> list[dict[str, Any]]:
    """Every stored webhook about one plate, in the simulator's own order."""
    rows = db.query(
        "SELECT sequence_id, event_class, server_datetime, received_at, payload FROM events "
        "WHERE json_extract(payload, '$.CarPlateNumber') = ? ORDER BY sequence_id LIMIT 200",
        (plate,))
    for row in rows:
        row["payload"] = json.loads(row["payload"])
    return rows


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
        (SELECT COUNT(*) FROM sequence_gaps)                  AS sequence_gaps""")[0]
    if user["role"] == "admin":
        revenue = db.query("SELECT COALESCE(SUM(amount), 0) AS r FROM payments WHERE valid = 1")[0]["r"]
        out["revenue"] = round(revenue, 2)
        out["net"] = round(revenue - out["total_fines"], 2)
        out["fines_by_reason"] = db.query(
            "SELECT reason, COUNT(*) AS count, SUM(fine_amount) AS total FROM penalties "
            "GROUP BY reason ORDER BY total DESC LIMIT 10")
    return out


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
