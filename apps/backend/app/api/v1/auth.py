from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.schemas.auth import (
    ChangePassword,
    TokenOut,
    UserLogin,
    UsernameUpdate,
    UserOut,
    UserRegister,
)
from app.schemas.response import ApiResponse, success
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=ApiResponse[UserOut])
def register(payload: UserRegister, db: DbSession) -> dict:
    user = auth_service.register_user(db, payload)
    return success(UserOut.model_validate(user))


@router.post("/login", response_model=ApiResponse[TokenOut])
def login(payload: UserLogin, db: DbSession) -> dict:
    token = auth_service.authenticate_user(db, payload)
    return success(token)


@router.get("/me", response_model=ApiResponse[UserOut])
def me(current_user: CurrentUser) -> dict:
    return success(UserOut.model_validate(current_user))


@router.patch("/username", response_model=ApiResponse[UserOut])
def update_username(
    payload: UsernameUpdate,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    user = auth_service.update_username(db, current_user, payload)
    return success(UserOut.model_validate(user))


@router.patch("/password", response_model=ApiResponse[None])
def change_password(
    payload: ChangePassword,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    auth_service.change_password(db, current_user, payload)
    return success(msg="密码已修改")
