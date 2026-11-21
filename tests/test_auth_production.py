"""Auth behaviour that only exists outside debug: the app is built in production mode."""

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tests.helpers import API, login, refresh_cookie, register

pytestmark = pytest.mark.anyio

SITE = "https://blog.example.com"
FRONTEND = "https://app.example.com"
STRONG_PASSWORD = "a long and unusual passphrase"


@pytest.fixture
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
    await register(client, password=STRONG_PASSWORD)

    response = await login(client, password=STRONG_PASSWORD)

    assert response.status_code == 200
    assert refresh_cookie(response)["secure"] is True


@pytest.mark.parametrize(
    ("password", "problem"),
    [
        ("short", "Password must be at least 12 characters."),
        ("123456789012", "Password can't be entirely numeric."),
        ("jane@example.com", "Password is too similar to your email or username."),
    ],
)
async def test_register_applies_the_strict_password_policy(
    client: AsyncClient, password: str, problem: str
) -> None:
    response = await register(client, password=password)

    assert response.status_code == 422
    assert problem in [error["msg"] for error in response.json()["detail"]]


@pytest.mark.parametrize("route", ["refresh", "logout"])
@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "null",  # what sandboxed iframes and some redirects send
        "http://blog.example.com",  # right host, wrong scheme
        "https://blog.example.com:8443",  # right host, wrong port
        "https://blog.example.com.evil.example",
    ],
)
async def test_cookie_routes_reject_other_origins(
    client: AsyncClient, route: str, origin: str
) -> None:
    await register(client, password=STRONG_PASSWORD)
    await login(client, password=STRONG_PASSWORD)  # the client now holds a valid cookie

    response = await client.post(f"{API}/auth/{route}", headers={"Origin": origin})

    assert response.status_code == 403
    assert response.json() == {"detail": "Origin not allowed"}
    # The request was stopped before it did anything: the session still works.
    assert (await client.post(f"{API}/auth/refresh")).status_code == 200


@pytest.mark.parametrize("route", ["refresh", "logout"])
@pytest.mark.parametrize("origin", [SITE, FRONTEND, None])
async def test_cookie_routes_accept_known_origins_and_non_browsers(
    client: AsyncClient, route: str, origin: str | None
) -> None:
    await register(client, password=STRONG_PASSWORD)
    await login(client, password=STRONG_PASSWORD)

    response = await client.post(
        f"{API}/auth/{route}", headers={"Origin": origin} if origin else {}
    )

    assert response.status_code in (200, 204)
