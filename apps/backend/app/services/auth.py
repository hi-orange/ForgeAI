from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException, UnauthorizedException
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User
from app.schemas.auth import TokenOut, UserLogin, UserRegister


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.scalar(select(User).where(User.username == username))


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == email))


def get_user_by_id(db: Session, user_id: int) -> User | None:
    return db.scalar(select(User).where(User.id == user_id))


def register_user(db: Session, payload: UserRegister) -> User:
    if get_user_by_username(db, payload.username):
        raise BusinessException("用户名已存在")
    if get_user_by_email(db, payload.email):
        raise BusinessException("邮箱已被注册")

    user = User(
        username=payload.username,
        email=payload.email,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: Session, payload: UserLogin) -> TokenOut:
    user = get_user_by_username(db, payload.username)
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise UnauthorizedException("用户名或密码错误")

    token = create_access_token({"sub": str(user.id)})
    return TokenOut(access_token=token)
