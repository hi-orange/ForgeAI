from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.main import app
from app.services.auth import generate_username_from_email

client = TestClient(app)


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("secret123")
    assert hashed != "secret123"
    assert verify_password("secret123", hashed)
    assert not verify_password("wrong", hashed)


def test_create_access_token_contains_sub() -> None:
    token = create_access_token({"sub": "1"})
    payload = decode_access_token(token)
    assert payload["sub"] == "1"


def test_me_requires_auth() -> None:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == 401


def test_generate_username_from_email_local_part(monkeypatch) -> None:
    class FakeDb:
        pass

    def no_user(_db: Session, _username: str):
        return None

    monkeypatch.setattr("app.services.auth.get_user_by_username", no_user)
    assert generate_username_from_email(FakeDb(), "orange@example.com") == "orange"  # type: ignore[arg-type]


def test_register_and_login_with_email_only() -> None:
    email = f"emailonly_case_{uuid4().hex[:8]}@example.com"
    password = "secret123"

    register = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password},
    )
    assert register.status_code == 200, register.text
    body = register.json()
    assert body["code"] == 0
    assert body["data"]["email"] == email
    assert body["data"]["username"].startswith("emailonly_case")

    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login.status_code == 200, login.text
    assert login.json()["data"]["access_token"]


def test_register_rejects_username_only_payload() -> None:
    """Ensure API no longer requires username; email+password is enough."""
    email = f"no_username_needed_{uuid4().hex[:8]}@example.com"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "secret123"},
    )
    assert response.status_code == 200
    assert "username: Field required" not in response.text


def _register_and_login(email: str, password: str = "secret123") -> str:
    register = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password},
    )
    assert register.status_code == 200, register.text
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login.status_code == 200, login.text
    return login.json()["data"]["access_token"]


def test_update_username_requires_auth() -> None:
    response = client.patch("/api/v1/auth/username", json={"username": "newname"})
    assert response.status_code == 401
    assert response.json()["code"] == 401


def test_update_username_success() -> None:
    suffix = uuid4().hex[:8]
    token = _register_and_login(f"rename_ok_{suffix}@example.com")
    response = client.patch(
        "/api/v1/auth/username",
        json={"username": f"renamed_{suffix}"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["username"] == f"renamed_{suffix}"

    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.json()["data"]["username"] == f"renamed_{suffix}"


def test_update_username_rejects_edge_spaces() -> None:
    token = _register_and_login(f"rename_spaces_{uuid4().hex[:8]}@example.com")
    response = client.patch(
        "/api/v1/auth/username",
        json={"username": " spaced "},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


def test_update_username_rejects_taken_name() -> None:
    suffix = uuid4().hex[:8]
    token_a = _register_and_login(f"rename_taken_a_{suffix}@example.com")
    token_b = _register_and_login(f"rename_taken_b_{suffix}@example.com")

    me_a = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token_a}"})
    taken_username = me_a.json()["data"]["username"]

    response = client.patch(
        "/api/v1/auth/username",
        json={"username": taken_username},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert response.status_code == 400
    assert "占用" in response.json()["msg"]


def test_change_password_requires_auth() -> None:
    response = client.patch(
        "/api/v1/auth/password",
        json={"old_password": "secret123", "new_password": "secret456"},
    )
    assert response.status_code == 401
    assert response.json()["code"] == 401


def test_change_password_success() -> None:
    suffix = uuid4().hex[:8]
    email = f"pwd_ok_{suffix}@example.com"
    old_password = "secret123"
    new_password = "secret456"
    token = _register_and_login(email, old_password)

    response = client.patch(
        "/api/v1/auth/password",
        json={"old_password": old_password, "new_password": new_password},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["code"] == 0
    assert "密码" in response.json()["msg"]

    old_login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": old_password},
    )
    assert old_login.status_code == 401

    new_login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": new_password},
    )
    assert new_login.status_code == 200, new_login.text
    assert new_login.json()["data"]["access_token"]


def test_change_password_rejects_wrong_old_password() -> None:
    token = _register_and_login(f"pwd_wrong_{uuid4().hex[:8]}@example.com")
    response = client.patch(
        "/api/v1/auth/password",
        json={"old_password": "not-the-password", "new_password": "secret456"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400
    assert "当前密码" in response.json()["msg"]


def test_change_password_rejects_same_password() -> None:
    token = _register_and_login(f"pwd_same_{uuid4().hex[:8]}@example.com")
    response = client.patch(
        "/api/v1/auth/password",
        json={"old_password": "secret123", "new_password": "secret123"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


def test_change_password_rejects_short_new_password() -> None:
    token = _register_and_login(f"pwd_short_{uuid4().hex[:8]}@example.com")
    response = client.patch(
        "/api/v1/auth/password",
        json={"old_password": "secret123", "new_password": "123"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
