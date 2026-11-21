"""Error responses: RFC 9457 problem+json with a stable `code` (NFR-5)."""

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from courtlock.errors import CourtLockError

logger = logging.getLogger("courtlock")
PROBLEM_JSON = "application/problem+json"


def problem(status: int, code: str, detail: str, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "code": code,
        "detail": detail,
    }
    body.update(extra)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON)


async def _courtlock_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, CourtLockError)
    return problem(exc.status_code, exc.code, exc.detail, **exc.extra)


async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    detail = "; ".join(
        f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
    )
    return problem(422, "validation_error", detail)


async def _unexpected_error(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error", exc_info=exc)
    return problem(500, "internal_error", "internal error")  # never leak SQL or internals


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(CourtLockError, _courtlock_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unexpected_error)
