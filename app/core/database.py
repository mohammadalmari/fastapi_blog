"""
MongoDB access through PyMongo's native async client. There is no ODM:
repositories work with plain documents and map them to Pydantic models by hand.
"""

from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, Request
from pymongo import AsyncMongoClient, IndexModel
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase

from app.core.settings import Settings

type Document = dict[str, Any]
type Client = AsyncMongoClient[Document]
type Database = AsyncDatabase[Document]
type Session = AsyncClientSession

# How long to look for a reachable server before giving up. The driver's
# default is 30 seconds, which makes "Mongo isn't running" slow to notice.
SERVER_SELECTION_TIMEOUT_MS = 5000

_indexes: dict[str, list[IndexModel]] = {}


def register_indexes(collection: str, indexes: list[IndexModel]) -> None:
    """
    Declare a collection's indexes; they are created at startup.

    Each repository module calls this once at import. That only works for
    modules that get imported, which today means reachable from a router that
    ``app.main`` includes; ``tests/core/test_database.py`` checks the result.
    """
    if collection in _indexes:
        raise RuntimeError(f"Indexes for {collection!r} are registered twice")
    _indexes[collection] = indexes


def registered_collections() -> frozenset[str]:
    """The collections that have registered indexes."""
    return frozenset(_indexes)


def utcnow() -> datetime:
    """
    The current time as MongoDB will store it: UTC, to the millisecond.

    BSON dates have no microseconds. Truncating up front means a document we
    just built and the same document read back hold the same timestamp.
    """
    now = datetime.now(UTC)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


async def in_transaction[T](db: Database, work: Callable[[Session], Coroutine[Any, Any, T]]) -> T:
    """
    Run ``work`` in a transaction: all of its writes happen, or none do.

    ``work`` gets a session and must pass it to every query that belongs to
    the transaction. It may run more than once: when two transactions touch
    the same document, MongoDB aborts one of them and the driver retries it,
    so ``work`` must not have side effects outside the database.
    """
    async with db.client.start_session() as session:
        return await session.with_transaction(work)


def create_client(settings: Settings) -> Client:
    """
    Build the client. Creating it does not connect; the first operation does.

    One client is shared by the whole app: it owns a connection pool.
    """
    return AsyncMongoClient(
        settings.database_url.get_secret_value(),
        # Return timezone-aware UTC datetimes instead of naive ones.
        tz_aware=True,
        serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS,
    )


async def ensure_indexes(db: Database) -> None:
    # Creating an index that already exists does nothing. Changing the options
    # of an existing one fails here: drop the old index in the same change.
    for collection, indexes in _indexes.items():
        await db[collection].create_indexes(indexes)


# `async def` although nothing is awaited: FastAPI runs plain `def`
# dependencies in its thread pool, which is a wasted thread hop for a lookup.
async def get_db(request: Request) -> Database:
    """The database opened by the app's lifespan (see ``app.main``)."""
    return request.app.state.db


DbDep = Annotated[Database, Depends(get_db)]
