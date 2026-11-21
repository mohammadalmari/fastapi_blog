"""
Cursor pagination.

A page is asked for with ``?limit=20&cursor=<opaque string>``. The cursor names
the last item of the previous page (its sort value and its id), and the next
page is "everything after that item". Unlike ``skip``/``offset`` this stays
fast on deep pages and doesn't repeat or lose items when new ones arrive.

Typical use in a repository::

    query = {"status": "published"}
    if params.cursor:
        query |= cursor_filter("published_at", decode_cursor(params.cursor), descending=True)
    docs = await collection.find(query).sort(cursor_sort("published_at", descending=True))
                           .limit(params.limit + 1).to_list()
    docs, next_cursor = split_page(docs, params.limit, "published_at")
"""

import base64
import binascii
import json
from datetime import datetime
from typing import Annotated, NamedTuple

from bson import ObjectId
from fastapi import HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.database import Document

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


class PageParams(BaseModel):
    """The query parameters of every paginated endpoint."""

    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    cursor: str | None = Field(
        default=None,
        description="`next_cursor` from the previous page. Omit for the first page.",
    )


PageParamsDep = Annotated[PageParams, Query()]


class Page[T](BaseModel):
    """The response shape of every paginated endpoint."""

    items: list[T]
    next_cursor: str | None = Field(description="Null when this is the last page.")


class CursorPosition(NamedTuple):
    """The item a page ended on: its sort value, and its id as the tie-breaker."""

    sort_value: datetime
    id: ObjectId


def encode_cursor(position: CursorPosition) -> str:
    payload = json.dumps([position.sort_value.isoformat(), str(position.id)])
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(cursor: str) -> CursorPosition:
    """Read a cursor sent by a client. A cursor we can't read is a 422."""
    try:
        sort_value, item_id = json.loads(base64.urlsafe_b64decode(cursor))
        return CursorPosition(datetime.fromisoformat(sort_value), ObjectId(item_id))
    except (binascii.Error, TypeError, ValueError) as error:
        # ValueError also covers bad JSON, bad UTF-8 and a bad ObjectId.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid cursor") from error


def cursor_sort(field: str, *, descending: bool) -> list[tuple[str, int]]:
    """The sort that goes with ``cursor_filter``. ``_id`` breaks ties."""
    direction = -1 if descending else 1
    return [(field, direction), ("_id", direction)]


def cursor_filter(field: str, position: CursorPosition, *, descending: bool) -> Document:
    """The query condition for "items that come after ``position``"."""
    op = "$lt" if descending else "$gt"
    return {
        "$or": [
            {field: {op: position.sort_value}},
            {field: position.sort_value, "_id": {op: position.id}},
        ]
    }


def split_page(docs: list[Document], limit: int, field: str) -> tuple[list[Document], str | None]:
    """
    Turn ``limit + 1`` fetched documents into a page and its ``next_cursor``.

    Fetching one extra document is how we know whether another page exists
    without running a second query.
    """
    if len(docs) <= limit:
        return docs, None
    page = docs[:limit]
    last = page[-1]
    return page, encode_cursor(CursorPosition(last[field], last["_id"]))
