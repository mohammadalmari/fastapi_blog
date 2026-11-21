"""
Shared fixtures.

Tests run against a real MongoDB, in a database with a random name that exists
only for this run (one per worker when run in parallel). Its collections are
emptied after every test, so tests can't see each other's data, and the
database is dropped at the end.

Which server: ``TEST_DATABASE_URL`` if set, else ``DATABASE_URL``, else the
app's default. Both are read from the environment or ``.env``.

Building an app and its indexes costs about 100 ms, so tests share one app. A
module that needs a different one (production mode, say) overrides the
module-scoped ``settings_overrides`` fixture and gets its own.

Async tests opt in with ``pytestmark = pytest.mark.anyio`` at the top of the
module.
"""

import os
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from pathlib import Path
from typing import Any, Protocol

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# The tests pin every setting they depend on, so nothing may leak in from the
# shell: drop all FASTAPI_* variables for this process. Then turn debug on,
# because importing app.main builds the module-level app from the environment
# (the tests never use that app), and outside debug it needs a secret key.
for _name in [name for name in os.environ if name.startswith("FASTAPI_")]:
    del os.environ[_name]
os.environ["FASTAPI_DEBUG"] = "true"

from app.core.database import Database, create_client, registered_collections  # noqa: E402
from app.core.security import create_access_token, hash_password, passwords  # noqa: E402
from app.core.settings import Settings  # noqa: E402
from app.core.settings.environment import ROOT_DIR  # noqa: E402
from app.main import create_app  # noqa: E402
from app.rbac.models import Role  # noqa: E402
from app.rbac.repository import PERMISSIONS, RoleRepository  # noqa: E402
from app.rbac.sync import ensure_system_roles  # noqa: E402
from app.users.models import User  # noqa: E402
from app.users.repository import UserRepository  # noqa: E402
from tests.helpers import ACCESS_TOKEN_TTL_MINUTES, PASSWORD, REFRESH_TOKEN_TTL_DAYS, bearer  # noqa: E402

# asgi-lifespan gives startup 5 seconds by default, which a busy database
# server can exceed; a slow start should be slow, not an error.
STARTUP_TIMEOUT = 30

# The hasher the app really uses, captured before any test replaces it.
PRODUCTION_HASHER = passwords._password_hash

# Argon2 at its weakest: a real hash, but in microseconds instead of 30 ms.
FAST_HASHER = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=8, parallelism=1),))


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


