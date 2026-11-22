"""The rate limiter itself, against the test database. The limited routes are in tests/auth."""

import hashlib
from datetime import UTC, datetime, timedelta

import anyio
import pytest

from app.core import ratelimit
from app.core.database import Database
from app.core.errors import TooManyRequestsError
from app.core.ratelimit import RateLimiter, address_key
from app.core.settings.limits import RateLimit

pytestmark = pytest.mark.anyio

THRICE_A_MINUTE = RateLimit("test-rule", 3, 60)
# The limiter's clock, held still: the middle of a window, so no test
# straddles the end of one by accident. And far in the future, on purpose.
# Counters carry an expiry date, and MongoDB sweeps expired documents away
# every sixty seconds. With a clock in the past, every counter a test made
# was already expired, and a sweep landing mid-test deleted it: about one
# run in seventy failed on whichever test the sweep happened to hit.
NOON = datetime(2100, 1, 1, 12, 0, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _a_fixed_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ratelimit, "utcnow", lambda: NOON)


async def _allowed(limiter: RateLimiter, rule: RateLimit, caller: str) -> bool:
    try:
        await limiter.hit(rule, caller)
    except TooManyRequestsError:
        return False
    return True


async def test_the_limit_is_allowed_and_one_more_is_not(db: Database) -> None:
    limiter = RateLimiter(db, "a-secret")

    results = [await _allowed(limiter, THRICE_A_MINUTE, "jane") for _ in range(5)]

    assert results == [True, True, True, False, False]


async def test_a_refusal_says_how_long_to_wait(db: Database) -> None:
    limiter = RateLimiter(db, "a-secret")
    for _ in range(3):
        await limiter.hit(THRICE_A_MINUTE, "jane")

    with pytest.raises(TooManyRequestsError) as refusal:
        await limiter.hit(THRICE_A_MINUTE, "jane")

    # Half the minute is gone at NOON, so half is left.
    assert refusal.value.headers == {"Retry-After": "30"}
    assert refusal.value.status_code == 429


async def test_each_caller_and_each_rule_has_its_own_allowance(db: Database) -> None:
    limiter = RateLimiter(db, "a-secret")
    another_rule = RateLimit("another-rule", 3, 60)
    for _ in range(3):
        await limiter.hit(THRICE_A_MINUTE, "jane")

    assert await _allowed(limiter, THRICE_A_MINUTE, "sam") is True
    assert await _allowed(limiter, another_rule, "jane") is True
    assert await _allowed(limiter, THRICE_A_MINUTE, "jane") is False


async def test_the_next_window_starts_afresh(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    limiter = RateLimiter(db, "a-secret")
    for _ in range(3):
        await limiter.hit(THRICE_A_MINUTE, "jane")

    monkeypatch.setattr(ratelimit, "utcnow", lambda: NOON + timedelta(seconds=30))

    assert await _allowed(limiter, THRICE_A_MINUTE, "jane") is True


async def test_requests_at_the_same_moment_cannot_share_the_last_place(db: Database) -> None:
    """The attack: send a burst in parallel, hoping each sees "one left"."""
    limiter = RateLimiter(db, "a-secret")
    results: list[bool] = []

    async def attempt() -> None:
        results.append(await _allowed(limiter, THRICE_A_MINUTE, "jane"))

    async with anyio.create_task_group() as group:
        for _ in range(12):
            group.start_soon(attempt)

    assert sorted(results) == [False] * 9 + [True] * 3


async def test_a_place_given_back_can_be_used_again(db: Database) -> None:
    """For rules that count only failures: taken first, returned on success."""
    limiter = RateLimiter(db, "a-secret")
    places = [await limiter.hit(THRICE_A_MINUTE, "jane") for _ in range(3)]

    await limiter.give_back(places[0])

    assert await _allowed(limiter, THRICE_A_MINUTE, "jane") is True
    assert await _allowed(limiter, THRICE_A_MINUTE, "jane") is False


async def test_a_counter_names_nobody_and_removes_itself(db: Database) -> None:
    await RateLimiter(db, "a-secret").hit(THRICE_A_MINUTE, "jane@example.com")

    (counter,) = await db.rate_limits.find({}).to_list()
    indexes = await db.rate_limits.index_information()

    # An address or an email is counted by a keyed hash, not kept: neither
    # the value nor a plain hash of it, which trying every address would undo.
    assert "jane" not in counter["_id"]
    assert hashlib.sha256(b"jane@example.com").hexdigest() not in counter["_id"]
    assert counter["count"] == 1
    # Gone when its window is: MongoDB deletes it at `expires_at`.
    assert counter["expires_at"] == NOON + timedelta(seconds=30)
    assert indexes["expires_at_1"]["expireAfterSeconds"] == 0


@pytest.mark.parametrize(
    ("host", "counted_as"),
    [
        ("203.0.113.7", "203.0.113.7"),
        # One customer's IPv6 network, whichever of its addresses is used.
        ("2001:db8:aa:1::1", "2001:db8:aa:1::/64"),
        ("2001:db8:aa:1:ffff:ffff:ffff:fffe", "2001:db8:aa:1::/64"),
        ("2001:DB8:AA:1::2", "2001:db8:aa:1::/64"),
        ("2001:db8:aa:2::1", "2001:db8:aa:2::/64"),
        # An IPv4 address written the IPv6 way is that IPv4 address.
        ("::ffff:203.0.113.7", "203.0.113.7"),
        ("testclient", "testclient"),
        (None, "unknown"),
    ],
)
def test_what_an_address_is_counted_by(host: str | None, counted_as: str) -> None:
    assert address_key(host) == counted_as
