from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from pymongo import AsyncMongoClient

from app.core.database import Database, Document, get_db

pytestmark = pytest.mark.anyio


async def test_health_pings_the_database(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_is_503_when_the_database_is_down(app: FastAPI, client: AsyncClient) -> None:
    # Nothing listens on port 1, so the ping fails after the short timeout.
    unreachable: AsyncMongoClient[Document] = AsyncMongoClient(
        "mongodb://localhost:1", serverSelectionTimeoutMS=200
    )
    app.dependency_overrides[get_db] = lambda: unreachable["nowhere"]

    response = await client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "Database unavailable"}
    await unreachable.close()


async def test_datetimes_come_back_timezone_aware(db: Database) -> None:
    await db.things.insert_one({"at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)})

    doc = await db.things.find_one({})

    assert doc is not None
    assert doc["at"] == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert doc["at"].tzinfo is not None


async def test_transactions_work(db: Database) -> None:
    """Fails on a standalone MongoDB: transactions need a replica set."""
    async with db.client.start_session() as session, await session.start_transaction():
        await db.things.insert_one({"n": 1}, session=session)
        await db.things.insert_one({"n": 2}, session=session)

    assert await db.things.count_documents({}) == 2


async def test_an_aborted_transaction_writes_nothing(db: Database) -> None:
    # A collection can't be created inside a transaction that also aborts
    # cleanly on every server version, so make sure it exists first.
    await db.create_collection("things")

    async with db.client.start_session() as session:
        await session.start_transaction()
        await db.things.insert_one({"n": 1}, session=session)
        await session.abort_transaction()

    assert await db.things.count_documents({}) == 0
