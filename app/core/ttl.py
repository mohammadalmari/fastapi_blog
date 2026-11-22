"""A value worth keeping for a little while instead of working it out every time."""

from collections.abc import Awaitable, Callable
from time import monotonic

import anyio


class Remembered[T]:
    """
    Remembers the result of an expensive call for ``seconds``.

    Kept per process: each worker has its own copy, which may be up to
    ``seconds`` old. That is the trade: the answer is slightly stale, and the
    database is asked once per period instead of once per request.
    """

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._value: T | None = None
        self._fresh_until = 0.0
        self._lock = anyio.Lock()

    async def get(self, work: Callable[[], Awaitable[T]]) -> T:
        # One request does the work; others arriving meanwhile wait for it
        # rather than all asking the database the same thing at once. With
        # zero seconds a value is never fresh, so the work is done every time.
        async with self._lock:
            if self._value is None or monotonic() >= self._fresh_until:
                self._value = await work()
                self._fresh_until = monotonic() + self._seconds
            return self._value
