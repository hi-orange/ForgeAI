from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.schemas.auth import TokenOut, UserLogin, UserOut, UserRegister
from app.schemas.response import ApiResponse, ok
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=ApiResponse[UserOut])
def register(payload: UserRegister, db: DbSession) -> dict:
    user = auth_service.register_user(db, payload)
    return ok(UserOut.model_validate(user))


@router.post("/login", response_model=ApiResponse[TokenOut])
def login(payload: UserLogin, db: DbSession) -> dict:
    token = auth_service.authenticate_user(db, payload)
    return ok(token)


@router.get("/me", response_model=ApiResponse[UserOut])
def me(current_user: CurrentUser) -> dict:
    return ok(UserOut.model_validate(current_user))
