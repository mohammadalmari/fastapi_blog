"""Bring the database in line with the permission catalog. Runs at startup."""

import contextlib
import logging

from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError

from app.core.database import Database, Document, utcnow
from app.rbac.permissions import ADMIN_ROLE, DESCRIPTIONS, READER_PERMISSIONS, READER_ROLE, Perm
from app.rbac.repository import PERMISSIONS, ROLES

log = logging.getLogger(__name__)

CATALOG = sorted(Perm)


async def sync_rbac(db: Database) -> None:
    """
    Safe to run any number of times, and by several workers at once.

    - Store the catalog in ``permissions``, and remove what has left it, from
      there and from every role.
    - Make sure the two system roles exist. ``admin`` is reset to hold every
      permission, so it gains new ones as the catalog grows. ``reader`` is
      created once and then left alone: an administrator may have tuned it.
    - Make sure exactly one role is the default, and that it isn't ``admin``.
    """
    await sync_catalog(db)
    await ensure_system_roles(db)


# The two halves are separate, and public, for the test fixtures: they redo
# only the second between tests.


async def sync_catalog(db: Database) -> None:
    await db[PERMISSIONS].bulk_write(
        [
            UpdateOne(
                {"_id": perm.value},
                {"$set": {"resource": perm.resource, "description": DESCRIPTIONS[perm]}},
                upsert=True,
            )
            for perm in Perm
        ]
    )

    # Removal is permanent, and it is also what starting an OLDER version of
    # the code does to permissions a newer version added: they are stripped
    # from every role and don't come back on upgrade. Hence the warning.
    stale = [doc["_id"] async for doc in db[PERMISSIONS].find({"_id": {"$nin": CATALOG}})]
    if stale:
        await db[PERMISSIONS].delete_many({"_id": {"$in": stale}})
    # From the roles by "not in the catalog" rather than by the list above: a
    # role can also name a permission the collection never held.
    cleaned = await db[ROLES].update_many(
        {"permissions": {"$elemMatch": {"$nin": CATALOG}}},
        {"$pull": {"permissions": {"$nin": CATALOG}}},
    )
    if stale or cleaned.modified_count:
        log.warning(
            "Removed permissions no longer in the code: %s (taken out of %d roles)",
            ", ".join(stale) or "(not in the catalog collection)",
            cleaned.modified_count,
        )


async def ensure_system_roles(db: Database) -> None:
    now = utcnow()
    created = {"is_default": False, "created_at": now, "updated_at": now}
    # `updated_at` is deliberately not touched when admin's permissions are
    # reset: a restart isn't an edit, and running this twice changes nothing.
    await _upsert_role(
        db,
        ADMIN_ROLE,
        {
            "$set": {"permissions": CATALOG, "is_system": True},
            "$setOnInsert": {"description": "Can do everything", **created},
        },
    )
    await _upsert_role(
        db,
        READER_ROLE,
        {
            "$set": {"is_system": True},
            "$setOnInsert": {
                "description": "Reads, comments and likes",
                "permissions": sorted(READER_PERMISSIONS),
                **created,
            },
        },
    )

    # New accounts get the default role, so it must exist and must not be
    # admin. The API refuses to make admin the default; this covers a
    # database edited by hand.
    await db[ROLES].update_one(
        {"name": ADMIN_ROLE, "is_default": True}, {"$set": {"is_default": False}}
    )
    if await db[ROLES].count_documents({"is_default": True}, limit=1) == 0:
        # Suppressed: another worker, or an administrator, set one meanwhile.
        with contextlib.suppress(DuplicateKeyError):
            await db[ROLES].update_one({"name": READER_ROLE}, {"$set": {"is_default": True}})


async def _upsert_role(db: Database, name: str, update: Document) -> None:
    try:
        await db[ROLES].update_one({"name": name}, update, upsert=True)
    except DuplicateKeyError:
        # Another worker starting at the same moment inserted the role between
        # our "does it exist" and our insert. It exists now; apply the update
        # to it, which can no longer insert.
        await db[ROLES].update_one({"name": name}, update)
