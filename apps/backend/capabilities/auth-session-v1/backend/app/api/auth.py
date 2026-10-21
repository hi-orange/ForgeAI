"""Optional login/logout/me API. Bearer tokens are never placed in cookies or URLs."""

from datetime import UTC
from typing import Annotated

from app.core.auth import bearer, login, require_user, token_digest
from app.models.auth import AuthSession, AuthUser
from fastapi import APIRouter, Depends, Response
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.schemas.response import success

router = APIRouter(prefix="/auth", tags=["auth"])


class Credentials(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=128)


def user_payload(user: AuthUser) -> dict:
    return {"id": user.id, "username": user.username}


@router.post("/login")
def sign_in(body: Credentials, response: Response, db: Annotated[Session, Depends(get_db)]):
    user, token, expires = login(db, body.username, body.password)
    response.headers["Cache-Control"] = "no-store"
    return success(
        {
            "user": user_payload(user),
            "access_token": token,
            "token_type": "bearer",
            "expires_at": expires.replace(tzinfo=UTC).isoformat(),
        }
    )


@router.get("/me")
def current_user(response: Response, user: Annotated[AuthUser, Depends(require_user)]):
    response.headers["Cache-Control"] = "no-store"
    return success(user_payload(user))


@router.post("/logout")
def sign_out(
    response: Response,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[Session, Depends(get_db)],
):
    # Idempotent, and only revokes the presented token. There is no user-id input.
    if credentials is not None and len(credentials.credentials) <= 256:
        db.execute(
            delete(AuthSession).where(
                AuthSession.token_hash == token_digest(credentials.credentials)
            )
        )
        db.commit()
    response.headers["Cache-Control"] = "no-store"
    return success()
