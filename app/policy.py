"""Explicit capabilities and projections shared by HTTP, WebSocket and UI."""
from __future__ import annotations

import json
import re
from typing import Any

CAPABILITIES = frozenset({
    "ops:view", "ops:control_gates", "ops:control_dispatch", "maint:view", "maint:control",
    "fin:view", "fin:write_tariff", "logs:view_ops", "logs:view_maint", "logs:view_fin",
    "logs:view_audit", "logs:flush", "admin:users", "admin:schema", "admin:reset",
})
ROLE_CAPABILITIES = {
    "admin": CAPABILITIES,
    "auditor": frozenset({"fin:view", "fin:write_tariff", "logs:view_fin", "ops:view"}),
    "facility_operator": frozenset({"ops:view", "ops:control_gates", "maint:view", "logs:view_ops", "logs:view_maint"}),
    "maintenance_technician": frozenset({"ops:view", "maint:view", "logs:view_ops", "logs:view_maint"}),
}
ROLES = tuple(ROLE_CAPABILITIES)


def capabilities(user: dict | None) -> frozenset[str]:
    return ROLE_CAPABILITIES.get((user or {}).get("role"), frozenset())


def has(user: dict | None, capability: str) -> bool:
    return capability in capabilities(user)


# Method-specific patterns are full-matched: adding a route never implicitly grants access.
# Empty set is public; 'authenticated' covers self-service for all four roles.
ROUTE_POLICY: list[tuple[str, str, frozenset[str]]] = []


def _routes(method: str, paths: tuple[str, ...], *needs: str) -> None:
    ROUTE_POLICY.extend((method, p, frozenset(needs)) for p in paths)


_routes("GET", ("/login", "/gate", "/healthz", "/favicon.ico", "/api/gate/bays", "/static(?:/.*)?"))
_routes("POST", ("/api/auth/login", "/webhooks/simulator", "/api/gate/checkin"))
_routes("GET", ("/api/me",), "authenticated")
_routes("POST", ("/api/auth/logout",), "authenticated")
_routes("GET", ("/", "/dashboard", "/api/state", "/api/spots", "/api/gates", "/api/layout", "/api/twin", "/api/stats", "/reports", "/api/reports/daily", "/api/ghost-cars"), "ops:view")
_routes("WEBSOCKET", ("/ws/live", "/ws/telemetry"), "ops:view")
_routes("GET", ("/history", "/api/history", "/api/history/search", "/api/history/timeline"), "ops:view")
_routes("GET", ("/payments", "/api/payments", "/penalties", "/api/penalties"), "fin:view")
_routes("GET", ("/api/broken",), "maint:view")
_routes("POST", ("/api/manual/barrier/[^/]+/(?:open|close|auto)", "/api/barriers/[^/]+/(?:open|close|auto)", "/api/ghost-cars/[0-9]+/override", "/api/ghost-car/override", "/api/ghost-cars/[0-9]+/release"), "ops:control_gates")
_routes("POST", ("/api/dispatch", "/api/manual/arrival", "/api/manual/sync"), "ops:control_dispatch")
_routes("POST", ("/api/manual/repair/[^/]+", "/api/manual/(?:fan|light)/[^/]+/(?:on|off)"), "maint:control")
_routes("GET", ("/api/events", "/logs", "/api/logs"), "logs:view_ops", "logs:view_maint", "logs:view_fin", "logs:view_audit")
_routes("GET", ("/tariffs", "/api/tariffs"), "fin:view")
_routes("PUT", ("/api/tariffs",), "fin:write_tariff")
_routes("GET", ("/admin", "/api/admin/users"), "admin:users")
_routes("POST", ("/api/admin/users",), "admin:users")
_routes("PATCH", ("/api/admin/users/[0-9]+",), "admin:users")
_routes("DELETE", ("/api/admin/users/[0-9]+",), "admin:users")
_routes("GET", ("/api/admin/audit", "/api/signature-report"), "logs:view_audit")
_routes("DELETE", ("/api/logs",), "logs:flush")
_routes("DELETE", ("/api/admin/data/[a-z]+",), "admin:reset")
_routes("GET", ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json", "/api/admin/schema"), "admin:schema")
_routes("POST", ("/api/admin/schema",), "admin:schema")


def required_capabilities(path: str, method: str = "GET") -> frozenset[str] | None:
    method = "GET" if method == "HEAD" else method
    for verb, pattern, needs in ROUTE_POLICY:
        if verb == method and re.fullmatch(pattern, path):
            return needs
    return None


def allowed(user: dict | None, path: str, method: str = "GET") -> bool:
    needs = required_capabilities(path, method)
    if needs == frozenset():
        return True
    if user is None:
        return False
    if user.get("role") == "admin":
        return True
    return needs is not None and ("authenticated" in needs or bool(needs & capabilities(user)))


EVENT_CAPABILITY = {
    "car_spot_action": "logs:view_ops", "gate_action": "logs:view_ops",
    "carbon_monoxide_event": "logs:view_ops", "test_webhook": "logs:view_ops",
    "component_broken": "logs:view_maint", "component_fixed": "logs:view_maint",
    "payment_made": "logs:view_fin", "penalty": "logs:view_fin",
}
FINANCIAL_KEYS = frozenset({
    "amount", "expected", "expected_amount", "expected_parking", "expected_charging",
    "parking_cost", "charging_cost", "paid_amount", "payment_ok", "charged", "paid",
    "fine", "fine_amount", "fineamount", "fallback_charge", "revenue", "net", "net_revenue",
    "total_fines", "suspect_payments", "penalty_count", "fines_by_reason", "penalties",
    "parkingcost", "chargingcost",
    "repaircost", "repair_cost",
    "repair_costs", "repair_cost_total",
})


def redact(value: Any, financial: bool) -> Any:
    if isinstance(value, dict):
        return {k: redact(v, financial) for k, v in value.items()
                if k.lower() != "signature" and (financial or k.lower() not in FINANCIAL_KEYS)}
    if isinstance(value, list):
        return [redact(v, financial) for v in value]
    return value


def project_events(rows: list[dict], user: dict) -> list[dict]:
    caps = capabilities(user)
    result = []
    for row in rows:
        if EVENT_CAPABILITY.get(row["event_class"], "logs:view_audit") not in caps:
            continue
        item = dict(row)
        if isinstance(item.get("payload"), str):
            item["payload"] = json.loads(item["payload"])
        # An exception string can embed a request URL or financial payload.
        item.pop("process_error", None)
        result.append(redact(item, "fin:view" in caps))
    return result


def project_snapshot(payload: dict, user: dict) -> dict:
    caps = capabilities(user)
    result = redact(payload, "fin:view" in caps)
    result["role"] = user["role"]
    result["capabilities"] = sorted(caps)
    if "maint:view" not in caps:
        for key in ("wear", "deferred_repairs", "maintenance_queue", "fans", "lights"):
            result.pop(key, None)
    # Free-form messages are explicitly classified when recorded, never inspected heuristically.
    if "activity" in result:
        result["activity"] = [a for a in result["activity"]
                              if a.get("capability", "logs:view_audit") in caps]
    if "fin:view" not in caps:
        for session in result.get("sessions", []):
            if session.get("phase") == "CHARGED":
                session["phase"] = "AT_EXIT"
    return result
