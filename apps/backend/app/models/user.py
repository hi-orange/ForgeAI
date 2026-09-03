from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class User(Base):
    """平台登录用户。"""

    __tablename__ = "user"
    __table_args__ = ({"comment": "平台登录用户"},)

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
        autoincrement=True,
        comment="内部自增主键",
    )
    username: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        comment="登录用户名，唯一",
    )
    hashed_password: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="密码哈希，不明文存储",
    )
    email: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        index=True,
        comment="邮箱，唯一",
    )
    avatar: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        comment="头像 URL，可空",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        nullable=False,
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="最后更新时间",
    )
