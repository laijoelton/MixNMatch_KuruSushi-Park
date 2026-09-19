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
from typing import Awaitable, Callable, Optional

from fastapi import Cookie, HTTPException, status

from app.config import settings

SESSION_COOKIE = "kuru_session"

Role = str

ROLE_ADMIN: Role = "admin"
ROLE_OPERATOR: Role = "operator"
ROLE_ACCOUNTANT: Role = "accountant"
ROLE_ENGINEER: Role = "engineer"

# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #
# Endpoints ask for a *permission*, never a role. Adding a role for level 2 is
# then one row in ROLE_PERMISSIONS, with no endpoint changes -- and no risk of
# a new role silently inheriting something because a check said `!= admin`.
PERM_LOT_VIEW = "lot.view"                # see spots, gates, zones, layout
PERM_LOT_CONTROL = "lot.control"          # open/close barriers, dispatch cars
PERM_MAINTENANCE = "maintenance"          # queue repairs, resync from simulator
PERM_FINANCE_VIEW = "finance.view"        # payments, penalties, earnings
PERM_DIAGNOSTICS_VIEW = "diagnostics.view"  # raw event log, signature report
PERM_HISTORY_VIEW = "history.view"        # completed sessions + plate search
PERM_STAFF_MANAGE = "staff.manage"        # who is logged in

ALL_PERMISSIONS: frozenset[str] = frozenset({
    PERM_LOT_VIEW, PERM_LOT_CONTROL, PERM_MAINTENANCE, PERM_FINANCE_VIEW,
    PERM_DIAGNOSTICS_VIEW, PERM_HISTORY_VIEW, PERM_STAFF_MANAGE,
})

ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    # Admin has every authority, including any permission added later -- that
    # is why this is ALL_PERMISSIONS and not a hand-listed set that would
    # quietly go stale the next time a permission is introduced.
    ROLE_ADMIN: ALL_PERMISSIONS,

    # Watches the floor: gates and bays, and can look a car up.
    ROLE_OPERATOR: frozenset({PERM_LOT_VIEW, PERM_LOT_CONTROL, PERM_HISTORY_VIEW}),

    # Money only. Deliberately no lot control and no raw event log.
    ROLE_ACCOUNTANT: frozenset({PERM_FINANCE_VIEW, PERM_HISTORY_VIEW}),

    # Diagnoses faults: needs the raw log and the ability to queue a repair,
    # and the lot view for the context of *which* component is broken.
    ROLE_ENGINEER: frozenset({PERM_LOT_VIEW, PERM_DIAGNOSTICS_VIEW,
                              PERM_MAINTENANCE, PERM_HISTORY_VIEW}),
}

# Where each role lands after login: a page it can actually read. Sending an
# accountant to the lot canvas would just bounce them off a 403.
ROLE_HOME: dict[Role, str] = {
    ROLE_ADMIN: "/admin",
    ROLE_OPERATOR: "/dashboard",
    ROLE_ACCOUNTANT: "/admin",
    ROLE_ENGINEER: "/admin",
}


def permissions_for(role: Role) -> frozenset[str]:
    return ROLE_PERMISSIONS.get(role, frozenset())


@dataclass
class SessionInfo:
    token: str
    username: str
    role: Role
    expires_at: float

    @property
    def permissions(self) -> frozenset[str]:
        return permissions_for(self.role)

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    @property
    def home(self) -> str:
        return ROLE_HOME.get(self.role, "/admin")


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


def staff_accounts() -> list[tuple[str, str, Role]]:
    """The configured (username, password, role) triples, from .env.

    One account per role is enough for a demo deployment. Growing this into a
    real user table means replacing this function and nothing else -- every
    check downstream goes through permissions, not usernames.
    """
    return [
        (settings.admin_username, settings.admin_password, ROLE_ADMIN),
        (settings.operator_username, settings.operator_password, ROLE_OPERATOR),
        (settings.accountant_username, settings.accountant_password, ROLE_ACCOUNTANT),
        (settings.engineer_username, settings.engineer_password, ROLE_ENGINEER),
    ]


def authenticate(username: str, password: str) -> Optional[Role]:
    """Check credentials against the configured staff accounts.

    Every account is compared even after a match so the work done does not
    depend on which role was supplied. compare_digest is given encoded bytes
    because its str form raises TypeError on any non-ASCII input -- a
    non-ASCII username would otherwise be a 500 instead of a failed login.
    """
    supplied_user = username.encode("utf-8")
    supplied_pass = password.encode("utf-8")
    matched: Optional[Role] = None
    for account_user, account_pass, role in staff_accounts():
        ok_user = secrets.compare_digest(supplied_user, account_user.encode("utf-8"))
        ok_pass = secrets.compare_digest(supplied_pass, account_pass.encode("utf-8"))
        if ok_user and ok_pass:
            matched = role
    return matched


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


def require_permission(permission: str) -> Callable[..., Awaitable[SessionInfo]]:
    """Build a dependency that admits any role holding ``permission``.

    Prefer this over a role comparison: a role test has to be revisited every
    time a role is added, a permission test does not.
    """
    async def dependency(
        kuru_session: Optional[str] = Cookie(default=None),
    ) -> SessionInfo:
        info = sessions.get(kuru_session)
        if info is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "login required")
        if not info.can(permission):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"role '{info.role}' lacks permission '{permission}'")
        return info

    return dependency


def require_any(*permissions: str) -> Callable[..., Awaitable[SessionInfo]]:
    """Dependency admitting a role holding at least one of ``permissions``."""
    async def dependency(
        kuru_session: Optional[str] = Cookie(default=None),
    ) -> SessionInfo:
        info = sessions.get(kuru_session)
        if info is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "login required")
        if not any(info.can(p) for p in permissions):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"role '{info.role}' lacks any of {list(permissions)}")
        return info

    return dependency


# --------------------------------------------------------------------------- #
# Snapshot filtering
# --------------------------------------------------------------------------- #
# Which snapshot keys each permission unlocks. Without this the role split is
# decorative: /api/spots refuses an Accountant, but /api/state and the live
# WebSocket would hand them the same spots anyway.
#
# Keys absent from every entry below are considered harmless and always sent
# (server_time, the "type" discriminator, and similar framing).
_SNAPSHOT_KEYS: dict[str, frozenset[str]] = {
    PERM_LOT_VIEW: frozenset({"spots", "barriers", "fans", "zones",
                              "zone_occupancy", "occupancy", "sessions",
                              "deferred_repairs"}),
    PERM_FINANCE_VIEW: frozenset({"penalties"}),
    PERM_DIAGNOSTICS_VIEW: frozenset({"penalties", "activity", "spots", "barriers",
                                      "fans", "zones", "zone_occupancy", "occupancy",
                                      "deferred_repairs", "last_sequence_id"}),
    PERM_LOT_CONTROL: frozenset({"activity"}),
}

_GATED_KEYS: frozenset[str] = frozenset().union(*_SNAPSHOT_KEYS.values())


def visible_snapshot_keys(permissions: frozenset[str]) -> frozenset[str]:
    """Snapshot keys a holder of ``permissions`` may see."""
    allowed: set[str] = set()
    for permission, keys in _SNAPSHOT_KEYS.items():
        if permission in permissions:
            allowed |= keys
    return frozenset(allowed)


def filter_snapshot(snapshot: dict, permissions: frozenset[str]) -> dict:
    """Drop the parts of a state snapshot ``permissions`` does not cover."""
    allowed = visible_snapshot_keys(permissions)
    return {k: v for k, v in snapshot.items()
            if k not in _GATED_KEYS or k in allowed}
