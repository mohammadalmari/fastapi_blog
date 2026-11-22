"""Auth behaviour that only exists outside debug: the app is built in production mode."""

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.logs import configure_logging
from app.core.settings import Settings
from tests.helpers import API, login, refresh_cookie, register

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("user")]

SITE = "https://blog.example.com"
FRONTEND = "https://app.example.com"


@pytest.fixture(scope="module")
def settings_overrides() -> dict[str, Any]:
    return {
        "debug": False,
        "secret_key": "test-secret-key-that-is-long-enough-for-production",
        "allowed_hosts": ["blog.example.com"],
        "cors_allowed_origins": [FRONTEND],
        "cors_allow_credentials": True,
    }


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url=SITE) as client:
        yield client


async def test_the_refresh_cookie_is_secure(client: AsyncClient) -> None:
    response = await login(client)

    assert response.status_code == 200
    assert refresh_cookie(response)["secure"] is True


async def test_register_applies_the_strict_password_policy(client: AsyncClient) -> None:
    """That the strict rules are the ones in force; test_passwords.py covers each rule."""
    response = await register(
        client, email="new@example.com", username="newcomer", password="123456789012"
    )

    assert response.status_code == 422
    assert [error["msg"] for error in response.json()["detail"]] == [
        "Password can't be entirely numeric."
    ]


@pytest.mark.parametrize(
    ("route", "origin"),
    [
        ("refresh", "https://evil.example"),
        ("logout", "https://evil.example"),
        ("refresh", "null"),  # what sandboxed iframes and some redirects send
        ("refresh", "http://blog.example.com"),  # right host, wrong scheme
        ("refresh", "https://blog.example.com:8443"),  # right host, wrong port
        ("refresh", "https://blog.example.com.evil.example"),
    ],
)
async def test_cookie_routes_reject_other_origins(
    client: AsyncClient, route: str, origin: str
) -> None:
    await login(client)  # the client now holds a valid cookie

    response = await client.post(f"{API}/auth/{route}", headers={"Origin": origin})

    assert response.status_code == 403
    assert response.json() == {"detail": "Origin not allowed"}
    # The request was stopped before it did anything: the session still works.
    assert (await client.post(f"{API}/auth/refresh")).status_code == 200


@pytest.mark.parametrize(
    ("route", "origin"),
    [("refresh", SITE), ("refresh", FRONTEND), ("refresh", None), ("logout", FRONTEND)],
)
async def test_cookie_routes_accept_known_origins_and_non_browsers(
    client: AsyncClient, route: str, origin: str | None
) -> None:
    await login(client)

    response = await client.post(
        f"{API}/auth/{route}", headers={"Origin": origin} if origin else {}
    )

    assert response.status_code in (200, 204)


async def test_production_logs_are_json_with_the_request_id(
    client: AsyncClient, settings: Settings
) -> None:
    """The security log is a file in production; each line must be one JSON object."""
    response = await login(client, password="not the password")

    assert response.status_code == 401
    lines = (settings.log_dir / "security.log").read_text().splitlines()
    entry = json.loads(lines[-1])
    assert entry["message"].startswith("Failed login")
    assert entry["request_id"] == response.headers["x-request-id"]
    assert entry["level"] == "WARNING"


@pytest.mark.parametrize("name", ["uvicorn", "uvicorn.error", "uvicorn.access"])
async def test_the_servers_own_lines_go_through_the_apps_handlers(
    settings: Settings, name: str
) -> None:
    """Otherwise the console is JSON mixed with the server's plain text."""
    # As the server leaves its loggers before the app is loaded: with a
    # handler of their own, and not passing records on.
    server_logger = logging.getLogger(name)
    server_logger.addHandler(logging.NullHandler())
    server_logger.propagate = False

    configure_logging(settings)

    assert server_logger.handlers == []
    assert server_logger.propagate is True
