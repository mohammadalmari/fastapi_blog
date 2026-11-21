"""
Shared fixtures.

Tests run against a real MongoDB (``docker compose up -d``), in a database
with a random name that exists only for this run. Every test gets a freshly
built app and the database is dropped after each one, so tests can't see each
other's data.

Async tests opt in with ``pytestmark = pytest.mark.anyio`` at the top of the
module.
"""

import os
import uuid
from collections.abc import AsyncGenerator

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

# Importing app.main builds the module-level app from the real environment,
# which refuses to start in production mode without a secret key. The tests
# never use that app; this only lets the import succeed on a bare machine.
os.environ.setdefault("FASTAPI_DEBUG", "true")

from app.core.database import Database
from app.core.settings import Settings
from app.main import create_app


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def database_name() -> str:
    return f"test_{uuid.uuid4().hex}"


@pytest.fixture
def settings(database_name: str) -> Settings:
    # `_env_file=None`: ignore the developer's .env so runs are repeatable.
    return Settings(_env_file=None, debug=True, DATABASE_NAME=database_name)


@pytest.fixture
async def app(settings: Settings) -> AsyncGenerator[FastAPI]:
    app = create_app(settings)
    # httpx doesn't run startup/shutdown; LifespanManager does.
    async with LifespanManager(app):
        yield app
        db: Database = app.state.db
        await db.client.drop_database(db.name)


@pytest.fixture
def db(app: FastAPI) -> Database:
    return app.state.db


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client
