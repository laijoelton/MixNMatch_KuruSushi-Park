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
    assert auth.required_capabilities("/webhooks/simulator", "POST") == frozenset()
    assert auth.required_capabilities("/api/state") == {"ops:view"}
    assert auth.required_capabilities("/api/manual/barrier/g1/open", "POST") == {"ops:control_gates"}
    assert auth.required_capabilities("/api/manual/sync", "POST") == {"ops:control_dispatch"}
    assert auth.required_capabilities("/api/new-unmapped-route") is None


def test_anonymous_is_blocked():
    c = _client()
    assert c.get("/api/state").status_code == 401
    page = c.get("/")
    assert page.status_code == 302 and page.headers["location"].startswith("/login")
    assert c.get("/healthz").status_code == 200
    assert c.get("/api/gate/bays").status_code == 200


def test_operator_vs_admin():
    op = _client("operator", "operator123")
    assert op.get("/api/state").status_code == 200
    assert op.post("/api/manual/sync").status_code == 403
    assert op.get("/api/admin/users").status_code == 403
    denied_page = op.get("/admin")
    assert denied_page.status_code == 302 and "denied" in denied_page.headers["location"]
    admin = _client("admin", "admin123")
    assert admin.get("/api/admin/users").status_code == 200


def test_websocket_requires_login():
    import pytest
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with _client().websocket_connect("/ws/live") as ws:
            ws.receive_json()
    with _client("operator", "operator123").websocket_connect("/ws/live") as ws:
        assert ws.receive_json()["type"] == "hello"


def test_mutations_are_audited():
    admin = _client("admin", "admin123")
    admin.post("/api/auth/logout")
    assert auth.recent_audit(1)[0]["path"] == "/api/auth/logout"
