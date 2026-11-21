"""The seed command: the optional built-in roles and the first admin."""

from typing import Any

import pytest
from httpx import AsyncClient

from app.core.database import Database
from app.core.settings import Settings
from app.rbac.permissions import OPTIONAL_ROLES, Perm
from app.scripts.seed import SeedError, seed
from tests.conftest import MakeRole, MakeUser
from tests.helpers import API, bearer, login

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("app")]

ADMIN: dict[str, Any] = {
    "email": "Root@Example.com",
    "username": "Root",
    "password": "a long seed passphrase",
}


async def _admin_id(db: Database) -> object:
    admin = await db.roles.find_one({"name": "admin"})
    assert admin is not None
    return admin["_id"]


# --- creating the first admin ---------------------------------------------------


async def test_seed_creates_the_optional_roles(db: Database, settings: Settings) -> None:
    result = await seed(db, settings, **ADMIN)

    assert sorted(result.roles_created) == ["author", "editor", "moderator"]
    names = [role["name"] async for role in db.roles.find().sort("name")]
    assert names == ["admin", "author", "editor", "moderator", "reader"]
    author = await db.roles.find_one({"name": "author"})
    assert author is not None
    assert author["permissions"] == sorted(OPTIONAL_ROLES["author"].permissions)
    assert (author["is_system"], author["is_default"]) == (False, False)


async def test_seed_creates_an_admin_who_can_log_in(
    client: AsyncClient, db: Database, settings: Settings
) -> None:
    result = await seed(db, settings, **ADMIN)

    assert result.outcome == "created"
    logged_in = await login(client, identifier="root@example.com", password=ADMIN["password"])
    me = await client.get(f"{API}/users/me", headers=bearer(logged_in.json()["access_token"]))
    assert [role["name"] for role in me.json()["roles"]] == ["admin"]
    assert me.json()["permissions"] == sorted(Perm)


async def test_seed_keeps_a_role_an_administrator_has_edited(
    db: Database, settings: Settings, make_role: MakeRole
) -> None:
    await make_role("author", Perm.LIKES_CREATE)

    result = await seed(db, settings, **ADMIN)

    author = await db.roles.find_one({"name": "author"})
    assert author is not None
    assert author["permissions"] == [Perm.LIKES_CREATE]
    assert "author" not in result.roles_created


@pytest.mark.parametrize(
    ("fields", "complaint"),
    [
        ({"password": "short"}, "at least 8 characters"),
        ({"password": None}, "password"),
        ({"email": "not-an-email"}, "email"),
        ({"username": "no spaces"}, "username"),
        ({"username": None}, "username"),
    ],
)
async def test_seed_refuses_bad_input(
    db: Database, settings: Settings, fields: dict[str, Any], complaint: str
) -> None:
    with pytest.raises(SeedError, match=complaint):
        await seed(db, settings, **(ADMIN | fields))

    assert await db.users.count_documents({}) == 0
    assert await db.roles.count_documents({}) == 2  # refused before anything was written


async def test_seed_refuses_an_email_that_already_has_an_account(
    db: Database, settings: Settings, make_user: MakeUser
) -> None:
    """
    The attack: sign-up is open and emails aren't verified, so a stranger
    registers the operator's email first. Seeding must not then crown them.
    """
    stranger = await make_user("root", "reader")  # root@example.com

    with pytest.raises(SeedError, match="--promote") as refused:
        await seed(
            db,
            settings,
            email=ADMIN["email"],
            username="the_operator",
            password=ADMIN["password"],
        )

    assert "root" in str(refused.value)  # says whose account it is
    stored = await db.users.find_one({"_id": stranger.id})
    assert stored is not None
    assert await _admin_id(db) not in stored["role_ids"]
    assert await db.users.count_documents({}) == 1


async def test_seed_refuses_a_username_that_is_taken(
    db: Database, settings: Settings, make_user: MakeUser
) -> None:
    await make_user("root", "reader")

    with pytest.raises(SeedError, match="username root is already taken"):
        await seed(
            db,
            settings,
            email="someone.else@example.com",
            username=ADMIN["username"],
            password=ADMIN["password"],
        )

    assert await db.users.count_documents({}) == 1
    assert await db.roles.count_documents({}) == 2  # refused before anything was written


async def test_seed_works_on_a_database_the_app_never_started_against(
    db: Database, settings: Settings
) -> None:
    """The operator may seed before the first start: seed prepares what it needs."""
    await db.roles.delete_many({})

    result = await seed(db, settings, **ADMIN)

    assert result.outcome == "created"
    assert await db.roles.count_documents({"name": {"$in": ["admin", "reader"]}}) == 2
    admin = await db.users.find_one({})
    assert admin is not None
    assert admin["role_ids"] == [await _admin_id(db)]


# --- promoting an existing account ----------------------------------------------


async def test_promote_gives_an_existing_account_the_admin_role(
    db: Database, settings: Settings, make_user: MakeUser
) -> None:
    existing = await make_user("jane", "reader")

    result = await seed(db, settings, email="Jane@Example.com", promote=True)

    assert result.outcome == "promoted"
    stored = await db.users.find_one({"_id": existing.id})
    assert stored is not None
    assert await _admin_id(db) in stored["role_ids"]
    assert existing.role_ids[0] in stored["role_ids"]  # the roles they had are kept
    assert stored["password_hash"] == existing.password_hash
    assert stored["username"] == "jane"


async def test_promote_twice_changes_nothing(
    db: Database, settings: Settings, make_user: MakeUser
) -> None:
    await make_user("jane", "reader")
    await seed(db, settings, email="jane@example.com", promote=True)
    before = (await db.users.find().to_list(), await db.roles.find().sort("name").to_list())

    result = await seed(db, settings, email="jane@example.com", promote=True)

    assert result.outcome == "unchanged"
    assert (await db.users.find().to_list(), await db.roles.find().sort("name").to_list()) == before


@pytest.mark.parametrize(
    ("email", "complaint"),
    [("nobody@example.com", "No account"), ("gone@example.com", "deactivated"), ("x", "email")],
)
async def test_promote_refuses_an_account_that_cannot_use_it(
    db: Database, settings: Settings, make_user: MakeUser, email: str, complaint: str
) -> None:
    gone = await make_user("gone", "reader", is_active=False)

    with pytest.raises(SeedError, match=complaint):
        await seed(db, settings, email=email, promote=True)

    stored = await db.users.find_one({"_id": gone.id})
    assert stored is not None
    assert await _admin_id(db) not in stored["role_ids"]
