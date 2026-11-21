"""
MongoDB access through PyMongo's native async client. There is no ODM:
repositories work with plain documents and map them to Pydantic models by hand.
"""

from typing import Annotated, Any

from fastapi import Depends, Request
from pymongo import AsyncMongoClient, IndexModel
from pymongo.asynchronous.database import AsyncDatabase

from app.core.settings import Settings

type Document = dict[str, Any]
type Client = AsyncMongoClient[Document]
type Database = AsyncDatabase[Document]

# How long to look for a reachable server before giving up. The driver's
# default is 30 seconds, which makes "Mongo isn't running" slow to notice.
SERVER_SELECTION_TIMEOUT_MS = 5000

# Indexes per collection. Each feature adds its own entry here; they are
# created at startup. Creating an index that already exists does nothing.
INDEXES: dict[str, list[IndexModel]] = {}


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
    for collection, indexes in INDEXES.items():
        await db[collection].create_indexes(indexes)


def get_db(request: Request) -> Database:
    """The database opened by the app's lifespan (see ``app.main``)."""
    return request.app.state.db


DbDep = Annotated[Database, Depends(get_db)]
