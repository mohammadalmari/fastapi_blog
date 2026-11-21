"""
Errors the app raises on purpose, and how they become HTTP responses.

Services raise these instead of FastAPI's ``HTTPException``, so the business
logic doesn't depend on the web framework and can be called from a script or
a test without one. The handlers at the bottom do the translation.
"""

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel


class AppError(Exception):
    """Base class. Subclasses set the status code; the message is ``detail``."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    headers: dict[str, str] | None = None

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class UnauthorizedError(AppError):
    """Not authenticated: no credentials, or ones we don't accept."""

    status_code = status.HTTP_401_UNAUTHORIZED
    # The HTTP spec asks a 401 to say how to authenticate.
    headers = {"WWW-Authenticate": "Bearer"}  # noqa: RUF012 - a constant, never mutated


class ForbiddenError(AppError):
    """Authenticated, or not needing to be, but not allowed to do this."""

    status_code = status.HTTP_403_FORBIDDEN


class NotFoundError(AppError):
    """The thing asked for doesn't exist (or the caller may not know it does)."""

    status_code = status.HTTP_404_NOT_FOUND


class ConflictError(AppError):
    """The request clashes with what already exists, e.g. a taken username."""

    status_code = status.HTTP_409_CONFLICT


class FieldError(AppError):
    """
    A request field failed a check that Pydantic couldn't make, because it
    depends on settings or on the database. Rendered in the same shape as
    FastAPI's own validation errors, so clients handle one format.
    """

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT

    def __init__(self, field: str, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.field = field
        self.problems = problems


class ErrorResponse(BaseModel):
    """The body of every error except validation errors. For the API docs."""

    detail: str


def error_responses(*status_codes: int) -> dict[int | str, dict[str, type[ErrorResponse]]]:
    """``responses=`` for a route, so /docs lists the errors it can return."""
    return {code: {"model": ErrorResponse} for code in status_codes}


async def _handle_app_error(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, AppError)  # noqa: S101 - registered for AppError only
    if isinstance(error, FieldError):
        detail: object = [
            {"type": "value_error", "loc": ["body", error.field], "msg": problem}
            for problem in error.problems
        ]
    else:
        detail = error.detail
    return JSONResponse({"detail": detail}, status_code=error.status_code, headers=error.headers)


async def _handle_validation_error(request: Request, error: Exception) -> JSONResponse:
    """
    FastAPI's default answer to invalid input, minus the ``input`` field.

    By default each error echoes back what was sent. For a sign-up with one
    missing field that is the whole body, password included, which then sits
    in browser consoles, proxies and client logs.
    """
    assert isinstance(error, RequestValidationError)  # noqa: S101 - registered for it only
    detail = [
        {"type": item["type"], "loc": list(item["loc"]), "msg": item["msg"]}
        for item in error.errors()
    ]
    return JSONResponse({"detail": detail}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
