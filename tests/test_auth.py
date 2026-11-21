import logging
from datetime import UTC, datetime, timedelta

import anyio
import jwt
import pytest
from bson import ObjectId
from httpx import AsyncClient, Response
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from app.auth import service as auth_service
from app.core.database import Database
from app.core.logs import SECURITY_LOGGER
from app.core.security import (
    create_access_token,
    dummy_hash,
    hash_refresh_token,
    verify_password,
)
from app.core.security.tokens import ALGORITHM
from app.core.settings import Settings
from tests.helpers import (
    ACCESS_TOKEN_TTL_MINUTES,
    API,
    PASSWORD,
    REFRESH_TOKEN_TTL_DAYS,
    bearer,
    login,
    logout_with,
    refresh_cookie,
    refresh_token_of,
    refresh_with,
    register,
    register_and_login,
)

pytestmark = pytest.mark.anyio


# --- register ---------------------------------------------------------------


async def test_register_creates_a_user(client: AsyncClient, db: Database) -> None:
    response = await register(client, email="Jane@Example.com", username="Jane")

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "jane@example.com"
    assert body["username"] == "jane"
    assert body["is_active"] is True
    assert "password" not in body
    assert "password_hash" not in body

    stored = await db.users.find_one({"email": "jane@example.com"})
    assert stored is not None
    assert str(stored["_id"]) == body["id"]
    assert stored["password_hash"].startswith("$argon2id$")
    assert verify_password(PASSWORD, stored["password_hash"])[0]


async def test_register_and_me_report_the_same_timestamp(client: AsyncClient) -> None:
    registered = await register(client)
    access_token = (await login(client)).json()["access_token"]

    me = await client.get(f"{API}/users/me", headers=bearer(access_token))

    # MongoDB keeps milliseconds only; the two must not differ by the rest.
    assert registered.json()["created_at"] == me.json()["created_at"]


async def test_register_rejects_a_duplicate_email_or_username(client: AsyncClient) -> None:
    await register(client)

    same_email = await register(client, email="JANE@example.com", username="other")
    same_username = await register(client, email="other@example.com", username="JANE")

    assert same_email.status_code == 409
    assert same_email.json() == {"detail": "Email already registered"}
    assert same_username.status_code == 409
    assert same_username.json() == {"detail": "Username already taken"}


@pytest.mark.parametrize(
    "fields",
    [
        {"email": "not-an-email"},
        {"username": "ab"},
        {"username": "has space"},
        {"username": "jane@example.com"},
        {"password": ""},
        {"password": "x" * 129},
    ],
)
async def test_register_validates_its_input(client: AsyncClient, fields: dict[str, str]) -> None:
    response = await register(client, **fields)

    assert response.status_code == 422


async def test_register_applies_the_password_policy(client: AsyncClient) -> None:
    response = await register(client, password="short")

    # The same shape as FastAPI's own validation errors.
    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {
                "type": "value_error",
                "loc": ["body", "password"],
                "msg": "Password must be at least 8 characters.",
            }
        ]
    }


async def test_validation_errors_do_not_echo_the_request(client: AsyncClient) -> None:
    secret = "a-password-that-must-not-come-back"

    # No username: by default FastAPI would return the whole body as "input".
    response = await client.post(
        f"{API}/auth/register", json={"email": "jane@example.com", "password": secret}
    )

    assert response.status_code == 422
    assert secret not in response.text
    assert response.json() == {
        "detail": [{"type": "missing", "loc": ["body", "username"], "msg": "Field required"}]
    }


