"""Small helpers shared by the test modules."""

from http.cookies import Morsel, SimpleCookie

from httpx import AsyncClient, Response

# Written out rather than imported from the app: if the app's prefix or cookie
# name changed by accident, the tests should notice.
API = "/api/v1"
COOKIE = "refresh_token"

PASSWORD = "correct horse battery"
ACCESS_TOKEN_TTL_MINUTES = 15
REFRESH_TOKEN_TTL_DAYS = 14


async def register(
    client: AsyncClient,
    *,
    email: str = "jane@example.com",
    username: str = "jane",
    password: str = PASSWORD,
) -> Response:
    return await client.post(
        f"{API}/auth/register",
        json={"email": email, "username": username, "password": password},
    )


async def login(
    client: AsyncClient, *, identifier: str = "jane@example.com", password: str = PASSWORD
) -> Response:
    # A form, not JSON: that is what OAuth2's password flow sends.
    return await client.post(
        f"{API}/auth/login", data={"username": identifier, "password": password}
    )


async def register_and_login(client: AsyncClient) -> str:
    """Create the default user, log in, and return the access token."""
    assert (await register(client)).status_code == 201
    response = await login(client)
    assert response.status_code == 200
    return str(response.json()["access_token"])


def bearer(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def refresh_cookie(response: Response) -> Morsel[str]:
    """The refresh-token cookie a response sets, parsed into its attributes."""
    for header in response.headers.get_list("set-cookie"):
        cookie = SimpleCookie(header)
        if COOKIE in cookie:
            return cookie[COOKIE]
    raise AssertionError("the response sets no refresh-token cookie")


def refresh_token_of(response: Response) -> str:
    return str(refresh_cookie(response).value)


async def refresh_with(client: AsyncClient, refresh_token: str) -> Response:
    """Call /auth/refresh with exactly this token, whatever the client holds."""
    client.cookies.clear()
    response = await client.post(
        f"{API}/auth/refresh", headers={"Cookie": f"{COOKIE}={refresh_token}"}
    )
    client.cookies.clear()
    return response


async def logout_with(client: AsyncClient, refresh_token: str) -> Response:
    """Call /auth/logout with exactly this token, whatever the client holds."""
    client.cookies.clear()
    response = await client.post(
        f"{API}/auth/logout", headers={"Cookie": f"{COOKIE}={refresh_token}"}
    )
    client.cookies.clear()
    return response
