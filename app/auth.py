"""Dashboard authentication: users, sessions, role policy and audit log.

Separate from the simulator login in ``app/client.py`` - this decides who may
use *our* dashboard. Everything is stdlib: PBKDF2 for passwords, random
tokens for sessions, and the same SQLite file the rest of the service uses.

Access control lives in one place, ``app.policy.required_capabilities()``, and is enforced by
``AuthMiddleware`` for every HTTP request and WebSocket - hiding a button in
the browser is never the security boundary.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import threading
import time
from http.cookies import SimpleCookie
from typing import Any, Optional

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse, RedirectResponse

from app import db  # noqa: F401 - importing db creates the data directory
from app.config import settings
from app.policy import ROLES, allowed, capabilities, has, required_capabilities

log = logging.getLogger("dashboard.auth")

COOKIE_NAME = "pg_session"
SESSION_TTL_S = 8 * 3600
PBKDF2_ITERATIONS = 200_000

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


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str, salt_hex: Optional[str] = None) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return digest.hex(), salt.hex()


def verify_password(password: str, hash_hex: str, salt_hex: str) -> bool:
    candidate, _ = hash_password(password, salt_hex)
    return hmac.compare_digest(candidate, hash_hex)


# --------------------------------------------------------------------------- #
# Small SQL helpers (own connection, serialised by a lock)
# --------------------------------------------------------------------------- #
def _one(sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    with _lock:
        row = _conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _all(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


def _write(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _lock, _conn:
        cur = _conn.execute(sql, params)
        return cur


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
def create_user(username: str, password: str, role: str) -> dict[str, Any]:
    username = username.strip()
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    if not username or len(password) < 6:
        raise ValueError("username is required and the password needs at least 6 characters")
    pw_hash, salt = hash_password(password)
    try:
        cur = _write(
            "INSERT INTO dashboard_users (username, pw_hash, salt, role, created_at) VALUES (?,?,?,?,?)",
            (username, pw_hash, salt, role, time.time()))
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"user '{username}' already exists") from exc
    return _one(f"SELECT {_PUBLIC_USER_FIELDS} FROM dashboard_users WHERE id = ?", (cur.lastrowid,))


def list_users() -> list[dict[str, Any]]:
    return _all(f"SELECT {_PUBLIC_USER_FIELDS} FROM dashboard_users ORDER BY id")


def delete_user(user_id: int, acting_user_id: int) -> None:
    if user_id == acting_user_id:
        raise ValueError("you cannot delete your own account")
    with _lock, _conn:
        _conn.execute("BEGIN IMMEDIATE")
        target = _one("SELECT role FROM dashboard_users WHERE id = ?", (user_id,))
        if target is None:
            raise ValueError("no such user")
        if target["role"] == "admin":
            admins = _one("SELECT COUNT(*) AS n FROM dashboard_users WHERE role = 'admin'")["n"]
            if admins <= 1:
                raise ValueError("cannot delete the last admin")
        _conn.execute("DELETE FROM dashboard_sessions WHERE user_id = ?", (user_id,))
        _conn.execute("DELETE FROM dashboard_users WHERE id = ?", (user_id,))


def authenticate(username: str, password: str) -> Optional[dict[str, Any]]:
    row = _one("SELECT * FROM dashboard_users WHERE username = ?", (username.strip(),))
    if row is None:
        hash_password(password)  # same cost either way, so timing does not reveal valid usernames
        return None
    if not verify_password(password, row["pw_hash"], row["salt"]):
        return None
    return {k: row[k] for k in ("id", "username", "role", "created_at")}


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    _write("INSERT INTO dashboard_sessions (token, user_id, expires_at) VALUES (?,?,?)",
           (token, user_id, time.time() + SESSION_TTL_S))
    return token


def user_for_token(token: Optional[str]) -> Optional[dict[str, Any]]:
    if not token:
        return None
    row = _one(
        "SELECT s.expires_at, u.id, u.username, u.role, u.created_at "
        "FROM dashboard_sessions s JOIN dashboard_users u ON u.id = s.user_id WHERE s.token = ?",
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


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #
def record_audit(username: str, method: str, path: str, status: Optional[int],
                 target: str = "", detail: Optional[dict] = None) -> None:
    _write("INSERT INTO audit_log (at, username, method, path, status, target, detail) VALUES (?,?,?,?,?,?,?)",
           (time.time(), username, method, path, status, target or path, json.dumps(detail or {})))


def recent_audit(limit: int = 100) -> list[dict[str, Any]]:
    return _all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))


def ensure_schema_and_seed() -> None:
    with _lock:
        _conn.executescript(_SCHEMA)
        columns = {r[1] for r in _conn.execute("PRAGMA table_info(audit_log)")}
        for name in ("target", "detail"):
            if name not in columns:
                _conn.execute(f"ALTER TABLE audit_log ADD COLUMN {name} TEXT")
        for old, new in (("operator", "facility_operator"), ("maintenance", "maintenance_technician"),
                         ("financial_auditor", "auditor")):
            _conn.execute("UPDATE dashboard_users SET role = ? WHERE role = ?", (new, old))
        _conn.commit()
    for username, role, password in (
        ("admin", "admin", settings.dashboard_admin_password),
        ("operator", "facility_operator", settings.dashboard_operator_password),
        ("auditor", "auditor", settings.dashboard_auditor_password),
        ("technician", "maintenance_technician", settings.dashboard_technician_password),
    ):
        if not _one("SELECT id FROM dashboard_users WHERE role = ?", (role,)) and password:
            if not _one("SELECT id FROM dashboard_users WHERE username = ?", (username,)):
                create_user(username, password, role)


# --------------------------------------------------------------------------- #
# Access policy - the single place that decides who may call what
# --------------------------------------------------------------------------- #
def _cookie(scope: dict, name: str) -> Optional[str]:
    for key, value in scope.get("headers", []):
        if key == b"cookie":
            jar = SimpleCookie()
            jar.load(value.decode("latin-1"))
            if name in jar:
                return jar[name].value
    return None


class AuthMiddleware:
    """Pure ASGI middleware, so it guards HTTP *and* the ``/ws/live`` WebSocket."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        user = SessionStore.get(_cookie(scope, COOKIE_NAME))
        scope.setdefault("state", {})["user"] = user

        method = "WEBSOCKET" if scope["type"] == "websocket" else scope["method"]
        need = required_capabilities(path, method)
        if need is None:
            log.warning("Unmapped route denied by default: %s %s", method, path)
        denied = None
        if not allowed(user, path, method):
            denied = 403 if need is None or user else 401

        if denied and scope["type"] == "websocket":
            await receive()  # consume websocket.connect, then refuse the handshake
            await send({"type": "websocket.close", "code": 4401 if denied == 401 else 4403})
            return
        if denied:
            is_page = not path.startswith("/api/") and scope.get("method") == "GET"
            if is_page and denied == 401:
                response = RedirectResponse(f"/login?next={path}", status_code=302)
            elif is_page:
                response = RedirectResponse("/?denied=1", status_code=302)
            else:
                detail = "Sign in required" if denied == 401 else "Capability required"
                response = JSONResponse({"detail": detail}, status_code=denied)
            if user:
                record_audit(user["username"], method, path, denied)
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


