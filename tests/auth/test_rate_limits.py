"""
The routes that are rate limited. How counting works is in
tests/core/test_ratelimit.py; here, that each route is counted, by the right
thing, and what a refusal looks like.

Using an allowance up through HTTP would take dozens of requests per test, so
each test spends all but the last of it straight on the limiter, and then
makes the requests that matter.
"""

import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import anyio
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth import service as auth_service
from app.core import ratelimit
from app.core.database import Database
from app.core.logs import SECURITY_LOGGER
from app.core.ratelimit import RateLimiter
from app.core.settings.environment import DEV_SECRET_KEY
from app.core.settings.limits import RateLimit
from app.core.settings.security import (
    AVATARS_PER_USER,
    COMMENTS_PER_USER,
    LIKES_PER_USER,
    LOGIN_FAILURES_PER_ACCOUNT,
    LOGIN_PER_ADDRESS,
    PASSWORD_CHANGE_PER_USER,
    REGISTER_PER_ADDRESS,
)
from app.posts.models import Post
from app.users.models import User
from tests.conftest import MakeUser
from tests.helpers import API, PASSWORD, login, register
from tests.users.pictures import picture

pytestmark = pytest.mark.anyio

Headers = dict[str, str]
# The address httpx's test transport gives every request.
HERE = "127.0.0.1"
TOO_MANY = {"detail": "Too many requests. Try again later."}
# The limiter's clock, held still in the middle of every rule's window. With
# the real clock, a window ending between two requests of one test would
# turn an expected refusal into a pass, once in a few hundred runs.
NOON = datetime(2026, 1, 1, 12, 0, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _the_limiters_clock_stands_still(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ratelimit, "utcnow", lambda: NOON)


async def _spend(db: Database, rule: RateLimit, caller: str, *, leaving: int = 1) -> None:
    """Use the caller's allowance up, except for the last `leaving`."""
    limiter = RateLimiter(db, DEV_SECRET_KEY)
    for _ in range(rule.limit - leaving):
        await limiter.hit(rule, caller)


@pytest.fixture
async def elsewhere(app: FastAPI) -> AsyncGenerator[AsyncClient]:
    """A client whose requests come from another address."""
    transport = ASGITransport(app=app, client=("203.0.113.9", 4321))
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


# --- logging in -----------------------------------------------------------------


async def test_an_account_is_locked_after_too_many_wrong_passwords(
    client: AsyncClient,
    elsewhere: AsyncClient,
    user: User,
    make_user: MakeUser,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    The attack: guess one account's password from many machines. The right
    password is refused too once the account is locked, from any address.
    """
    await make_user("sam", "reader")
    guesses = [
        await login(client, password=f"guess number {n}")
        for n in range(LOGIN_FAILURES_PER_ACCOUNT.limit)
    ]

    with caplog.at_level(logging.WARNING, logger=SECURITY_LOGGER):
        locked = await login(elsewhere, password=PASSWORD)
    someone_else = await login(elsewhere, identifier="sam", password=PASSWORD)

    assert {guess.status_code for guess in guesses} == {401}
    assert (locked.status_code, locked.json()) == (429, TOO_MANY)
    # The fifteen-minute window that holds NOON ends at 12:15.
    assert locked.headers["retry-after"] == "870"
    assert "set-cookie" not in locked.headers
    assert someone_else.status_code == 200
    assert "Login refused: locked" in caplog.text


async def test_the_lock_follows_the_account_however_it_is_spelt(
    client: AsyncClient, db: Database, user: User
) -> None:
    """The attack: get more guesses by typing the same account differently."""
    await _spend(db, LOGIN_FAILURES_PER_ACCOUNT, "email:jane@example.com", leaving=0)

    by_email = await login(client, identifier="  JANE@Example.com ", password=PASSWORD)
    by_username = await login(client, identifier="jane", password=PASSWORD)

    assert by_email.status_code == 429
    # A known gap, kept on purpose: the email and the username are counted
    # apart, so an account can be tried by both. Twice the guesses, no more.
    assert by_username.status_code == 200


async def test_logging_in_successfully_does_not_count_against_the_account(
    client: AsyncClient, db: Database, user: User
) -> None:
    for _ in range(3):
        assert (await login(client)).status_code == 200

    counters = await db.rate_limits.find({"_id": {"$regex": "^login-account"}}).to_list()
    assert sum(counter["count"] for counter in counters) == 0


async def test_a_locked_name_looks_the_same_whether_or_not_it_has_an_account(
    client: AsyncClient, db: Database, user: User
) -> None:
    """Otherwise the lock would tell a stranger which emails are registered."""
    for identifier in ("jane@example.com", "nobody@example.com"):
        for n in range(LOGIN_FAILURES_PER_ACCOUNT.limit):
            await login(client, identifier=identifier, password=f"guess {n}")

    real = await login(client, identifier="jane@example.com", password="one more")
    invented = await login(client, identifier="nobody@example.com", password="one more")

    assert (
        (real.status_code, real.json())
        == (invented.status_code, invented.json())
        == (429, TOO_MANY)
    )


async def test_one_address_cannot_try_account_after_account(
    client: AsyncClient, elsewhere: AsyncClient, db: Database, user: User
) -> None:
    """The attack: one password tried against many accounts, so that no account locks."""
    await _spend(db, LOGIN_PER_ADDRESS, HERE)

    last_allowed = await login(client, identifier="someone@example.com", password="Spring2026!")
    refused = await login(client, password=PASSWORD)
    from_elsewhere = await login(elsewhere, password=PASSWORD)

    assert last_allowed.status_code == 401
    assert (refused.status_code, refused.json()) == (429, TOO_MANY)
    assert from_elsewhere.status_code == 200


# --- the other limited routes -----------------------------------------------------


async def test_signing_up_is_limited_by_address(
    client: AsyncClient, elsewhere: AsyncClient, db: Database
) -> None:
    await _spend(db, REGISTER_PER_ADDRESS, HERE)

    last_allowed = await register(client, email="a@example.com", username="first")
    refused = await register(client, email="b@example.com", username="second")
    from_elsewhere = await register(elsewhere, email="c@example.com", username="third")

    assert (last_allowed.status_code, refused.status_code, from_elsewhere.status_code) == (
        201,
        429,
        201,
    )
    assert await db.users.count_documents({"username": "second"}) == 0


async def test_guessing_the_current_password_is_limited(
    client: AsyncClient, db: Database, user: User, user_headers: Headers
) -> None:
    """The attack: a stolen token used to try passwords until one is right."""
    url = f"{API}/users/me/password"
    await _spend(db, PASSWORD_CHANGE_PER_USER, str(user.id))

    a_guess = await client.post(
        url,
        json={"current_password": "a guess", "new_password": "a new passphrase"},
        headers=user_headers,
    )
    the_right_one = await client.post(
        url,
        json={"current_password": PASSWORD, "new_password": "a new passphrase"},
        headers=user_headers,
    )

    assert a_guess.status_code == 422
    assert (the_right_one.status_code, the_right_one.json()) == (429, TOO_MANY)
    assert (await login(client, password=PASSWORD)).status_code == 200


async def test_comments_and_replies_share_one_allowance(
    client: AsyncClient, db: Database, post: Post, user: User, user_headers: Headers
) -> None:
    await _spend(db, COMMENTS_PER_USER, str(user.id), leaving=2)

    comment = await client.post(
        f"{API}/posts/{post.id}/comments", json={"content": "One"}, headers=user_headers
    )
    reply = await client.post(
        f"{API}/comments/{comment.json()['id']}/replies",
        json={"content": "Two"},
        headers=user_headers,
    )
    refused = await client.post(
        f"{API}/posts/{post.id}/comments", json={"content": "Three"}, headers=user_headers
    )

    assert (comment.status_code, reply.status_code, refused.status_code) == (201, 201, 429)
    assert await db.comments.count_documents({}) == 2
    stored = await db.posts.find_one({"_id": post.id})
    assert stored is not None
    assert stored["comment_count"] == 2


async def test_liking_and_unliking_share_one_allowance(
    client: AsyncClient, db: Database, post: Post, user: User, user_headers: Headers
) -> None:
    """The attack: like and unlike in a loop, each a transaction on the post."""
    await _spend(db, LIKES_PER_USER, str(user.id), leaving=2)
    url = f"{API}/posts/{post.id}/like"

    like = await client.put(url, headers=user_headers)
    unlike = await client.delete(url, headers=user_headers)
    refused = await client.put(url, headers=user_headers)

    assert (like.status_code, unlike.status_code, refused.status_code) == (204, 204, 429)
    assert await db.likes.count_documents({}) == 0


async def test_uploading_avatars_is_limited(
    client: AsyncClient, db: Database, user: User, user_headers: Headers
) -> None:
    await _spend(db, AVATARS_PER_USER, str(user.id))
    files = {"file": ("me.png", picture(size=(32, 32)), "image/png")}

    last_allowed = await client.put(f"{API}/users/me/avatar", files=files, headers=user_headers)
    refused = await client.put(f"{API}/users/me/avatar", files=files, headers=user_headers)

    assert (last_allowed.status_code, refused.status_code) == (200, 429)
    # The hour that holds NOON ends at 13:00.
    assert refused.headers["retry-after"] == "3570"


async def test_a_limit_by_user_is_each_users_own(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    author_headers: Headers,
) -> None:
    await _spend(db, LIKES_PER_USER, str(user.id), leaving=0)

    response = await client.put(f"{API}/posts/{post.id}/like", headers=author_headers)

    assert response.status_code == 204


async def test_a_request_without_a_login_is_refused_before_it_is_counted(
    client: AsyncClient, db: Database, post: Post
) -> None:
    for _ in range(3):
        await client.put(f"{API}/posts/{post.id}/like")

    assert await db.rate_limits.count_documents({}) == 0


async def test_the_api_docs_say_which_routes_can_answer_429(app: FastAPI) -> None:
    paths = app.openapi()["paths"]
    limited = {
        (method, path)
        for path, operations in paths.items()
        for method, operation in operations.items()
        if "429" in operation["responses"]
    }

    assert limited == {
        ("post", f"{API}/auth/register"),
        ("post", f"{API}/auth/login"),
        ("post", f"{API}/users/me/password"),
        ("put", f"{API}/users/me/avatar"),
        ("post", f"{API}/posts/{{post_id}}/comments"),
        ("post", f"{API}/comments/{{comment_id}}/replies"),
        ("put", f"{API}/posts/{{post_id}}/like"),
        ("delete", f"{API}/posts/{{post_id}}/like"),
        ("put", f"{API}/comments/{{comment_id}}/like"),
        ("delete", f"{API}/comments/{{comment_id}}/like"),
    }


# --- found in review --------------------------------------------------------------


async def test_a_burst_of_guesses_at_one_account_cannot_outrun_the_lock(
    app: FastAPI, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The attack: many machines guess one account's password at the same
    moment. Counting a failure only after the slow password check let every
    guess in the burst through; a place has to be taken before the check.
    """
    verified = 0
    real = auth_service.verify_password_async

    async def counting(password: str, password_hash: str) -> tuple[bool, str | None]:
        nonlocal verified
        verified += 1
        await anyio.sleep(0.05)  # hashing is slow; that is what the burst exploits
        return await real(password, password_hash)

    monkeypatch.setattr(auth_service, "verify_password_async", counting)
    statuses: list[int] = []

    async def guess(number: int) -> None:
        # Each from its own address, so only the account's lock is in the way.
        transport = ASGITransport(app=app, client=(f"198.51.100.{number}", 4000))
        async with AsyncClient(transport=transport, base_url="http://localhost") as machine:
            statuses.append((await login(machine, password=f"guess {number}")).status_code)

    async with anyio.create_task_group() as group:
        for number in range(40):
            group.start_soon(guess, number)

    assert verified == LOGIN_FAILURES_PER_ACCOUNT.limit
    assert sorted(statuses) == [401] * 10 + [429] * 30


async def test_addresses_in_one_ipv6_network_share_an_allowance(
    app: FastAPI, db: Database, user: User
) -> None:
    """
    The attack: an ordinary IPv6 connection comes with 2^64 addresses. Using
    a new one for each request made every limit by address meaningless.
    """
    statuses: list[int] = []
    for host in ("2001:db8:aa:1::1", "2001:db8:aa:1:ffff:ffff:ffff:fffe", "2001:db8:aa:2::1"):
        transport = ASGITransport(app=app, client=(host, 4000))
        async with AsyncClient(transport=transport, base_url="http://localhost") as machine:
            if not statuses:
                # The first address spends the network's whole allowance.
                for n in range(LOGIN_PER_ADDRESS.limit):
                    await login(machine, identifier=f"nobody{n}@example.com", password="x")
            statuses.append((await login(machine, password=PASSWORD)).status_code)

    # Refused, refused (same /64), allowed (the network next door).
    assert statuses == [429, 429, 200]
