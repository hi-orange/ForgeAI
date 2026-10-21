"""Local credential verification. Never store raw passwords or bearer tokens."""

import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from app.models.auth import AuthSession, AuthUser
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import case, delete, select, update
from sqlalchemy.orm import Session

from app.db.database import get_db

PASSWORD_ROUNDS = 600_000
SESSION_LIFETIME = timedelta(hours=8)
LOGIN_LOCKOUT = timedelta(minutes=5)
MAX_LOGIN_FAILURES = 5
bearer = HTTPBearer(auto_error=False)


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def normalize_username(value: str) -> str:
    username = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.@+\-]{2,119}", username):
        raise ValueError("Username must contain 3–120 letters, digits or . _ @ + -")
    return username


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 128:
        raise ValueError("Password must contain 12–128 characters")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PASSWORD_ROUNDS)
    return f"pbkdf2_sha256${PASSWORD_ROUNDS}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    if not 1 <= len(password) <= 128:
        return False
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or int(rounds) != PASSWORD_ROUNDS:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


# Constant dummy input prevents an absent account from skipping password work.
_DUMMY_HASH = f"pbkdf2_sha256${PASSWORD_ROUNDS}${'0' * 32}${'0' * 64}"


def create_user(db: Session, username: str, password: str) -> AuthUser:
    """Called only by approved provisioning/registration code; no public signup route."""
    user = AuthUser(username=normalize_username(username), password_hash=hash_password(password))
    db.add(user)
    db.flush()
    return user


def unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="Invalid credentials or session")


def login(db: Session, username: str, password: str) -> tuple[AuthUser, str, datetime]:
    now = utc_now()
    try:
        normalized = normalize_username(username)
    except ValueError:
        normalized = ""
    user = db.scalar(select(AuthUser).where(AuthUser.username == normalized).with_for_update())
    verified = verify_password(password, user.password_hash if user else _DUMMY_HASH)
    if user is None or not user.active or (user.locked_until and user.locked_until > now):
        raise unauthorized()
    if not verified:
        attempts = case((AuthUser.locked_until <= now, 1), else_=AuthUser.failed_logins + 1)
        db.execute(
            update(AuthUser)
            .where(AuthUser.id == user.id)
            .values(
                failed_logins=attempts,
                locked_until=case(
                    (attempts >= MAX_LOGIN_FAILURES, now + LOGIN_LOCKOUT), else_=None
                ),
            )
        )
        db.commit()
        raise unauthorized()
    user.failed_logins = 0
    user.locked_until = None
    # Expired sessions are no longer useful; current sessions remain independent.
    db.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
    token = secrets.token_urlsafe(32)
    expires = now + SESSION_LIFETIME
    db.add(AuthSession(token_hash=token_digest(token), user_id=user.id, expires_at=expires))
    db.commit()
    return user, token, expires


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def require_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> AuthUser:
    if credentials is None or len(credentials.credentials) > 256:
        raise unauthorized()
    user = db.scalar(
        select(AuthUser)
        .join(AuthSession, AuthSession.user_id == AuthUser.id)
        .where(
            AuthSession.token_hash == token_digest(credentials.credentials),
            AuthSession.expires_at > utc_now(),
            AuthUser.active.is_(True),
        )
    )
    if user is None:
        raise unauthorized()
    return user
