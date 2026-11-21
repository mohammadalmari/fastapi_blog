from datetime import UTC, datetime, timedelta

import pytest
from bson import ObjectId
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.core.database import Database
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


@pytest.mark.parametrize("cursor", ["", "not-base64!", "aGVsbG8=", "WzEsIDJd", "W10="])
def test_a_bad_cursor_is_a_422(cursor: str) -> None:
    with pytest.raises(HTTPException) as raised:
        decode_cursor(cursor)

    assert raised.value.status_code == 422


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
