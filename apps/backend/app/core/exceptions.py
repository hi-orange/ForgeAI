from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.response import fail

logger = logging.getLogger("forgeai")


class AppException(Exception):
    def __init__(
        self,
        msg: str = "请求失败",
        *,
        code: int | None = None,
        status_code: int = 400,
        data: Any = None,
    ) -> None:
        self.msg = msg
        self.status_code = status_code
        self.code = code if code is not None else status_code
        self.data = data
        super().__init__(msg)


class BusinessException(AppException):
    """可预期的业务错误（参数不合法、资源冲突等）。"""

    def __init__(self, msg: str, *, code: int = 400, data: Any = None) -> None:
        super().__init__(msg, code=code, status_code=400, data=data)


class UnauthorizedException(AppException):
    def __init__(self, msg: str = "未登录或登录已过期") -> None:
        super().__init__(msg, code=401, status_code=401)


class NotFoundException(AppException):
    def __init__(self, msg: str = "资源不存在") -> None:
        super().__init__(msg, code=404, status_code=404)


def _detail_to_msg(detail: Any) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        parts: list[str] = []
        for item in detail:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and "msg" in item:
                parts.append(str(item["msg"]))
            else:
                parts.append(str(item))
        return "；".join(parts) if parts else "请求失败"
    if detail is None:
        return "请求失败"
    return str(detail)


def _format_validation_message(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "请求参数错误"

    first = errors[0]
    loc = [str(part) for part in first.get("loc", []) if part != "body"]
    field = ".".join(loc)
    message = first.get("msg", "请求参数错误")
    return f"{field}: {message}" if field else str(message)


async def app_exception_handler(_: Request, exc: AppException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=fail(code=exc.code, msg=exc.msg, data=exc.data),
    )


async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=fail(code=exc.status_code, msg=_detail_to_msg(exc.detail)),
    )


async def starlette_http_exception_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=fail(code=exc.status_code, msg=_detail_to_msg(exc.detail)),
    )


async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=fail(code=422, msg=_format_validation_message(exc)),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "Unhandled exception on %s %s: %s",
        request.method,
        request.url.path,
        exc,
    )
    return JSONResponse(
        status_code=500,
        content=fail(code=500, msg="服务器异常，请稍后重试"),
    )


def register_exception_handlers(app: Any) -> None:
    app.add_exception_handler(AppException, app_exception_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(StarletteHTTPException, starlette_http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