# --- password hashing -------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def fast_password_hashing() -> Generator[None]:
    """
    Hash passwords with cheap parameters in tests. Only the parameters change:
    the app's code path is the same. ``real_password_hashing`` opts out.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(passwords, "_password_hash", FAST_HASHER)
        passwords.dummy_hash.cache_clear()
        yield
    passwords.dummy_hash.cache_clear()


@pytest.fixture
def real_password_hashing(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """For the few tests that are about the production hasher itself."""
    monkeypatch.setattr(passwords, "_password_hash", PRODUCTION_HASHER)
    passwords.dummy_hash.cache_clear()
    yield
    passwords.dummy_hash.cache_clear()


# --- settings ---------------------------------------------------------------


@pytest.fixture(scope="session")
def database_name() -> str:
    return f"test_{uuid.uuid4().hex}"


@pytest.fixture(scope="session")
def database_url() -> SecretStr | None:
    urls = DatabaseUrls()
    return urls.test_database_url or urls.database_url


def _build_settings(
    database_name: str,
    database_url: SecretStr | None,
    log_dir: Path,
    overrides: dict[str, Any],
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
        "log_dir": log_dir,
        **overrides,
    }
    # `_env_file=None`: apart from the database URL, ignore the developer's
    # .env so runs are repeatable.
    settings = Settings(_env_file=None, DATABASE_NAME=database_name, **values)
    if database_url is not None:
        settings = settings.model_copy(update={"database_url": database_url})
    return settings


@pytest.fixture(scope="session")
def default_settings(
    database_name: str, database_url: SecretStr | None, tmp_path_factory: pytest.TempPathFactory
) -> Settings:
    return _build_settings(database_name, database_url, tmp_path_factory.mktemp("logs"), {})


@pytest.fixture(scope="module")
def settings_overrides() -> dict[str, Any]:
    """Override in a test module (keep it module-scoped) for other settings."""
    return {}


@pytest.fixture(scope="module")
def settings(
    default_settings: Settings,
    database_name: str,
    database_url: SecretStr | None,
    settings_overrides: dict[str, Any],
    tmp_path_factory: pytest.TempPathFactory,
) -> Settings:
    if not settings_overrides:
        return default_settings
    log_dir = tmp_path_factory.mktemp("logs")
    return _build_settings(database_name, database_url, log_dir, settings_overrides)


# --- app and database -------------------------------------------------------


@pytest.fixture(scope="session")
async def _drop_database_afterwards(default_settings: Settings) -> AsyncGenerator[None]:
    yield
    client = create_client(default_settings)
    # Once per run, not per test: besides costing 100 ms, on a replica set a
    # drop occasionally stalls for about five seconds (measured: roughly 1 in
    # 100, also when dropping the collections one by one). At the end of a run
    # that is a rare short wait; after every test it would be a slow suite.
    await client.drop_database(default_settings.database_name)
    await client.close()


@pytest.fixture(scope="session")
async def _default_app(
    default_settings: Settings, _drop_database_afterwards: None
) -> AsyncGenerator[FastAPI]:
    """The one app most tests share."""
    app = create_app(default_settings)
    # httpx doesn't run startup/shutdown; LifespanManager does.
    async with LifespanManager(app, startup_timeout=STARTUP_TIMEOUT):
        yield app


@pytest.fixture(scope="module")
async def _module_app(
    settings: Settings, default_settings: Settings, _default_app: FastAPI
) -> AsyncGenerator[FastAPI]:
    if settings is default_settings:
        yield _default_app
        return
    # This module overrides the settings, so it gets an app of its own. It
    # uses the same database, which the shared app has already prepared.
    app = create_app(settings)
    async with LifespanManager(app, startup_timeout=STARTUP_TIMEOUT):
        yield app


@pytest.fixture
async def app(_module_app: FastAPI) -> AsyncGenerator[FastAPI]:
    yield _module_app
    # Undo what a test may have left behind in the shared app and database.
    _module_app.dependency_overrides.clear()
    db: Database = _module_app.state.db
    for name in await db.list_collection_names():
        if name == PERMISSIONS:
            continue  # the catalog is the same for every test; startup stored it
        if name in registered_collections():
            await db[name].delete_many({})  # keeps the collection's indexes
        else:
            await db.drop_collection(name)
    # Emptying `roles` undid part of startup's work; redo it, so every test
    # starts with the two system roles in place.
    await ensure_system_roles(db)


@pytest.fixture
def db(app: FastAPI) -> Database:
    return app.state.db


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        yield client


# --- roles and users --------------------------------------------------------


class MakeRole(Protocol):
    def __call__(
        self, name: str, *permissions: str, is_default: bool = False
    ) -> Awaitable[Role]: ...


class MakeUser(Protocol):
    def __call__(
        self, username: str, *role_names: str, is_active: bool = True
    ) -> Awaitable[User]: ...


type HeadersFor = Callable[[User], dict[str, str]]


@pytest.fixture
async def roles(db: Database) -> dict[str, Role]:
    """The roles that exist when the test starts, by name: admin and reader."""
    return {role.name: role for role in await RoleRepository(db).list_all()}


@pytest.fixture
def make_role(db: Database) -> MakeRole:
    """``await make_role("author", Perm.POSTS_CREATE)`` creates an ordinary role."""

    async def make(name: str, *permissions: str, is_default: bool = False) -> Role:
        role = await RoleRepository(db).create(name=name, description="", permissions=permissions)
        if is_default:
            await db.roles.update_many({"is_default": True}, {"$set": {"is_default": False}})
            await db.roles.update_one({"_id": role.id}, {"$set": {"is_default": True}})
            role = role.model_copy(update={"is_default": True})
        return role

    return make


@pytest.fixture
def make_user(db: Database) -> MakeUser:
    """``await make_user("jane", "reader")`` puts a user straight into the database."""

    async def make(username: str, *role_names: str, is_active: bool = True) -> User:
        role_ids = []
        for name in role_names:
            role = await RoleRepository(db).get_by_name(name)
            # A typo here would quietly make a user with no roles, and a
            # "403" test would then pass for the wrong reason.
            assert role is not None, f"no role named {name!r}"
            role_ids.append(role.id)
        user = await UserRepository(db).create(
            email=f"{username}@example.com",
            username=username,
            password_hash=hash_password(PASSWORD),
            role_ids=role_ids,
        )
        if not is_active:
            await db.users.update_one({"_id": user.id}, {"$set": {"is_active": False}})
            user = user.model_copy(update={"is_active": False})
        return user

    return make


@pytest.fixture
async def user(make_user: MakeUser) -> User:
    """
    An ordinary user, as sign-up would leave them: the reader role. For tests
    that need a user but aren't about signing up; ``PASSWORD`` logs them in.
    """
    return await make_user("jane", "reader")


@pytest.fixture
async def admin(make_user: MakeUser) -> User:
    return await make_user("root", "admin")


@pytest.fixture
def access_token(user: User, settings: Settings) -> str:
    """A valid access token for ``user``, without going through login."""
    return create_access_token(user.id, settings)


@pytest.fixture
def headers_for(settings: Settings) -> HeadersFor:
    """``headers_for(someone)`` is the Authorization header that logs them in."""
    return lambda who: bearer(create_access_token(who.id, settings))


@pytest.fixture
def user_headers(user: User, headers_for: HeadersFor) -> dict[str, str]:
    return headers_for(user)


@pytest.fixture
def admin_headers(admin: User, headers_for: HeadersFor) -> dict[str, str]:
    return headers_for(admin)
