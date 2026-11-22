"""Request ids, and the two log formats."""

import json
import logging
import sys
from typing import override

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.logs import SECURITY_LOGGER, JsonFormatter, RequestIdFilter, configure_logging
from app.core.request_id import request_id
from app.core.settings import Settings
from app.users.repository import UserRepository
from tests.helpers import login

pytestmark = pytest.mark.anyio


async def test_every_response_carries_a_fresh_request_id(client: AsyncClient) -> None:
    first = await client.get("/health")
    second = await client.get("/health")
    rejected = await client.get("/api/v1/users/me")

    ids = [response.headers["x-request-id"] for response in (first, second, rejected)]
    assert len(set(ids)) == 3
    assert all(len(one) == 32 and one.isalnum() for one in ids)


async def test_an_id_given_by_a_proxy_is_kept(client: AsyncClient) -> None:
    response = await client.get("/health", headers={"X-Request-ID": "proxy-7f3a9c21.42"})

    assert response.headers["x-request-id"] == "proxy-7f3a9c21.42"


@pytest.mark.parametrize(
    "sent",
    ["short", "x" * 65, "has a space in it", "id\tINFO forged line", "<script>alert(1)</script>"],
)
async def test_an_id_that_does_not_look_like_one_is_replaced(
    client: AsyncClient, sent: str
) -> None:
    """The attack: a header written to look like a log line, or to break whatever shows it."""
    response = await client.get("/health", headers={"X-Request-ID": sent})

    assert response.headers["x-request-id"] != sent
    assert len(response.headers["x-request-id"]) == 32


async def test_log_lines_written_during_a_request_carry_its_id(
    client: AsyncClient, user: object
) -> None:
    seen: list[str] = []

    class Collect(logging.Handler):
        @override
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(str(getattr(record, "request_id", None)))

    handler = Collect()
    handler.addFilter(RequestIdFilter())
    logging.getLogger(SECURITY_LOGGER).addHandler(handler)
    try:
        response = await login(client, password="not the password")
    finally:
        logging.getLogger(SECURITY_LOGGER).removeHandler(handler)

    assert response.status_code == 401
    assert seen == [response.headers["x-request-id"]]
    # And nothing is left set once the request is over.
    assert request_id.get() is None


def _record(message: str, *args: object, exc_info: bool = False) -> logging.LogRecord:
    record = logging.LogRecord(
        "app.security",
        logging.WARNING,
        __file__,
        1,
        message,
        args,
        sys.exc_info() if exc_info else None,
    )
    RequestIdFilter().filter(record)
    return record


def test_a_json_log_line_has_the_fields_a_collector_filters_on() -> None:
    token = request_id.set("abc12345")
    try:
        line = JsonFormatter().format(_record("Failed login for %s", "user 42"))
    finally:
        request_id.reset(token)

    entry = json.loads(line)
    assert entry["level"] == "WARNING"
    assert entry["logger"] == "app.security"
    assert entry["message"] == "Failed login for user 42"
    assert entry["request_id"] == "abc12345"
    assert entry["time"].endswith("+00:00")


def test_a_message_cannot_break_out_of_its_line() -> None:
    """The attack: text with a line break in it, so that one event reads as two."""
    forged = 'x\n{"level": "INFO", "message": "all is well"}'

    line = JsonFormatter().format(_record("Rejected origin %r", forged))

    assert "\n" not in line
    assert json.loads(line)["message"].startswith("Rejected origin")


def test_an_error_is_logged_with_its_traceback() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        line = JsonFormatter().format(_record("It failed", exc_info=True))

    entry = json.loads(line)
    assert "ValueError: boom" in entry["exception"]
    assert "\n" not in line


async def test_a_record_that_missed_the_filter_is_still_printed(settings: Settings) -> None:
    """
    The readable format names the request id. A record arriving through a
    handler added later, without the filter, must print a dash, not fail.
    """
    configure_logging(settings)
    (formatter,) = {
        handler.formatter
        for handler in logging.getLogger().handlers
        if handler.formatter is not None and "request_id" in str(handler.formatter._fmt)
    }
    bare = logging.LogRecord("app", logging.INFO, __file__, 1, "Starting", (), None)

    assert "[-] app Starting" in formatter.format(bare)


def test_outside_a_request_the_id_is_a_dash() -> None:
    assert json.loads(JsonFormatter().format(_record("Starting")))["request_id"] == "-"


async def test_a_page_on_another_origin_can_read_the_two_headers_it_needs(
    client: AsyncClient,
) -> None:
    """A browser hides response headers from other origins unless they are listed."""
    response = await client.get("/health", headers={"Origin": "https://app.example.com"})

    exposed = {
        name.strip().lower()
        for name in response.headers["access-control-expose-headers"].split(",")
    }
    assert {"retry-after", "x-request-id"} <= exposed


async def test_an_error_nobody_handled_still_has_its_request_id(
    app: FastAPI, user: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The one case the id exists for: "I got an error at ten past three". The
    response must carry the id, and so must the logged traceback.
    """

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the database is having a moment")

    monkeypatch.setattr(UserRepository, "get_by_login", broken)
    logged: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        @override
        def emit(self, record: logging.LogRecord) -> None:
            logged.append(record)

    handler = Collect(level=logging.ERROR)
    handler.addFilter(RequestIdFilter())
    logging.getLogger("app").addHandler(handler)
    # A browser gets a response, not a Python exception: ask httpx for the same.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://localhost") as browser:
            response = await login(browser)
    finally:
        logging.getLogger("app").removeHandler(handler)

    assert (response.status_code, response.json()) == (500, {"detail": "Internal Server Error"})
    assert len(response.headers["x-request-id"]) == 32
    # The other protections are on this response too.
    assert response.headers["x-content-type-options"] == "nosniff"
    (record,) = logged
    assert getattr(record, "request_id", None) == response.headers["x-request-id"]
    assert record.exc_info is not None
    assert "the database is having a moment" not in response.text
