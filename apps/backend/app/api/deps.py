from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.exceptions import UnauthorizedException
from app.core.security import decode_access_token
from app.db.database import get_db
from app.models.user import User
from app.services import auth as auth_service

bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise UnauthorizedException()

    payload = decode_access_token(credentials.credentials)
    user_id = payload.get("sub")
    if user_id is None:
        raise UnauthorizedException("Token无效")

    try:
        parsed_id = int(user_id)
    except (TypeError, ValueError) as exc:
        raise UnauthorizedException("Token无效") from exc

    user = auth_service.get_user_by_id(db, parsed_id)
    if user is None:
        raise UnauthorizedException("用户不存在或已被删除")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
