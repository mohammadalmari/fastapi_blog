"""POST /users/me/password."""

import logging
from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient

from app.auth import service as auth_service
from app.core.database import Database, utcnow
from app.core.logs import SECURITY_LOGGER
from app.core.security import hash_password
from app.users.models import User
from tests.helpers import (
    API,
    PASSWORD,
    bearer,
    login,
    refresh_cookie,
    refresh_token_of,
    refresh_with,
)

pytestmark = pytest.mark.anyio

Headers = dict[str, str]
URL = f"{API}/users/me/password"
NEW = "a brand new passphrase"


def _request(current: str = PASSWORD, new: str = NEW) -> dict[str, str]:
    return {"current_password": current, "new_password": new}


async def test_change_your_password(client: AsyncClient, user: User, user_headers: Headers) -> None:
    response = await client.post(URL, json=_request(), headers=user_headers)

    assert (response.status_code, response.content) == (204, b"")
    assert (await login(client, password=PASSWORD)).status_code == 401
    assert (await login(client, password=NEW)).status_code == 200


async def test_changing_the_password_ends_every_session(
    client: AsyncClient, db: Database, user: User
) -> None:
    """
    Why someone changes a password: they fear another person is logged in.
    Every session must end, on every device, the thief's included.
    """
    here = await login(client)
    elsewhere = await login(client)

    response = await client.post(URL, json=_request(), headers=bearer(here.json()["access_token"]))

    assert response.status_code == 204
    for session in (here, elsewhere):
        assert (await refresh_with(client, refresh_token_of(session))).status_code == 401
    assert await db.refresh_tokens.count_documents({"revoked_at": None}) == 0
    # The browser is told to drop its cookie, too.
    assert refresh_cookie(response).value == ""


async def test_a_stolen_access_token_is_not_enough(
    client: AsyncClient,
    db: Database,
    user: User,
    user_headers: Headers,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The attack: whoever holds a token tries to make the account theirs for good."""
    session = await login(client)

    with caplog.at_level(logging.WARNING, logger=SECURITY_LOGGER):
        response = await client.post(URL, json=_request(current="a guess"), headers=user_headers)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "current_password"]
    assert (await login(client, password=PASSWORD)).status_code == 200
    # Nobody was logged out by a wrong guess.
    assert (await refresh_with(client, refresh_token_of(session))).status_code == 200
    assert "Wrong current password" in caplog.text
    assert "a guess" not in caplog.text


@pytest.mark.parametrize(
    ("new", "problem"),
    [("short", "at least"), (PASSWORD, "different from the current one")],
    ids=["too-weak", "unchanged"],
)
async def test_a_new_password_that_will_not_do_is_refused(
    client: AsyncClient, user: User, user_headers: Headers, new: str, problem: str
) -> None:
    session = await login(client)

    response = await client.post(URL, json=_request(new=new), headers=user_headers)

    assert response.status_code == 422
    (error,) = response.json()["detail"]
    assert error["loc"] == ["body", "new_password"]
    assert problem in error["msg"]
    assert (await refresh_with(client, refresh_token_of(session))).status_code == 200


async def test_a_request_with_a_field_it_does_not_have_is_refused_and_not_echoed(
    client: AsyncClient, user: User, user_headers: Headers
) -> None:
    """A misspelt or invented field is an error, and the error repeats no password."""
    response = await client.post(
        URL, json={**_request(), "logout_everywhere": False}, headers=user_headers
    )

    assert response.status_code == 422
    assert PASSWORD not in response.text
    assert NEW not in response.text
    assert (await login(client, password=PASSWORD)).status_code == 200


async def test_changing_a_password_needs_a_login(client: AsyncClient) -> None:
    response = await client.post(URL, json=_request())

    assert response.status_code == 401


# --- things that happen at the same moment ---------------------------------------
# Each test lets one request get as far as "the password was right", then makes
# something else happen before that request writes anything.


def _meanwhile(monkeypatch: pytest.MonkeyPatch, happen: Callable[[], Awaitable[object]]) -> None:
    """Run `happen` right after the next password check succeeds."""
    real = auth_service.verify_password_async
    done = False

    async def verify_then(password: str, password_hash: str) -> tuple[bool, str | None]:
        nonlocal done
        result = await real(password, password_hash)
        if result[0] and not done:
            done = True
            await happen()
        return result

    monkeypatch.setattr(auth_service, "verify_password_async", verify_then)


async def test_a_change_made_with_a_password_that_was_replaced_meanwhile_is_refused(
    client: AsyncClient,
    db: Database,
    user: User,
    user_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The attack: someone holding a stolen token and the old password sends a
    change just as the owner changes it. Whoever wrote last used to win, and
    the owner was locked out of their own account.
    """
    owners_hash = hash_password("the owner's new passphrase")

    async def the_owner_changes_it() -> None:
        await db.users.update_one({"_id": user.id}, {"$set": {"password_hash": owners_hash}})

    _meanwhile(monkeypatch, the_owner_changes_it)

    response = await client.post(
        URL, json=_request(new="the thief's passphrase"), headers=user_headers
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "current_password"]
    stored = await db.users.find_one({"_id": user.id})
    assert stored is not None
    assert stored["password_hash"] == owners_hash


@pytest.mark.parametrize("event", ["password-changed", "deactivated"])
async def test_a_login_overtaken_by_a_change_opens_no_session(
    client: AsyncClient,
    db: Database,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
) -> None:
    """
    The attack: log in with the old password at the very moment it is
    changed, or the account switched off. The password was right when it was
    checked; the session must still not outlive what happened next.
    """
    change = (
        {"password_hash": hash_password(NEW)}
        if event == "password-changed"
        else {"is_active": False}
    )

    async def it_happens() -> None:
        await db.users.update_one({"_id": user.id}, {"$set": change})
        await db.refresh_tokens.update_many({}, {"$set": {"revoked_at": utcnow()}})

    _meanwhile(monkeypatch, it_happens)

    response = await login(client, password=PASSWORD)

    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert await db.refresh_tokens.count_documents({"revoked_at": None}) == 0
