"""Security boundaries: independent matrix, runtime route coverage and live role changes."""
import asyncio
import json
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from app import auth, db, main, policy, tariffs
from app.ws_manager import ConnectionManager


EXPECTED = {
    "admin": set(policy.CAPABILITIES),
    "auditor": {"ops:view", "fin:view", "fin:write_tariff", "logs:view_fin"},
    "facility_operator": {"ops:view", "ops:control_gates", "maint:view", "logs:view_ops", "logs:view_maint"},
    "maintenance_technician": {"ops:view", "maint:view", "logs:view_ops", "logs:view_maint"},
}
ACCOUNTS = {"admin": ("admin", "admin123"), "auditor": ("auditor", "auditor123"),
            "facility_operator": ("operator", "operator123"),
            "maintenance_technician": ("technician", "technician123")}


def client(role):
    c = TestClient(main.app, follow_redirects=False)
    username, password = ACCOUNTS[role]
    assert c.post("/api/auth/login", json={"username": username, "password": password}).status_code == 200
    return c


@pytest.mark.parametrize("role", EXPECTED)
@pytest.mark.parametrize("capability", sorted(policy.CAPABILITIES))
def test_full_capability_matrix(role, capability):
    assert policy.has({"role": role}, capability) == (capability in EXPECTED[role])


def routes(router):
    for route in router.routes:
        if hasattr(route, "original_router"):
            yield from routes(route.original_router)
        else:
            yield route


def test_all_runtime_routes_have_explicit_policy():
    seen = set()
    for route in routes(main.app):
        path = getattr(route, "path", None)
        if path is None:
            continue
        path = re.sub(r"\{user_id\}|\{ghost_id\}", "1", path)
        path = re.sub(r"\{[^}]+\}", "sample", path)
        for method in getattr(route, "methods", None) or ({"GET"} if path == "/static" else {"WEBSOCKET"}):
            assert policy.required_capabilities(path, method) is not None, (method, path)
            seen.add((method, path))
    assert ("PUT", "/api/tariffs") in seen
    assert ("WEBSOCKET", "/ws/live") in seen


@pytest.mark.parametrize("role,method,path", [
    ("auditor", "POST", "/api/barriers/gateA/open"),
    ("auditor", "POST", "/api/ghost-car/override"),
    ("facility_operator", "GET", "/api/payments"),
    ("facility_operator", "GET", "/api/penalties"),
    ("facility_operator", "POST", "/api/manual/repair/S1"),
    ("facility_operator", "PUT", "/api/tariffs"),
    ("maintenance_technician", "POST", "/api/manual/fan/fan0/on"),
    ("maintenance_technician", "POST", "/api/barriers/gateA/close"),
    ("maintenance_technician", "POST", "/api/manual/repair/S1"),
    ("auditor", "POST", "/api/admin/users"),
    ("auditor", "DELETE", "/api/logs"),
    ("auditor", "DELETE", "/api/admin/data/payments"),
    ("facility_operator", "DELETE", "/api/admin/data/history"),
    ("maintenance_technician", "DELETE", "/api/admin/data/penalties"),
])
def test_negative_routes(role, method, path):
    assert client(role).request(method, path, json={}).status_code == 403


def test_unknown_routes_deny_non_admin_and_wrong_methods(caplog):
    assert client("facility_operator").get("/api/surprise").status_code == 403
    assert client("auditor").post("/api/stats").status_code == 403
    assert client("admin").get("/api/surprise").status_code == 404
    assert "Unmapped route" in caplog.text


def test_financial_fields_absent_in_operator_http_and_ws():
    main.state.log_activity("Invoice amount 88.00", capability="logs:view_fin")
    op = client("facility_operator")
    for url in ("/api/state", "/api/stats", "/api/history", "/api/reports/daily", "/api/ghost-cars", "/healthz"):
        payload = op.get(url).json()
        text = json.dumps(payload).lower()
        for key in policy.FINANCIAL_KEYS:
            assert f'"{key}":' not in text, (url, key)
        assert "88.00" not in text
    with op.websocket_connect("/ws/live") as ws:
        frame = ws.receive_json()
        assert "penalties" not in frame
        assert "88.00" not in json.dumps(frame)


