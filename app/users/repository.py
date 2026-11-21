"""Every MongoDB query on the ``users`` collection."""

from collections.abc import Sequence
from typing import Literal

from bson import ObjectId
from pymongo import IndexModel
from pymongo.errors import DuplicateKeyError

from app.core.database import Database, Document, Session, register_indexes, utcnow
from app.users.models import User
from app.users.schemas import normalize_login_identifier

COLLECTION = "users"

# Unique indexes are what really prevent duplicates: checking first and
# inserting after would let two simultaneous sign-ups both pass the check.
register_indexes(
    COLLECTION,
    [
        IndexModel("email", unique=True),
        IndexModel("username", unique=True),
    ],
)


class DuplicateUserError(Exception):
    """The email or username is already taken; ``field`` says which."""

    def __init__(self, field: Literal["email", "username"]) -> None:
        super().__init__(f"duplicate {field}")
        self.field: Literal["email", "username"] = field


def _to_user(doc: Document) -> User:
    # Mapped by hand: Mongo calls the id "_id", the model calls it "id".
    return User.model_validate({**doc, "id": doc["_id"]})


class UserRepository:
    def __init__(self, db: Database) -> None:
        self._users = db[COLLECTION]

    async def create(
        self,
        *,
        email: str,
        username: str,
        password_hash: str,
        role_ids: Sequence[ObjectId] = (),
    ) -> User:
        now = utcnow()
        doc: Document = {
            "email": email,
            "username": username,
            "password_hash": password_hash,
            "display_name": None,
            "bio": None,
            "avatar_key": None,
            "role_ids": list(role_ids),
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        try:
            await self._users.insert_one(doc)  # adds "_id" to doc
        except DuplicateKeyError as error:
            # The server reports which index was violated. Without that detail
            # (it is always there on current servers) assume the email.
            pattern = (error.details or {}).get("keyPattern", {})
            raise DuplicateUserError("username" if "username" in pattern else "email") from error
        return _to_user(doc)

    async def get(self, user_id: ObjectId, *, session: Session | None = None) -> User | None:
        doc = await self._users.find_one({"_id": user_id}, session=session)
        return _to_user(doc) if doc else None

    async def get_by_email(self, email: str) -> User | None:
        doc = await self._users.find_one({"email": email})
        return _to_user(doc) if doc else None

    async def get_by_login(self, identifier: str) -> User | None:
        """Find a user by what they typed in the login form: email or username."""
        field, value = normalize_login_identifier(identifier)
        doc = await self._users.find_one({field: value})
        return _to_user(doc) if doc else None

    async def set_password_hash(self, user_id: ObjectId, password_hash: str) -> None:
        await self._users.update_one(
            {"_id": user_id},
            {"$set": {"password_hash": password_hash, "updated_at": utcnow()}},
        )

    async def set_roles(
        self, user_id: ObjectId, role_ids: Sequence[ObjectId], *, session: Session | None = None
    ) -> User | None:
        doc = await self._users.find_one_and_update(
            {"_id": user_id},
            {"$set": {"role_ids": list(role_ids), "updated_at": utcnow()}},
            return_document=True,
            session=session,
        )
        return _to_user(doc) if doc else None

    async def add_role(self, user_id: ObjectId, role_id: ObjectId) -> None:
        await self._users.update_one(
            {"_id": user_id},
            {"$addToSet": {"role_ids": role_id}, "$set": {"updated_at": utcnow()}},
        )

    async def remove_role(self, role_id: ObjectId, *, session: Session | None = None) -> None:
        """Take a role away from everyone who has it."""
        await self._users.update_many(
            {"role_ids": role_id}, {"$pull": {"role_ids": role_id}}, session=session
        )

    async def count_active_with_role(
        self, role_id: ObjectId, *, excluding: ObjectId, session: Session | None = None
    ) -> int:
        return await self._users.count_documents(
            {"role_ids": role_id, "is_active": True, "_id": {"$ne": excluding}}, session=session
        )
