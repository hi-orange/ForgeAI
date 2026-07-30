from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class UserRegister(BaseModel):
    """注册只需邮箱和密码，username 由后端根据邮箱自动生成。"""

    model_config = ConfigDict(extra="ignore")

    email: EmailStr
    password: str = Field(min_length=6, max_length=128)


class UserLogin(BaseModel):
    """登录只需邮箱和密码。"""

    model_config = ConfigDict(extra="ignore")

    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: EmailStr
    avatar: str | None = None
    created_at: datetime
    updated_at: datetime


class UsernameUpdate(BaseModel):
    username: str = Field(min_length=3, max_length=35)

    @field_validator("username")
    @classmethod
    def no_leading_or_trailing_spaces(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("用户名开头和结尾不能有空格")
        return value


class ChangePassword(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)

    @model_validator(mode="after")
    def passwords_must_differ(self) -> "ChangePassword":
        if self.old_password == self.new_password:
            raise ValueError("新密码不能与当前密码相同")
        return self