# --------------------------------------------------------------------------- #
# Per-route role dependencies - fine-grained, on top of AuthMiddleware
# --------------------------------------------------------------------------- #
def require_staff(request: Request) -> dict[str, Any]:
    """Any signed-in dashboard user."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "Sign in required")
    return user


def require_capability(capability: str):
    def dependency(request: Request) -> dict[str, Any]:
        user = require_staff(request)
        if not has(user, capability):
            raise HTTPException(403, f"Requires capability: {capability}")
        return user
    return dependency


require_admin = require_capability("admin:users")
require_maintenance = require_capability("maint:control")
require_financial_auditor = require_capability("fin:view")


def update_role(user_id: int, role: str) -> dict:
    if role not in ROLES:
        raise ValueError("unknown role")
    with _lock, _conn:
        _conn.execute("BEGIN IMMEDIATE")
        target = _one("SELECT * FROM dashboard_users WHERE id = ?", (user_id,))
        if target is None:
            raise ValueError("no such user")
        if target["role"] == "admin" and role != "admin":
            if _one("SELECT COUNT(*) AS n FROM dashboard_users WHERE role = 'admin'")["n"] <= 1:
                raise ValueError("cannot demote the last admin")
        _conn.execute("UPDATE dashboard_users SET role = ? WHERE id = ?", (role, user_id))
    return _one(f"SELECT {_PUBLIC_USER_FIELDS} FROM dashboard_users WHERE id = ?", (user_id,))


class SessionStore:
    """Durable cookie sessions; each lookup joins the current user role."""
    create = staticmethod(create_session)
    get = staticmethod(user_for_token)
    revoke = staticmethod(revoke_session)


def install(app) -> None:
    """Create tables, seed default accounts, and put the middleware in front of every route."""
    ensure_schema_and_seed()
    app.add_middleware(AuthMiddleware)
