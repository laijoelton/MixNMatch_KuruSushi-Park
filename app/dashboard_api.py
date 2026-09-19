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
    raise NotImplementedError("TODO: reimplement _page")


def _user(request: Request) -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement _user")


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@router.get("/login", include_in_schema=False)
async def login_page(request: Request):
    raise NotImplementedError("TODO: reimplement login_page")


@router.get("/history", include_in_schema=False)
async def history_page(request: Request):
    raise NotImplementedError("TODO: reimplement history_page")


@router.get("/payments", include_in_schema=False)
async def payments_page(request: Request):
    raise NotImplementedError("TODO: reimplement payments_page")


@router.get("/admin", include_in_schema=False)
async def admin_page(request: Request):
    raise NotImplementedError("TODO: reimplement admin_page")


@router.get("/penalties", include_in_schema=False)
async def penalties_page(request: Request):
    raise NotImplementedError("TODO: reimplement penalties_page")


@router.get("/reports", include_in_schema=False)
async def reports_page(request: Request):
    raise NotImplementedError("TODO: reimplement reports_page")


@router.get("/ml-insights", include_in_schema=False)
async def ml_insights_page(request: Request):
    raise NotImplementedError("TODO: reimplement ml_insights_page")


# --------------------------------------------------------------------------- #
# Sign in / out
# --------------------------------------------------------------------------- #
class LoginIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


@router.post("/api/auth/login")
async def login(body: LoginIn, request: Request, response: Response) -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement login")


@router.post("/api/auth/logout")
async def logout(request: Request, response: Response) -> dict[str, bool]:
    raise NotImplementedError("TODO: reimplement logout")


@router.get("/api/me")
async def me(request: Request) -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement me")


# --------------------------------------------------------------------------- #
# Geometry for the console twin, and the public bay list for the gate kiosk
# --------------------------------------------------------------------------- #
@router.get("/api/twin")
async def twin_geometry() -> dict[str, Any]:
    """Detected level + list-based world geometry (see ``app.layout.current_geometry``)."""
    raise NotImplementedError("TODO: reimplement twin_geometry")


def _natural_key(name: str) -> tuple:
    raise NotImplementedError("TODO: reimplement _natural_key")


@router.get("/api/gate/bays")
async def gate_bays() -> list[dict[str, Any]]:
    """Park bays for the anonymous driver kiosk - no plates, no internals."""
    raise NotImplementedError("TODO: reimplement gate_bays")


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
    raise NotImplementedError("TODO: reimplement history_search")


@router.get("/api/history/timeline")
async def history_timeline(request: Request, plate: str = Query(..., min_length=1, max_length=16)) -> list[dict[str, Any]]:
    """Every stored webhook about one plate, in the simulator's own order."""
    raise NotImplementedError("TODO: reimplement history_timeline")


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
@router.get("/api/stats")
async def stats(request: Request) -> dict[str, Any]:
    """Operational counters for everyone; money figures for admins only."""
    raise NotImplementedError("TODO: reimplement stats")


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
class NewUserIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)
    role: str


@router.get("/api/admin/users")
async def admin_list_users() -> list[dict[str, Any]]:
    raise NotImplementedError("TODO: reimplement admin_list_users")


@router.post("/api/admin/users", status_code=201)
async def admin_create_user(body: NewUserIn, request: Request) -> dict[str, Any]:
    raise NotImplementedError("TODO: reimplement admin_create_user")


@router.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: int, request: Request) -> dict[str, bool]:
    raise NotImplementedError("TODO: reimplement admin_delete_user")


@router.get("/api/admin/audit")
async def admin_audit(limit: int = 100) -> list[dict[str, Any]]:
    raise NotImplementedError("TODO: reimplement admin_audit")


class RoleIn(BaseModel):
    role: str


@router.patch("/api/admin/users/{user_id}")
async def admin_update_user(user_id: int, body: RoleIn, request: Request):
    raise NotImplementedError("TODO: reimplement admin_update_user")


@router.get("/tariffs", include_in_schema=False)
async def tariffs_page(request: Request):
    raise NotImplementedError("TODO: reimplement tariffs_page")


@router.get("/api/tariffs")
async def read_tariffs():
    raise NotImplementedError("TODO: reimplement read_tariffs")


@router.put("/api/tariffs")
async def write_tariffs(request: Request, changes: dict):
    raise NotImplementedError("TODO: reimplement write_tariffs")


@router.get("/logs", include_in_schema=False)
async def logs_page(request: Request):
    raise NotImplementedError("TODO: reimplement logs_page")


LOG_TABS = {"operations": "logs:view_ops", "maintenance": "logs:view_maint",
            "financial": "logs:view_fin", "audit": "logs:view_audit", "logins": "logs:view_audit",
            "unsigned": "logs:view_audit"}


@router.get("/api/logs")
async def logs(request: Request, tab: str = "operations", page: int = 1, size: int = 50):
    raise NotImplementedError("TODO: reimplement logs")


@router.delete("/api/logs")
async def flush_logs(request: Request, tab: str):
    raise NotImplementedError("TODO: reimplement flush_logs")


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
    raise NotImplementedError("TODO: reimplement clear_section")


@router.get("/api/admin/schema")
async def schema():
    raise NotImplementedError("TODO: reimplement schema")


class SchemaColumnIn(BaseModel):
    table: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    column: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    type: str = Field(pattern=r"^(TEXT|INTEGER|REAL|BLOB)$")


@router.post("/api/admin/schema")
async def add_schema_column(body: SchemaColumnIn, request: Request):
    """Add a nullable reporting column without dropping existing data or constraints."""
    raise NotImplementedError("TODO: reimplement add_schema_column")
