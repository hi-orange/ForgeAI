"""Run in an installed bundle's isolated backend, never the ForgeAI app namespace."""

from datetime import timedelta

from alembic.config import Config
from app.core.auth import create_user, token_digest, utc_now, verify_password
from app.models.auth import AuthSession, AuthUser
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from alembic import command
from app.db.database import SessionLocal, engine
from app.main import app

config = Config("alembic.ini")
command.upgrade(config, "head")
assert {"auth_users", "auth_sessions"} <= set(inspect(engine).get_table_names())
password = "test-only-strong-password"
with SessionLocal() as db:
    alice = create_user(db, "Alice", password)
    bob = create_user(db, "bob", password)
    db.commit()
    alice_id, bob_id = alice.id, bob.id
    assert alice.password_hash != password
    assert alice.password_hash != bob.password_hash
    assert verify_password(password, alice.password_hash)
    assert not verify_password("wrong", alice.password_hash)
    try:
        create_user(db, "short", "weak")
    except ValueError:
        pass
    else:
        raise AssertionError("weak password accepted")

with TestClient(app) as client:
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/auth/me?access_token=spoofed").status_code == 401
    assert client.post("/api/v1/auth/register", json={}).status_code == 404

    def sign_in(username):
        response = client.post(
            "/api/v1/auth/login", json={"username": username, "password": password}
        )
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert "set-cookie" not in response.headers
        data = response.json()["data"]
        assert "password_hash" not in data["user"]
        return data["access_token"]

    alice_token, bob_token = sign_in("alice"), sign_in("bob")
    alice_headers = {"Authorization": f"Bearer {alice_token}"}
    bob_headers = {"Authorization": f"Bearer {bob_token}"}
    assert client.get("/api/v1/auth/me", headers=alice_headers).json()["data"]["id"] == alice_id
    assert client.get("/api/v1/auth/me", headers=bob_headers).json()["data"]["id"] == bob_id
    # Cookie credentials are ignored; an ambient cross-site request cannot act as a user.
    client.cookies.set("access_token", alice_token)
    assert client.get("/api/v1/auth/me").status_code == 401
    with SessionLocal() as db:
        session = db.scalar(select(AuthSession).where(AuthSession.user_id == alice_id))
        assert session.token_hash == token_digest(alice_token)
        assert session.token_hash != alice_token
        assert session.expires_at > utc_now()
    assert client.post("/api/v1/auth/logout", headers=alice_headers).status_code == 200
    assert client.post("/api/v1/auth/logout", headers=alice_headers).status_code == 200
    assert client.get("/api/v1/auth/me", headers=alice_headers).status_code == 401
    assert client.get("/api/v1/auth/me", headers=bob_headers).status_code == 200
    with SessionLocal() as db:
        session = db.get(AuthSession, token_digest(bob_token))
        session.expires_at = utc_now() - timedelta(seconds=1)
        db.commit()
    assert client.get("/api/v1/auth/me", headers=bob_headers).status_code == 401
    with SessionLocal() as db:
        user = db.get(AuthUser, bob_id)
        user.active = False
        db.commit()
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "bob", "password": password}
        ).status_code
        == 401
    )
    unknown = client.post("/api/v1/auth/login", json={"username": "unknown", "password": password})
    for _ in range(5):
        wrong = client.post("/api/v1/auth/login", json={"username": "alice", "password": "wrong"})
        assert wrong.status_code == 401
        assert wrong.json() == unknown.json()
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "alice", "password": password}
        ).status_code
        == 401
    )
    with SessionLocal() as db:
        user = db.get(AuthUser, alice_id)
        user.locked_until = utc_now() - timedelta(seconds=1)
        db.commit()
    recovered_token = sign_in("alice")
    assert recovered_token not in {alice_token, bob_token}

command.downgrade(config, "0001_create_app_meta")
assert "auth_users" not in inspect(engine).get_table_names()
print("AUTH_CAPABILITY_RUNTIME_OK")