@pytest.mark.parametrize("with_username", [True, False])
async def test_a_password_that_cannot_be_encoded_is_a_422(
    client: AsyncClient, with_username: bool
) -> None:
    # "\ud800" is half of a surrogate pair: legal in JSON, impossible to encode.
    username = '"username": "jane",' if with_username else ""
    body = f'{{"email": "jane@example.com", {username} "password": "\\ud800 long enough"}}'

    response = await client.post(
        f"{API}/auth/register", content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422


# --- login ------------------------------------------------------------------


@pytest.mark.parametrize("identifier", ["jane@example.com", "JANE@example.com", "jane", " Jane "])
async def test_login_with_email_or_username(client: AsyncClient, identifier: str) -> None:
    await register(client)

    response = await login(client, identifier=identifier)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == ACCESS_TOKEN_TTL_MINUTES * 60
    assert body["access_token"]
    assert response.headers["cache-control"] == "no-store"


async def test_login_normalizes_the_email_like_register_does(client: AsyncClient) -> None:
    # "é" written as "e" + a combining accent: a different string for the same
    # address. Register stores the composed form; login must find it.
    decomposed = "josé@example.com"
    assert (await register(client, email=decomposed)).json()["email"] == "josé@example.com"

    assert (await login(client, identifier=decomposed)).status_code == 200
    assert (await login(client, identifier="josé@example.com")).status_code == 200


async def test_login_sets_the_refresh_cookie(client: AsyncClient, db: Database) -> None:
    await register(client)

    response = await login(client)

    cookie = refresh_cookie(response)
    assert cookie["httponly"] is True
    assert cookie["samesite"] == "strict"
    assert cookie["path"] == "/api/v1/auth"
    assert cookie["max-age"] == str(REFRESH_TOKEN_TTL_DAYS * 24 * 60 * 60)
    assert cookie["secure"] == ""  # debug mode: plain HTTP on localhost
    assert cookie["domain"] == ""  # host-only: not shared with subdomains

    # Only the hash of the token is stored.
    assert await db.refresh_tokens.find_one({"_id": cookie.value}) is None
    assert await db.refresh_tokens.find_one({"_id": hash_refresh_token(cookie.value)}) is not None


async def test_login_stores_a_truncated_user_agent(client: AsyncClient, db: Database) -> None:
    await register(client)

    await client.post(
        f"{API}/auth/login",
        data={"username": "jane", "password": PASSWORD},
        headers={"User-Agent": "x" * 1000},
    )

    stored = await db.refresh_tokens.find_one({})
    assert stored is not None
    assert stored["user_agent"] == "x" * 256


@pytest.mark.parametrize(
    ("identifier", "password"),
    [
        ("jane@example.com", "wrong password"),
        ("nobody@example.com", PASSWORD),
        ("nobody", PASSWORD),
        ("not an email@", PASSWORD),
        ("jane@example.com", "x" * 200),
    ],
)
async def test_login_failures_all_look_the_same(
    client: AsyncClient, identifier: str, password: str
) -> None:
    await register(client)

    response = await login(client, identifier=identifier, password=password)

    assert response.status_code == 401
    assert response.json() == {"detail": "Incorrect email, username or password"}
    assert response.headers["www-authenticate"] == "Bearer"
    assert "set-cookie" not in response.headers


async def test_login_for_an_unknown_account_still_verifies_a_hash(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this, "no such user" would answer faster than "wrong password"."""
    verified: list[str] = []
    real = auth_service.verify_password_async

    async def spy(password: str, password_hash: str) -> tuple[bool, str | None]:
        verified.append(password_hash)
        return await real(password, password_hash)

    monkeypatch.setattr(auth_service, "verify_password_async", spy)

    await login(client, identifier="nobody@example.com")

    assert verified == [dummy_hash()]


async def test_login_upgrades_an_outdated_password_hash(client: AsyncClient, db: Database) -> None:
    await register(client)
    # A hash made with weaker parameters than today's, as an old account has.
    weak = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=8192),)).hash(PASSWORD)
    await db.users.update_one({}, {"$set": {"password_hash": weak}})

    response = await login(client)

    assert response.status_code == 200
    stored = await db.users.find_one({})
    assert stored is not None
    assert stored["password_hash"] != weak
    assert verify_password(PASSWORD, stored["password_hash"]) == (True, None)


async def test_a_deactivated_user_cannot_log_in(client: AsyncClient, db: Database) -> None:
    await register(client)
    await db.users.update_one({}, {"$set": {"is_active": False}})

    response = await login(client)

    assert response.status_code == 401
    assert response.json() == {"detail": "Incorrect email, username or password"}


# --- access token -----------------------------------------------------------


async def test_me_returns_the_logged_in_user(client: AsyncClient) -> None:
    access_token = await register_and_login(client)

    response = await client.get(f"{API}/users/me", headers=bearer(access_token))

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "jane@example.com"
    assert body["username"] == "jane"
    assert set(body) == {
        "id",
        "email",
        "username",
        "display_name",
        "bio",
        "is_active",
        "created_at",
    }


async def test_me_requires_a_token(client: AsyncClient) -> None:
    response = await client.get(f"{API}/users/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def _user_id(db: Database) -> ObjectId:
    user = await db.users.find_one({})
    assert user is not None
    user_id = user["_id"]
    assert isinstance(user_id, ObjectId)
    return user_id


def _claims(user_id: ObjectId, **changes: object) -> dict[str, object]:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "type": "access",
    }
    return {**claims, **changes}


async def test_me_accepts_a_properly_signed_token(
    client: AsyncClient, db: Database, settings: Settings
) -> None:
    """The control for the test below: the same claims, untampered, are accepted."""
    await register(client)
    key = settings.secret_key.get_secret_value()
    token = jwt.encode(_claims(await _user_id(db)), key, ALGORITHM)

    response = await client.get(f"{API}/users/me", headers=bearer(token))

    assert response.status_code == 200


# Signing the "another algorithm" case with our key makes PyJWT warn that the
# key is short for HS512; the token is meant to be rejected anyway.
@pytest.mark.filterwarnings("ignore:The HMAC key is")
@pytest.mark.parametrize(
    "case",
    [
        "garbage",
        "wrong key",
        "expired",
        "wrong type",
        "no type",
        "bad subject",
        "unknown user",
        "unsigned",
        "another algorithm",
    ],
)
async def test_me_rejects_bad_tokens(
    client: AsyncClient, db: Database, settings: Settings, case: str
) -> None:
    await register(client)
    user_id = await _user_id(db)
    key = settings.secret_key.get_secret_value()
    past = datetime.now(UTC) - timedelta(seconds=1)
    no_type = {name: value for name, value in _claims(user_id).items() if name != "type"}
    tokens = {
        "garbage": "not.a.token",
        "wrong key": jwt.encode(_claims(user_id), "another-secret-key-of-enough-length", ALGORITHM),
        "expired": jwt.encode(_claims(user_id, exp=past), key, ALGORITHM),
        "wrong type": jwt.encode(_claims(user_id, type="refresh"), key, ALGORITHM),
        "no type": jwt.encode(no_type, key, ALGORITHM),
        "bad subject": jwt.encode(_claims(user_id, sub="not-an-id"), key, ALGORITHM),
        "unknown user": jwt.encode(_claims(user_id, sub="0" * 24), key, ALGORITHM),
        "unsigned": jwt.encode(_claims(user_id), "", algorithm="none"),
        "another algorithm": jwt.encode(_claims(user_id), key, algorithm="HS512"),
    }

    response = await client.get(f"{API}/users/me", headers=bearer(tokens[case]))

    assert response.status_code == 401
    assert response.json() == {"detail": "Could not validate credentials"}


async def test_deactivating_a_user_takes_effect_immediately(
    client: AsyncClient, db: Database
) -> None:
    access_token = await register_and_login(client)
    await db.users.update_one({}, {"$set": {"is_active": False}})

    response = await client.get(f"{API}/users/me", headers=bearer(access_token))

    assert response.status_code == 401


# --- refresh ----------------------------------------------------------------


async def test_refresh_issues_new_tokens(client: AsyncClient) -> None:
    await register(client)
    first = refresh_token_of(await login(client))

    response = await refresh_with(client, first)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert refresh_token_of(response) != first
    me = await client.get(f"{API}/users/me", headers=bearer(response.json()["access_token"]))
    assert me.status_code == 200


async def test_the_client_sends_the_cookie_only_to_the_auth_routes(client: AsyncClient) -> None:
    await register(client)
    await login(client)  # the client's cookie jar now holds the refresh token

    refreshed = await client.post(f"{API}/auth/refresh")
    elsewhere = await client.get(f"{API}/users/me")

    assert refreshed.status_code == 200
    assert "cookie" in refreshed.request.headers
    assert "cookie" not in elsewhere.request.headers


@pytest.mark.parametrize("token", ["", "made-up-token"])
async def test_refresh_rejects_unknown_tokens(client: AsyncClient, token: str) -> None:
    response = await refresh_with(client, token)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid refresh token"}


async def test_refresh_without_a_cookie(client: AsyncClient) -> None:
    response = await client.post(f"{API}/auth/refresh")

    assert response.status_code == 401


async def test_reusing_a_refresh_token_revokes_the_whole_family(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    await register(client)
    first = refresh_token_of(await login(client))
    second = refresh_token_of(await refresh_with(client, first))
    other_session = refresh_token_of(await login(client))

    # `first` was already traded for `second`: someone is replaying it.
    with caplog.at_level(logging.WARNING, logger=SECURITY_LOGGER):
        replay = await refresh_with(client, first)

    assert replay.status_code == 401
    assert "Refresh token reuse detected" in caplog.text
    # The token that replaced it dies too, since the two holders can't be told apart.
    assert (await refresh_with(client, second)).status_code == 401
    # A session from a separate login is a different family and is untouched.
    assert (await refresh_with(client, other_session)).status_code == 200


async def test_two_refreshes_at_once_never_leave_a_live_token(client: AsyncClient) -> None:
    """
    Two requests with the same token at the same moment are a reuse like any
    other: afterwards nothing that came out of them may still work.
    """
    await register(client)

    for _ in range(15):
        token = refresh_token_of(await login(client))
        responses: list[Response] = []

        async def refresh(token: str = token, responses: list[Response] = responses) -> None:
            responses.append(
                await client.post(
                    f"{API}/auth/refresh", headers={"Cookie": f"refresh_token={token}"}
                )
            )

        client.cookies.clear()
        async with anyio.create_task_group() as group:
            group.start_soon(refresh)
            group.start_soon(refresh)
        client.cookies.clear()

        statuses = sorted(response.status_code for response in responses)
        assert statuses in ([200, 401], [401, 401])
        for response in responses:
            if response.status_code == 200:
                assert (await refresh_with(client, refresh_token_of(response))).status_code == 401


async def test_a_refresh_that_fails_to_store_its_token_keeps_the_session(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await register(client)
    token = refresh_token_of(await login(client))
    real = auth_service.AuthService._issue

    async def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the database is having a moment")

    monkeypatch.setattr(auth_service.AuthService, "_issue", fail)
    with pytest.raises(RuntimeError):
        await refresh_with(client, token)
    monkeypatch.setattr(auth_service.AuthService, "_issue", real)

    # The old token was not retired, so the client can simply try again.
    assert (await refresh_with(client, token)).status_code == 200


async def test_an_expired_refresh_token_is_rejected(client: AsyncClient, db: Database) -> None:
    await register(client)
    token = refresh_token_of(await login(client))
    await db.refresh_tokens.update_one(
        {}, {"$set": {"expires_at": datetime.now(UTC) - timedelta(seconds=1)}}
    )

    response = await refresh_with(client, token)

    assert response.status_code == 401


async def test_deactivation_ends_every_session_for_good(client: AsyncClient, db: Database) -> None:
    await register(client)
    phone = refresh_token_of(await login(client))
    laptop = refresh_token_of(await login(client))
    await db.users.update_one({}, {"$set": {"is_active": False}})

    assert (await refresh_with(client, phone)).status_code == 401

    # Reactivating the account brings back neither the session that tried to
    # refresh meanwhile nor the one that didn't.
    await db.users.update_one({}, {"$set": {"is_active": True}})
    assert (await refresh_with(client, phone)).status_code == 401
    assert (await refresh_with(client, laptop)).status_code == 401


# --- logout -----------------------------------------------------------------


async def test_logout_ends_this_session_only(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    await register(client)
    other_session = refresh_token_of(await login(client))
    token = refresh_token_of(await login(client))

    response = await client.post(f"{API}/auth/logout")

    assert response.status_code == 204
    cleared = refresh_cookie(response)
    assert cleared.value == ""
    assert cleared["max-age"] == "0"
    assert cleared["path"] == "/api/v1/auth"

    with caplog.at_level(logging.WARNING, logger=SECURITY_LOGGER):
        assert (await refresh_with(client, token)).status_code == 401
    # Using a logged-out token is not theft and must not be reported as such.
    assert "reuse" not in caplog.text
    assert (await refresh_with(client, other_session)).status_code == 200


async def test_logout_with_a_stale_token_still_ends_the_session(client: AsyncClient) -> None:
    """
    A thief rotated the stolen token; the user's browser still holds the old
    one and logs out. That must end the thief's session too.
    """
    await register(client)
    users_token = refresh_token_of(await login(client))
    thiefs_token = refresh_token_of(await refresh_with(client, users_token))

    response = await logout_with(client, users_token)

    assert response.status_code == 204
    assert (await refresh_with(client, thiefs_token)).status_code == 401


async def test_logout_without_a_session_is_fine(client: AsyncClient) -> None:
    assert (await client.post(f"{API}/auth/logout")).status_code == 204
    assert (await logout_with(client, "made-up-token")).status_code == 204


async def test_logout_all_ends_every_session(client: AsyncClient) -> None:
    await register(client)
    phone = refresh_token_of(await login(client))
    laptop_login = await login(client)
    laptop = refresh_token_of(laptop_login)
    access_token = laptop_login.json()["access_token"]

    response = await client.post(f"{API}/auth/logout-all", headers=bearer(access_token))

    assert response.status_code == 204
    assert (await refresh_with(client, phone)).status_code == 401
    assert (await refresh_with(client, laptop)).status_code == 401
    # By design the access token itself lives on until it expires.
    assert (await client.get(f"{API}/users/me", headers=bearer(access_token))).status_code == 200


async def test_logout_all_requires_an_access_token(client: AsyncClient) -> None:
    response = await client.post(f"{API}/auth/logout-all")

    assert response.status_code == 401


# --- tokens -----------------------------------------------------------------


def test_access_token_claims(settings: Settings) -> None:
    user_id = ObjectId()

    token = create_access_token(user_id, settings)

    claims = jwt.decode(token, settings.secret_key.get_secret_value(), algorithms=[ALGORITHM])
    assert claims["sub"] == str(user_id)
    assert claims["type"] == "access"
    assert claims["exp"] - claims["iat"] == ACCESS_TOKEN_TTL_MINUTES * 60
