from fastapi.testclient import TestClient

from app.core.security import create_access_token, hash_password, verify_password
from app.main import app

client = TestClient(app)


def test_health_check() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("secret123")
    assert hashed != "secret123"
    assert verify_password("secret123", hashed)
    assert not verify_password("wrong", hashed)


def test_create_access_token_contains_sub() -> None:
    from app.core.security import decode_access_token

    token = create_access_token({"sub": "1"})
    payload = decode_access_token(token)
    assert payload["sub"] == "1"


def test_me_requires_auth() -> None:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == 401
