"""Every MongoDB query on the ``roles`` and ``permissions`` collections."""

from collections.abc import Iterable

from bson import ObjectId
from pymongo import IndexModel
from pymongo.errors import DuplicateKeyError

from app.core.database import Database, Document, Session, register_indexes, utcnow
from app.rbac.models import Role

ROLES = "roles"
PERMISSIONS = "permissions"

register_indexes(
    ROLES,
    [
        IndexModel("name", unique=True),
        # "Exactly one default role", enforced by the database: a unique index
        # that only covers documents whose flag is true, so a second true is a
        # duplicate while any number of roles may be false.
        IndexModel(
            "is_default",
            name="one_default_role",
            unique=True,
            partialFilterExpression={"is_default": True},
        ),
    ],
)


class DuplicateRoleError(Exception):
    """Another role already has this name."""


def _to_role(doc: Document) -> Role:
    return Role.model_validate({**doc, "id": doc["_id"]})


class RoleRepository:
    def __init__(self, db: Database) -> None:
        self._roles = db[ROLES]

    # Not named `list`: inside the class that would shadow the builtin used
    # in the annotations below.
    async def list_all(self) -> list[Role]:
        return [_to_role(doc) async for doc in self._roles.find().sort("name")]

    async def get(self, role_id: ObjectId, *, session: Session | None = None) -> Role | None:
        doc = await self._roles.find_one({"_id": role_id}, session=session)
        return _to_role(doc) if doc else None

    async def get_many(
        self, role_ids: Iterable[ObjectId], *, session: Session | None = None
    ) -> list[Role]:
        """The roles with these ids, by name. Ids that match nothing are skipped."""
        cursor = self._roles.find({"_id": {"$in": list(role_ids)}}, session=session).sort("name")
        return [_to_role(doc) async for doc in cursor]

    async def get_by_name(self, name: str) -> Role | None:
        doc = await self._roles.find_one({"name": name})
        return _to_role(doc) if doc else None

    async def get_default(self) -> Role | None:
        doc = await self._roles.find_one({"is_default": True})
        return _to_role(doc) if doc else None

    async def create(self, *, name: str, description: str, permissions: Iterable[str]) -> Role:
        now = utcnow()
        doc: Document = {
            "name": name,
            "description": description,
            "permissions": sorted(set(permissions)),
            "is_system": False,
            "is_default": False,
            "created_at": now,
            "updated_at": now,
        }
        try:
            await self._roles.insert_one(doc)
        except DuplicateKeyError as error:
            raise DuplicateRoleError from error
        return _to_role(doc)

    async def update(self, role_id: ObjectId, changes: Document) -> Role | None:
        """Set the given fields. None if the role is gone."""
        try:
            doc = await self._roles.find_one_and_update(
                {"_id": role_id},
                {"$set": {**changes, "updated_at": utcnow()}},
                return_document=True,
            )
        except DuplicateKeyError as error:
            raise DuplicateRoleError from error
        return _to_role(doc) if doc else None

    async def move_default_to(self, role_id: ObjectId, *, session: Session) -> Role | None:
        """
        Make this role the default one, taking the flag from whichever had it.
        None, and nothing changed, if the role doesn't exist.
        """
        if await self._roles.count_documents({"_id": role_id}, session=session) == 0:
            return None
        # Cleared first, set second: the unique index allows one default at a
        # time, even in the middle of a transaction.
        await self._roles.update_many(
            {"is_default": True}, {"$set": {"is_default": False}}, session=session
        )
        doc = await self._roles.find_one_and_update(
            {"_id": role_id},
            {"$set": {"is_default": True, "updated_at": utcnow()}},
            return_document=True,
            session=session,
        )
        return _to_role(doc) if doc else None

    async def delete_ordinary(self, role_id: ObjectId, *, session: Session) -> bool:
        """Delete the role unless it is a system role or the default. True if deleted."""
        result = await self._roles.delete_one(
            {"_id": role_id, "is_system": {"$ne": True}, "is_default": {"$ne": True}},
            session=session,
        )
        return result.deleted_count == 1

    async def lock(self, role_id: ObjectId, *, session: Session) -> None:
        """
        Make this transaction conflict with any other that also locks the role.

        A write to the same document from two concurrent transactions makes
        MongoDB abort one of them, and the driver then retries it after the
        other has finished. So two transactions that both lock a role run one
        after the other, each seeing what the previous one did.

        A counter, because the write has to change the document every time:
        setting a field to the value it already has is no write at all, and
        would lock nothing.
        """
        await self._roles.update_one(
            {"_id": role_id}, {"$inc": {"lock_version": 1}}, session=session
        )
