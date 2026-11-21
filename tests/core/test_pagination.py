import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from bson import ObjectId
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.database import Database
from app.core.errors import FieldError
from app.core.pagination import (
    CursorPosition,
    Page,
    PageParams,
    PageParamsDep,
    cursor_filter,
    cursor_sort,
    decode_cursor,
    encode_cursor,
    split_page,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def test_cursor_round_trips() -> None:
    position = CursorPosition(START + timedelta(milliseconds=123), ObjectId())

    assert decode_cursor(encode_cursor(position)) == position


def _forged(*payload: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(list(payload)).encode()).decode()


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "not-base64!",
        "aGVsbG8=",  # base64, but not JSON
        _forged(1, 2),
        _forged(),
        _forged("2024-01-01T00:00:00+00:00"),
        # The attack: a cursor is not signed, so anyone can write one. Each of
        # these once got past decoding and crashed later, as a 500.
        _forged("2024-01-01T00:00:00+00:00", "zzz"),
        _forged("2024-01-01T00:00:00+00:00", 5),
        _forged("0001-01-01T00:00:00+23:59", "a" * 24),
        _forged("9999-12-31T23:59:59-23:59", "a" * 24),
        _forged({"$gt": ""}, "a" * 24),
    ],
)
def test_a_cursor_that_cannot_be_read_is_refused(cursor: str) -> None:
    with pytest.raises(FieldError) as raised:
        decode_cursor(cursor)

    assert raised.value.status_code == 422
    assert (raised.value.where, raised.value.field) == ("query", "cursor")


def test_a_cursor_without_a_timezone_is_read_as_utc() -> None:
    position = decode_cursor(_forged("2026-01-01T00:00:00", "a" * 24))

    assert position.sort_value == START


def test_split_page_without_a_next_page() -> None:
    docs = [{"_id": ObjectId(), "at": START}]

    assert split_page(docs, 5, "at") == (docs, None)
    assert split_page(docs, 1, "at") == (docs, None)


async def _walk(db: Database, *, limit: int, descending: bool) -> list[list[int]]:
    """Read every page of ``db.items`` and return the ``n`` values per page."""
    pages: list[list[int]] = []
    cursor: str | None = None
    while True:
        query = cursor_filter("at", decode_cursor(cursor), descending=descending) if cursor else {}
        docs = await (
            db.items.find(query)
            .sort(cursor_sort("at", descending=descending))
            .limit(limit + 1)
            .to_list()
        )
        docs, cursor = split_page(docs, limit, "at")
        pages.append([doc["n"] for doc in docs])
        if cursor is None:
            return pages


@pytest.mark.anyio
@pytest.mark.parametrize("descending", [True, False])
async def test_pages_cover_every_item_once_even_with_equal_sort_values(
    db: Database, descending: bool
) -> None:
    # Items 0-1 share a timestamp, as do 2-3 and so on, so a page boundary
    # falls between two items that only their ids can tell apart.
    await db.items.insert_many(
        [{"n": n, "at": START + timedelta(minutes=n // 2)} for n in range(7)]
    )

    pages = await _walk(db, limit=3, descending=descending)

    expected = list(range(7))
    if descending:
        expected.reverse()
    assert [len(page) for page in pages] == [3, 3, 1]
    assert [n for page in pages for n in page] == expected


@pytest.mark.anyio
async def test_query_parameters() -> None:
    app = FastAPI()

    @app.get("/things")
    async def list_things(params: PageParamsDep) -> Page[int]:
        return Page(items=[params.limit], next_cursor=params.cursor)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        default = await client.get("/things")
        custom = await client.get("/things", params={"limit": 100, "cursor": "abc"})
        too_big = await client.get("/things", params={"limit": 101})
        too_small = await client.get("/things", params={"limit": 0})

    assert default.json() == {"items": [PageParams().limit], "next_cursor": None}
    assert custom.json() == {"items": [100], "next_cursor": "abc"}
    assert too_big.status_code == 422
    assert too_small.status_code == 422


@pytest.mark.anyio
async def test_query_parameters_that_do_not_exist_are_refused() -> None:
    """A misspelt filter should be an error, not a silently unfiltered list."""
    app = FastAPI()

    @app.get("/things")
    async def list_things(params: PageParamsDep) -> Page[int]:
        return Page(items=[], next_cursor=None)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        misspelt = await client.get("/things", params={"limti": 5})
        too_long = await client.get("/things", params={"cursor": "x" * 201})

    assert misspelt.status_code == 422
    assert too_long.status_code == 422
