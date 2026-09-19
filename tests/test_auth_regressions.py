"""Regression coverage for authentication bookkeeping and transaction recovery."""
import uuid

import pytest
from fastapi.testclient import TestClient

from app import auth, main


def test_whitespace_login_attempts_share_canonical_history():
    username = "login-history-" + uuid.uuid4().hex[:12]
    user = auth.create_user(username, "secret123", "auditor")
    client = TestClient(main.app)
    try:
        for _ in range(3):
            assert client.post("/api/auth/login", json={
                "username": f" {username} ", "password": "incorrect",
            }).status_code == 401
        response = client.post("/api/auth/login", json={
            "username": username, "password": "secret123",
        })
        assert response.status_code == 200
        attempts = response.json()["prior_attempts"]
        assert len(attempts) == 3
        assert all(row["username"] == username and not row["success"] for row in attempts)
    finally:
        auth.delete_user(user["id"], auth.authenticate("admin", "admin123")["id"])


def test_duplicate_username_does_not_poison_next_transaction():
    username = "duplicate-" + uuid.uuid4().hex[:12]
    user = auth.create_user(username, "secret123", "auditor")
    try:
        with pytest.raises(ValueError, match="already exists"):
            auth.create_user(username, "secret123", "auditor")
        assert not auth._conn.in_transaction
        assert auth.update_role(user["id"], "facility_operator")["role"] == "facility_operator"
    finally:
        if auth._conn.in_transaction:
            auth._conn.rollback()
        auth.delete_user(user["id"], auth.authenticate("admin", "admin123")["id"])
