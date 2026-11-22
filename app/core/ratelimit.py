"""
Rate limiting: how often one caller may do one thing.

The counters are kept in MongoDB, not in the process. A count in memory
would be per worker, so four workers would allow four times the limit, and
it would be lost on every restart. The database is already there, and one
small write per limited request is cheap next to what those requests do
(hash a password, run a transaction, decode a picture).

The method is a fixed window: time is cut into windows of the rule's length,
and each (rule, caller, window) has one counter. It is simple and atomic. Its
known weakness is the boundary: someone who spends a whole allowance at the
end of one window and another at the start of the next gets twice the rate
for a moment. The limits are chosen with that in mind.
"""

import hashlib
import hmac
import ipaddress
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, Request
from pymongo import IndexModel, ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.database import Database, DbDep, register_indexes, utcnow
from app.core.errors import TooManyRequestsError
from app.core.settings import SettingsDep
from app.core.settings.limits import RateLimit

COLLECTION = "rate_limits"

register_indexes(
    COLLECTION,
    [
        # A TTL index: MongoDB deletes each counter once its window is over.
        IndexModel("expires_at", expireAfterSeconds=0),
    ],
)

# How much of an IPv6 address identifies a customer. The rest, 64 bits, is
# theirs to fill in as they like.
_IPV6_NETWORK_BITS = 64


class RateLimiter:
    def __init__(self, db: Database, secret: str) -> None:
        self._counters = db[COLLECTION]
        self._secret = secret.encode()

    async def hit(self, rule: RateLimit, caller: str) -> str:
        """
        Count one use. Raises TooManyRequestsError once over the limit.

        Returns the counter that was used, for `give_back`.
        """
        now = int(utcnow().timestamp())
        window = now // rule.window_seconds
        window_ends = (window + 1) * rule.window_seconds
        counter_id = f"{rule.name}:{self._label(caller)}:{window}"
        update = {
            "$inc": {"count": 1},
            "$setOnInsert": {"expires_at": datetime.fromtimestamp(window_ends, UTC)},
        }
        try:
            count = await self._count(counter_id, update)
        except DuplicateKeyError:
            # Two first requests of a window at the same moment: both tried to
            # create the counter and one lost. It exists now, so count again.
            count = await self._count(counter_id, update)
        if count > rule.limit:
            raise TooManyRequestsError(retry_after=window_ends - now)
        return counter_id

    async def give_back(self, counter_id: str) -> None:
        """
        Undo one `hit`, for a rule that counts only failures.

        Such a rule still counts first and gives back on success, never the
        other way round. If the count were added only once a failure is
        known, every request arriving in the meantime would see the old
        count and be let through.
        """
        await self._counters.update_one({"_id": counter_id}, {"$inc": {"count": -1}})

    async def _count(self, counter_id: str, update: dict[str, Any]) -> int:
        # Adding one and reading the result is a single operation, so no two
        # requests can both see "one left".
        counter = await self._counters.find_one_and_update(
            {"_id": counter_id}, update, upsert=True, return_document=ReturnDocument.AFTER
        )
        assert counter is not None  # noqa: S101 - `upsert` always leaves one to return
        return int(counter["count"])

    def _label(self, caller: str) -> str:
        # The caller is an address, an email or a user id. It is counted by a
        # keyed hash: enough to tell callers apart, and not something that
        # can be turned back into the address or email by trying them all,
        # as a plain hash of so few possibilities could be.
        return hmac.new(self._secret, caller.encode(), hashlib.sha256).hexdigest()


async def get_rate_limiter(db: DbDep, settings: SettingsDep) -> RateLimiter:
    return RateLimiter(db, settings.secret_key.get_secret_value())


RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


def address_key(host: str | None) -> str:
    """
    What to count an address by.

    An IPv4 address is counted as itself. An IPv6 connection comes with a
    whole network of addresses, 2^64 of them, so counting each separately
    would let one customer use a new address for every request: it is
    counted by its network instead.
    """
    if host is None:
        # No address at all, as when the server listens on a unix socket.
        # Everyone then shares one allowance; see the rate limiting docs.
        return "unknown"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(address, ipaddress.IPv4Address):
        return str(address)
    if address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)  # an IPv4 address written the IPv6 way
    network = ipaddress.ip_network((address, _IPV6_NETWORK_BITS), strict=False)
    return str(network)


def client_address(request: Request) -> str:
    """
    The address the request came from, as it is counted.

    Behind a proxy this is the proxy's address, and every visitor would share
    one allowance, unless the server is told to trust the proxy's
    X-Forwarded-For header (``--forwarded-allow-ips``, see the README). It
    must trust only the proxy: the header is otherwise the client's to write.
    """
    return address_key(request.client.host if request.client else None)


def limit_by_address(rule: RateLimit) -> Callable[..., Coroutine[Any, Any, None]]:
    """
    A dependency that counts the request against the caller's address, for
    routes used without a login::

        @router.post("/register", dependencies=[Depends(limit_by_address(REGISTER))])

    For routes that need a login, count by user instead: `limit_by_user` in
    ``app.auth.dependencies``. An address may be shared by an office or a
    phone network; an account is one person.
    """

    async def dependency(request: Request, limiter: RateLimiterDep) -> None:
        await limiter.hit(rule, client_address(request))

    return dependency
