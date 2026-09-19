"""Basic staff authentication: two roles, Admin and Operator.

Only the staff-facing surfaces (operator dashboard, admin dashboard, manual
control endpoints) sit behind this. The public driver portal (``/gate``,
``/api/gate/checkin``, ``/api/dispatch``) is deliberately left open - a
walk-in driver has no staff account and was never meant to need one.

Sessions are an in-memory token -> {username, role, expires_at} map, keyed by
a random token handed to the browser as an ``HttpOnly`` cookie. This is
"basic authentication" in the sense the brief asks for - good enough for a
single-process demo deployment, not a replacement for a real identity
provider. Passwords are compared with ``secrets.compare_digest`` to avoid
timing side-channels; they are not hashed at rest because they only ever
live in ``.env``, never in a database.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import Cookie, HTTPException, status

from app.config import settings

SESSION_COOKIE = "kuru_session"

Role = str  # "admin" | "operator"
ROLE_ADMIN: Role = "admin"
ROLE_OPERATOR: Role = "operator"


@dataclass
class SessionInfo:
    token: str
    username: str
    role: Role
    expires_at: float


class SessionStore:
    """In-memory session table. Fine for a single-process dispatcher; a
    restart logs everyone out, which is an acceptable trade-off here."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionInfo] = {}

    def create(self, username: str, role: Role) -> SessionInfo:
        token = secrets.token_urlsafe(32)
        info = SessionInfo(token=token, username=username, role=role,
                           expires_at=time.time() + settings.session_ttl_s)
        self._sessions[token] = info
        return info

    def get(self, token: Optional[str]) -> Optional[SessionInfo]:
        if not token:
            return None
        info = self._sessions.get(token)
        if info is None:
            return None
        if info.expires_at < time.time():
            self._sessions.pop(token, None)
            return None
        return info

    def destroy(self, token: Optional[str]) -> None:
        if token:
            self._sessions.pop(token, None)

    def active_sessions(self) -> list[SessionInfo]:
        now = time.time()
        return [s for s in self._sessions.values() if s.expires_at >= now]


sessions = SessionStore()


def authenticate(username: str, password: str) -> Optional[Role]:
    """Check credentials against the two configured staff accounts."""
    if (secrets.compare_digest(username, settings.admin_username)
            and secrets.compare_digest(password, settings.admin_password)):
        return ROLE_ADMIN
    if (secrets.compare_digest(username, settings.operator_username)
            and secrets.compare_digest(password, settings.operator_password)):
        return ROLE_OPERATOR
    return None


async def optional_user(
    kuru_session: Optional[str] = Cookie(default=None),
) -> Optional[SessionInfo]:
    """Dependency: the current session if any, without requiring one."""
    return sessions.get(kuru_session)


async def require_staff(
    kuru_session: Optional[str] = Cookie(default=None),
) -> SessionInfo:
    """Dependency: any logged-in staff member (Operator or Admin)."""
    info = sessions.get(kuru_session)
    if info is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "login required")
    return info


async def require_admin(
    kuru_session: Optional[str] = Cookie(default=None),
) -> SessionInfo:
    """Dependency: Admin role only."""
    info = sessions.get(kuru_session)
    if info is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "login required")
    if info.role != ROLE_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return info
