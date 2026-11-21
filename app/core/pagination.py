"""
Cursor pagination.

A page is asked for with ``?limit=20&cursor=<opaque string>``. The cursor names
the last item of the previous page (its sort value and its id), and the next
page is "everything after that item". Unlike ``skip``/``offset`` this stays
fast on deep pages and doesn't repeat or lose items when new ones arrive.

In a route, ``params.after()`` reads the cursor. In a repository::

    query = {"status": "published"}
    if after is not None:
        query |= cursor_filter("published_at", after, descending=True)
    docs = await collection.find(query).sort(cursor_sort("published_at", descending=True))
                           .limit(limit + 1).to_list()
    docs, next_cursor = split_page(docs, limit, "published_at")

For this to be fast, the collection needs an index that ends with the sort
field *and* ``_id``, in that order: the sort uses both.
"""

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Annotated, NamedTuple

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field

from app.core.database import Document
from app.core.errors import FieldError

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
# One of our own cursors is about 90 characters.
CURSOR_MAX_LENGTH = 200


class PageParams(BaseModel):
    """The query parameters of every paginated endpoint. Extend it to add filters."""

    # A parameter the endpoint doesn't have is refused, not ignored: a
    # misspelt filter should be an error, not a quietly unfiltered list.
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Items per page.")
    cursor: str | None = Field(
        default=None,
        max_length=CURSOR_MAX_LENGTH,
        description="`next_cursor` from the previous page. Omit for the first page.",
    )

    def after(self) -> CursorPosition | None:
        """Where the previous page ended, or None for the first page."""
        return decode_cursor(self.cursor) if self.cursor else None


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
    """
    Read a cursor sent by a client; one we can't read is a 422.

    A cursor is not signed, so a client can send anything, and "anything"
    must end here. Everything this function lets through is safe to put in a
    query: a real date that MongoDB can store, and a real id. A forged cursor
    can then only move the client's own position in a list, never widen what
    the list contains, because the list's filters are applied separately.
    """
    try:
        sort_value, item_id = json.loads(base64.urlsafe_b64decode(cursor))
        moment = datetime.fromisoformat(sort_value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        # Converting to UTC is what catches dates at the very edge of the
        # range, such as year 1 with a positive offset: they overflow here
        # rather than later, inside the database driver.
        return CursorPosition(moment.astimezone(UTC), ObjectId(item_id))
    except (binascii.Error, TypeError, ValueError, OverflowError, InvalidId) as error:
        # ValueError also covers bad JSON and bad UTF-8, and TypeError a
        # value that isn't a string (a number, a nested object). InvalidId is
        # not a ValueError, which an earlier version of this comment assumed.
        raise FieldError("cursor", ["Invalid cursor"], where="query") from error


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
