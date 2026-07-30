import random
import re
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException, UnauthorizedException
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User
from app.schemas.auth import ChangePassword, TokenOut, UserLogin, UsernameUpdate, UserRegister


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.scalar(select(User).where(User.username == username))


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == email))


def get_user_by_id(db: Session, user_id: int) -> User | None:
    return db.scalar(select(User).where(User.id == user_id))


def _email_local_part(email: str) -> str:
    return email.split("@", 1)[0]


def generate_username_from_email(db: Session, email: str) -> str:
    local = _email_local_part(email)
    base = re.sub(r"[^a-zA-Z0-9_]", "_", local).strip("_") or "user"
    base = base[:80]
    if len(base) < 3:
        base = f"{base}user"[:3]

    if get_user_by_username(db, base) is None:
        return base

    for _ in range(8):
        candidate = f"{base}{random.randint(1000, 9999)}"[:100]
        if get_user_by_username(db, candidate) is None:
            return candidate

    candidate = f"{base}{int(time.time())}"[:100]
    if get_user_by_username(db, candidate) is None:
        return candidate

    raise BusinessException("无法生成唯一用户名，请稍后重试")


def register_user(db: Session, payload: UserRegister) -> User:
    email = payload.email.lower()
    if get_user_by_email(db, email):
        raise BusinessException("邮箱已被注册")

    username = generate_username_from_email(db, email)
    user = User(
        username=username,
        email=email,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: Session, payload: UserLogin) -> TokenOut:
    email = payload.email.lower()
    user = get_user_by_email(db, email)
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise UnauthorizedException("邮箱或密码错误")

    token = create_access_token({"sub": str(user.id)})
    return TokenOut(access_token=token)


def update_username(db: Session, user: User, payload: UsernameUpdate) -> User:
    new_username = payload.username
    if new_username == user.username:
        return user

    existing = get_user_by_username(db, new_username)
    if existing is not None and existing.id != user.id:
        raise BusinessException("用户名已被占用")

    user.username = new_username
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def change_password(db: Session, user: User, payload: ChangePassword) -> None:
    if not verify_password(payload.old_password, user.hashed_password):
        raise BusinessException("当前密码不正确")

    if verify_password(payload.new_password, user.hashed_password):
        raise BusinessException("新密码不能与当前密码相同")

    user.hashed_password = hash_password(payload.new_password)
    db.add(user)
    db.commit()
    db.refresh(user)
