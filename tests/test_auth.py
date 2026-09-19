import pytest

from app import auth


def setup_module():
    auth.ensure_schema_and_seed()


def test_hash_roundtrip_and_salt_differs():
    h1, s1 = auth.hash_password("secret")
    h2, s2 = auth.hash_password("secret")
    assert s1 != s2 and h1 != h2
    assert auth.verify_password("secret", h1, s1)
    assert not auth.verify_password("wrong", h1, s1)


def test_seed_users_exist_and_authenticate():
    assert auth.authenticate("admin", "admin123")["role"] == "admin"
    assert auth.authenticate("operator", "operator123")["role"] == "operator"
    assert auth.authenticate("admin", "nope") is None
    assert auth.authenticate("ghost", "x") is None


def test_session_lifecycle():
    user = auth.authenticate("operator", "operator123")
    token = auth.create_session(user["id"])
    assert auth.user_for_token(token)["username"] == "operator"
    auth.revoke_session(token)
    assert auth.user_for_token(token) is None
    assert auth.user_for_token(None) is None
    assert auth.user_for_token("garbage") is None


def test_create_and_delete_user_rules():
    admin = auth.authenticate("admin", "admin123")
    u = auth.create_user("night-shift", "pw12345", "operator")
    assert u["role"] == "operator"
    with pytest.raises(ValueError):
        auth.create_user("night-shift", "pw12345", "operator")   # duplicate
    with pytest.raises(ValueError):
        auth.create_user("x", "pw12345", "superuser")             # bad role
    with pytest.raises(ValueError):
        auth.delete_user(admin["id"], acting_user_id=admin["id"])  # self
    auth.delete_user(u["id"], acting_user_id=admin["id"])
    assert all(x["username"] != "night-shift" for x in auth.list_users())


def test_cannot_delete_last_admin():
    admin = auth.authenticate("admin", "admin123")
    op = auth.authenticate("operator", "operator123")
    with pytest.raises(ValueError):
        auth.delete_user(admin["id"], acting_user_id=op["id"])


def test_audit_roundtrip():
    auth.record_audit("admin", "POST", "/api/manual/sync", 200)
    row = auth.recent_audit(1)[0]
    assert row["path"] == "/api/manual/sync" and row["status"] == 200