def test_event_class_filter_and_recursive_signature_redaction():
    prefix = uuid.uuid4().hex[:12]
    for kind in ("payment_made", "component_fixed", "car_spot_action"):
        db.record_event({"EventId": prefix + kind, "EventClass": kind, "Signature": "secret",
                         "CarPlateNumber": prefix, "Amount": 99, "RepairCost": 12,
                         "Nested": {"Signature": "nested"}}, True)
    op = client("facility_operator")
    assert op.get("/api/events?event_class=payment_made").json() == []
    events = op.get("/api/events?event_class=component_fixed").json()
    assert events and "RepairCost" not in events[0]["payload"]
    for role in EXPECTED:
        data = client(role).get("/api/events").json()
        assert '"Signature"' not in json.dumps(data)
        assert '"signature"' not in json.dumps(data)
    timeline = op.get("/api/history/timeline", params={"plate": prefix}).json()
    assert all(r["event_class"] != "payment_made" for r in timeline)


def test_tariff_write_isolation_and_validation():
    before = tariffs.effective()
    auditor = client("auditor")
    try:
        assert auditor.put("/api/tariffs", json={"parking_rate_per_minute": 2.75}).status_code == 200
        assert tariffs.effective()["parking_rate_per_minute"] == 2.75
        for changes in ({"minimum_charge": -1}, {"billing_basis": "guess"}, {"billing_rounding": "floor"},
                        {"autopilot": True}, {"electric_split_charging": "yes"}, {"minimum_charge": True}):
            assert auditor.put("/api/tariffs", json=changes).status_code == 400
        assert tariffs.effective()["minimum_charge"] == before["minimum_charge"]
        assert auditor.get("/api/admin/users").status_code == 403
        details = [r for r in auth.recent_audit(100) if r["target"] == "tariff_settings"]
        assert any("before" in json.loads(r["detail"]) for r in details)
    finally:
        tariffs.update(before, "test cleanup")


def test_role_change_immediate_for_existing_cookie_and_websocket():
    name = "role-" + uuid.uuid4().hex
    user = auth.create_user(name, "secret123", "auditor")
    token = auth.create_session(user["id"])
    c = TestClient(main.app)
    c.cookies.set(auth.COOKIE_NAME, token)
    assert c.get("/api/payments").status_code == 200
    frames = []
    class Socket:
        cookies = {auth.COOKIE_NAME: token}
        async def send_json(self, value):
            frames.append(value)
        async def close(self, code):
            frames.append({"closed": code})
    manager = ConnectionManager()
    manager._connections.add(Socket())
    async def scenario():
        await manager.broadcast({"penalties": {"total_fines": 10}})
        auth.update_role(user["id"], "facility_operator")
        await manager.broadcast({"penalties": {"total_fines": 10}})
        auth.revoke_session(token)
        await manager.broadcast({"penalties": {"total_fines": 10}})
    try:
        asyncio.run(scenario())
        assert "penalties" in frames[0] and "penalties" not in frames[1]
        assert frames[2] == {"closed": 4401}
        assert not manager.count
        token = auth.create_session(user["id"])
        c.cookies.set(auth.COOKIE_NAME, token)
        assert c.get("/api/payments").status_code == 403
    finally:
        auth.delete_user(user["id"], auth.authenticate("admin", "admin123")["id"])


def test_last_admin_cannot_be_demoted():
    admin = auth.authenticate("admin", "admin123")
    with pytest.raises(ValueError, match="last admin"):
        auth.update_role(admin["id"], "auditor")


def test_logs_paginate_and_reject_wrong_tabs():
    auditor = client("auditor")
    assert auditor.get("/api/logs?tab=operations").status_code == 403
    result = auditor.get("/api/logs?tab=financial&size=1").json()
    assert result["tabs"] == ["financial"] and len(result["items"]) <= 1
    assert client("facility_operator").get("/api/logs?tab=audit").status_code == 403


