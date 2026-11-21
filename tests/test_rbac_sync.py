"""What startup does to the permission and role collections."""

import logging
import uuid

import pytest
from asgi_lifespan import LifespanManager

from app.core.database import Database, Document
from app.core.settings import Settings
from app.main import create_app
from app.rbac.permissions import READER_PERMISSIONS, Perm
from app.rbac.sync import sync_rbac

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("app")]

# These tests may leave `permissions` as they found it only: the collection is
# shared by the whole run and not reset between tests.

EVERYTHING = sorted(Perm)


async def _role(db: Database, name: str) -> Document:
    role = await db.roles.find_one({"name": name})
    assert role is not None
    return role


async def test_the_catalog_is_stored_whole(db: Database) -> None:
    stored = await db.permissions.find().sort("_id").to_list()

    assert [doc["_id"] for doc in stored] == EVERYTHING
    assert stored[0].keys() == {"_id", "resource", "description"}
    assert all(doc["resource"] == doc["_id"].split(":")[0] for doc in stored)


async def test_the_two_system_roles_exist(db: Database) -> None:
    admin, reader = await _role(db, "admin"), await _role(db, "reader")

    assert admin["permissions"] == EVERYTHING
    assert (admin["is_system"], admin["is_default"]) == (True, False)
    assert reader["permissions"] == sorted(READER_PERMISSIONS)
    assert (reader["is_system"], reader["is_default"]) == (True, True)
    assert await db.roles.count_documents({}) == 2


async def test_sync_changes_nothing_when_there_is_nothing_to_do(db: Database) -> None:
    """Not even `updated_at`: a restart isn't an edit."""
    before = await db.roles.find().sort("name").to_list()

    await sync_rbac(db)
    await sync_rbac(db)

    assert await db.roles.find().sort("name").to_list() == before
    assert await db.permissions.count_documents({}) == len(Perm)


async def test_sync_removes_a_permission_that_left_the_catalog(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    await db.permissions.insert_one({"_id": "posts:fly", "resource": "posts", "description": ""})
    await db.roles.insert_one(
        {"name": "pilot", "permissions": ["posts:fly", Perm.POSTS_CREATE], "is_system": False}
    )

    try:
        with caplog.at_level(logging.WARNING, logger="app.rbac.sync"):
            await sync_rbac(db)

        assert await db.permissions.find_one({"_id": "posts:fly"}) is None
        assert (await _role(db, "pilot"))["permissions"] == [Perm.POSTS_CREATE]
        # Loud, because this is permanent: it is what a rollback to older
        # code does to permissions that newer code added.
        assert "posts:fly" in caplog.text
    finally:
        # `permissions` isn't reset between tests; don't leave the row behind.
        await db.permissions.delete_one({"_id": "posts:fly"})


async def test_sync_gives_admin_back_every_permission(db: Database) -> None:
    await db.roles.update_one({"name": "admin"}, {"$set": {"permissions": [], "is_system": False}})

    await sync_rbac(db)

    admin = await _role(db, "admin")
    assert admin["permissions"] == EVERYTHING
    assert admin["is_system"] is True


async def test_sync_keeps_an_administrators_changes_to_reader(db: Database) -> None:
    await db.roles.update_one(
        {"name": "reader"}, {"$set": {"permissions": [Perm.LIKES_CREATE], "description": "mine"}}
    )

    await sync_rbac(db)

    reader = await _role(db, "reader")
    assert reader["permissions"] == [Perm.LIKES_CREATE]
    assert reader["description"] == "mine"


async def test_sync_keeps_another_default_role(db: Database) -> None:
    await db.roles.update_one({"name": "reader"}, {"$set": {"is_default": False}})
    await db.roles.insert_one({"name": "member", "permissions": [], "is_default": True})

    await sync_rbac(db)

    assert [role["name"] async for role in db.roles.find({"is_default": True})] == ["member"]


async def test_sync_makes_reader_the_default_when_there_is_none(db: Database) -> None:
    await db.roles.update_many({}, {"$set": {"is_default": False}})

    await sync_rbac(db)

    assert [role["name"] async for role in db.roles.find({"is_default": True})] == ["reader"]


async def test_sync_takes_the_default_away_from_admin(db: Database) -> None:
    await db.roles.update_many({}, {"$set": {"is_default": False}})
    await db.roles.update_one({"name": "admin"}, {"$set": {"is_default": True}})

    await sync_rbac(db)

    assert [role["name"] async for role in db.roles.find({"is_default": True})] == ["reader"]


async def test_starting_the_app_prepares_an_empty_database(settings: Settings) -> None:
    """
    The other tests here call `sync_rbac` themselves, and the fixtures put the
    system roles back after every test. Only this one shows that starting the
    app is what does it: a database nobody has touched, and a new app.
    """
    fresh = settings.model_copy(update={"database_name": f"test_startup_{uuid.uuid4().hex}"})
    app = create_app(fresh)

    async with LifespanManager(app):
        db: Database = app.state.db
        try:
            assert await db.permissions.count_documents({}) == len(Perm)
            assert [role["name"] async for role in db.roles.find().sort("name")] == [
                "admin",
                "reader",
            ]
            assert (await _role(db, "reader"))["is_default"] is True
            assert (await _role(db, "admin"))["permissions"] == EVERYTHING
        finally:
            await db.client.drop_database(db.name)
