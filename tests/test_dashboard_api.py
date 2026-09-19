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
    result = _login("operator", "operator123").get("/api/me").json()
    assert result["role"] == "facility_operator"
    assert "fin:view" not in result["capabilities"]


def test_history_filters_and_paging():
    _seed_sessions()
    c = _login("auditor", "auditor123")
    everything = c.get("/api/history/search", params={"size": 2}).json()
    assert everything["total"] >= 3 and len(everything["items"]) == 2
    hit = c.get("/api/history/search", params={"plate": "wct759"}).json()["items"]
    assert hit and hit[0]["plate"] == "WCT 759"
    suspect = c.get("/api/history/search", params={"status": "suspect"}).json()["items"]
    assert suspect and all(r["payment_ok"] == 0 for r in suspect)
    assert c.get("/api/history/search", params={"size": 999}).json()["size"] == 100
    window = c.get("/api/history/search", params={"from": "2026-09-19T01:00:00", "to": "2026-09-19T01:59:59"}).json()
    assert [r["plate"] for r in window["items"]] == ["ABC 123"]


def test_stats_hides_finance_from_operator():
    assert "revenue" not in _login("operator", "operator123").get("/api/stats").json()
    assert "revenue" in _login("admin", "admin123").get("/api/stats").json()


def test_admin_user_management():
    admin = _login("admin", "admin123")
    created = admin.post("/api/admin/users", json={"username": "temp", "password": "temp123", "role": "facility_operator"})
    assert created.status_code == 201
    dup = admin.post("/api/admin/users", json={"username": "temp", "password": "temp123", "role": "facility_operator"})
    assert dup.status_code == 400 and "already exists" in dup.json()["detail"]
    assert admin.delete(f"/api/admin/users/{created.json()['id']}").status_code == 200


def test_twin_geometry_and_gate_bays():
    c = _login("operator", "operator123")
    geo = c.get("/api/twin").json()
    assert set(geo) >= {"level", "zones", "spots", "gates", "fans", "lights", "bounds"}
    bays = TestClient(app).get("/api/gate/bays").json()
    assert isinstance(bays, list)


def test_pages_render_for_logged_in_user():
    c = _login("admin", "admin123")
    for path in ("/", "/history", "/payments", "/admin"):
        assert c.get(path).status_code == 200, path
    assert TestClient(app).get("/login").status_code == 200
