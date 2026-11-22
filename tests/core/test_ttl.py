"""Remembered: a value kept for a while. No app, no database."""

import anyio
import pytest

from app.core import ttl
from app.core.ttl import Remembered

pytestmark = pytest.mark.anyio


class Counter:
    """Something expensive to ask, that notes how often it was asked."""

    def __init__(self) -> None:
        self.asked = 0

    async def __call__(self) -> list[int]:
        self.asked += 1
        await anyio.sleep(0.01)
        return [self.asked]


async def test_the_work_is_done_once_and_remembered() -> None:
    work, remembered = Counter(), Remembered[list[int]](60)

    answers = [await remembered.get(work) for _ in range(3)]

    assert answers == [[1], [1], [1]]
    assert work.asked == 1


async def test_it_is_done_again_once_the_time_is_up(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1000.0
    # Only this module's clock: the event loop keeps time by the real one.
    monkeypatch.setattr(ttl, "monotonic", lambda: now)
    work, remembered = Counter(), Remembered[list[int]](60)
    await remembered.get(work)

    now = 1059.0
    still_fresh = await remembered.get(work)
    now = 1060.0
    stale = await remembered.get(work)

    assert (still_fresh, stale) == ([1], [2])


async def test_zero_seconds_remembers_nothing() -> None:
    work, remembered = Counter(), Remembered[list[int]](0)

    answers = [await remembered.get(work) for _ in range(2)]

    assert answers == [[1], [2]]


async def test_an_empty_answer_is_remembered_too() -> None:
    asked = 0

    async def nothing() -> list[int]:
        nonlocal asked
        asked += 1
        return []

    remembered = Remembered[list[int]](60)

    await remembered.get(nothing)
    await remembered.get(nothing)

    assert asked == 1


async def test_many_asking_at_once_cause_the_work_once() -> None:
    """Otherwise the moment the value expires, every waiting request asks the database."""
    work, remembered = Counter(), Remembered[list[int]](60)

    async with anyio.create_task_group() as group:
        for _ in range(10):
            group.start_soon(remembered.get, work)

    assert work.asked == 1
