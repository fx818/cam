from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

_STATUS_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    410: "GONE",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMITED",
    500: "INTERNAL",
}


@dataclass(frozen=True, slots=True)
class ApiProblem(Exception):
    code: str
    message: str
    status: int = 400

    def __str__(self) -> str:
        return self.message


def envelope(code: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message}}


def error_response(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(envelope(code, message), status_code=status)


async def api_problem_handler(_request: Request, exc: ApiProblem) -> JSONResponse:
    return error_response(exc.code, exc.message, exc.status)


async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    status = exc.status_code
    code = _STATUS_CODES.get(status, "ERROR")
    detail = str(exc.detail) if exc.detail else ""
    message = detail or code.replace("_", " ").lower().capitalize()
    return error_response(code, message, status)


async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    errors: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        field = loc or "body"
        errors.append(f"{field}: {error.get('msg', 'invalid')}")
    message = "; ".join(errors) if errors else "Validation failed."
    return error_response("VALIDATION_ERROR", message, 422)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApiProblem, api_problem_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(
        RequestValidationError,
        validation_exception_handler,  # type: ignore[arg-type]
    )


def internal_error(message: str) -> dict[str, Any]:
    return envelope("INTERNAL", message)