def test_login_returns_exact_last_three_attempts():
    c = TestClient(main.app)
    for _ in range(3):
        c.post("/api/auth/login", json={"username": "auditor", "password": "incorrect"})
    result = c.post("/api/auth/login", json={"username": "auditor", "password": "auditor123"}).json()
    assert len(result["prior_attempts"]) == 3
    assert all(not r["success"] and r["ip"] for r in result["prior_attempts"])


def test_schema_capability_and_additive_migration():
    column = "report_" + uuid.uuid4().hex[:8]
    body = {"table": "sessions", "column": column, "type": "TEXT"}
    assert client("auditor").post("/api/admin/schema", json=body).status_code == 403
    admin = client("admin")
    result = admin.post("/api/admin/schema", json=body)
    assert result.status_code == 200
    assert any(c["name"] == column for c in result.json()["columns"])
    assert admin.post("/api/admin/schema", json={**body, "column": 'x; DROP TABLE sessions'}).status_code == 422


def test_role_migration_preserves_existing_sessions():
    name = "legacy-" + uuid.uuid4().hex[:8]
    user = auth.create_user(name, "secret123", "facility_operator")
    token = auth.create_session(user["id"])
    auth._write("UPDATE dashboard_users SET role='operator' WHERE id=?", (user["id"],))
    try:
        auth.ensure_schema_and_seed()
        assert auth.user_for_token(token)["role"] == "facility_operator"
    finally:
        auth.delete_user(user["id"], auth.authenticate("admin", "admin123")["id"])


def test_all_new_pages_and_assets_render():
    for role in EXPECTED:
        c = client(role)
        for path in ("/", "/history", "/logs", "/reports"):
            assert c.get(path).status_code == 200, (role, path)
        if "fin:view" in EXPECTED[role]:
            for path in ("/tariffs", "/payments", "/penalties"):
                assert c.get(path).status_code == 200
    for name in ("logs", "tariffs", "reports", "penalties"):
        assert TestClient(main.app).get(f"/static/js/pages/{name}.js").status_code == 200


def test_admin_clears_one_section_and_leaves_live_cars_alone():
    with db._lock, db._conn:
        db._conn.execute("INSERT INTO penalties (event_id, reason, fine_amount, type, component_name, server_datetime) "
                         "VALUES (?, 'Car escaped without paying', 10, 'Car', 'CLR 001', '2026-09-19 23:00:00')", (uuid.uuid4().hex,))
        db._conn.execute("INSERT INTO sessions (plate, car_type, completed_at) VALUES ('CLR 002', 'Normal', '2026-09-19T15:00:00+00:00')")
        db._conn.execute("INSERT INTO active_sessions (plate, session_id, payload, charge_attempted) VALUES ('CLR 003', ?, '{}', 0)",
                         (uuid.uuid4().hex,))
    main.state.record_penalty("Car escaped without paying", 10.0, "Car", "CLR 001")
    admin = client("admin")
    result = admin.delete("/api/admin/data/penalties").json()
    assert result["ok"] and result["removed"] >= 1
    assert db.query("SELECT COUNT(*) AS n FROM penalties")[0]["n"] == 0
    assert main.state.penalty_count == 0 and main.state.total_fines == 0 and not main.state.penalty_log
    assert db.query("SELECT COUNT(*) AS n FROM sessions WHERE plate = 'CLR 002'")[0]["n"] == 1
    assert admin.delete("/api/admin/data/history").json()["ok"]
    assert db.query("SELECT COUNT(*) AS n FROM sessions")[0]["n"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM active_sessions WHERE plate = 'CLR 003'")[0]["n"] == 1
    assert admin.delete("/api/admin/data/events").status_code == 404
    audit = auth._all("SELECT path FROM audit_log WHERE path LIKE '/api/admin/data/%'", ())
    assert {"/api/admin/data/penalties", "/api/admin/data/history"} <= {row["path"] for row in audit}
