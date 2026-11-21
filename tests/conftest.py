"""
Shared fixtures.

Tests run against a real MongoDB, in a database with a random name that exists
only for this run. Every test gets a freshly built app and the database is
dropped after each one, so tests can't see each other's data.

Which server: ``TEST_DATABASE_URL`` if set, else ``DATABASE_URL``, else the
app's default. Both are read from the environment or ``.env``.

Async tests opt in with ``pytestmark = pytest.mark.anyio`` at the top of the
module.
"""

import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Importing app.main builds the module-level app from the real environment,
# which refuses to start in production mode without a secret key. The tests
# never use that app; this only lets the import succeed on a bare machine.
os.environ.setdefault("FASTAPI_DEBUG", "true")

from app.core.database import Database
from app.core.settings import Settings
from app.core.settings.environment import ROOT_DIR
from app.main import create_app
from tests.helpers import ACCESS_TOKEN_TTL_MINUTES, REFRESH_TOKEN_TTL_DAYS


class DatabaseUrls(BaseSettings):
    """Where the tests may find a MongoDB. Nothing else is read from .env."""

    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env", env_ignore_empty=True, extra="ignore"
    )

    test_database_url: SecretStr | None = Field(default=None, validation_alias="TEST_DATABASE_URL")
    database_url: SecretStr | None = Field(default=None, validation_alias="DATABASE_URL")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def database_name() -> str:
    return f"test_{uuid.uuid4().hex}"


@pytest.fixture(scope="session")
def database_url() -> SecretStr | None:
    urls = DatabaseUrls()
    return urls.test_database_url or urls.database_url


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    """Override in a test module to build the app with other settings."""
    return {}


@pytest.fixture
def settings(
    database_name: str,
    database_url: SecretStr | None,
    settings_overrides: dict[str, Any],
    tmp_path: Path,
) -> Settings:
    values: dict[str, Any] = {
        "debug": True,
        # Pinned, so that FASTAPI_* variables in the shell can't change what
        # the tests assert on.
        "access_token_ttl_minutes": ACCESS_TOKEN_TTL_MINUTES,
        "refresh_token_ttl_days": REFRESH_TOKEN_TTL_DAYS,
        "cors_allowed_origins": [],
        "cors_allow_credentials": False,
        # Production-mode apps write log files; keep them out of the project.
        "log_dir": tmp_path / "logs",
        **settings_overrides,
    }
    # `_env_file=None`: apart from the database URL above, ignore the
    # developer's .env so runs are repeatable.
    settings = Settings(_env_file=None, DATABASE_NAME=database_name, **values)
    if database_url is not None:
        settings = settings.model_copy(update={"database_url": database_url})
    return settings


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
