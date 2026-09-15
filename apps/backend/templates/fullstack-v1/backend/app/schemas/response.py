from typing import Any

from pydantic import BaseModel


class ApiResponse[T](BaseModel):
    code: int = 0
    msg: str = "ok"
    data: T | None = None


def success(data: Any = None, *, msg: str = "ok") -> dict[str, Any]:
    if isinstance(data, BaseModel):
        data = data.model_dump(mode="json")
    return {"code": 0, "msg": msg, "data": data}
