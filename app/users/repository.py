"""Every MongoDB query on the ``users`` collection."""

from collections.abc import Iterable, Sequence
from typing import Literal

from bson import ObjectId
from pymongo import DESCENDING, IndexModel, ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.database import Database, Document, Session, register_indexes, utcnow
from app.core.pagination import CursorPosition, cursor_filter, cursor_sort, split_page
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
        # The administrators' list, newest account first, with and without
        # the "active only" filter. Each ends with the sort field and `_id`
        # (see core.pagination).
        IndexModel([("created_at", DESCENDING), ("_id", DESCENDING)], name="users_newest"),
        IndexModel(
            [("is_active", DESCENDING), ("created_at", DESCENDING), ("_id", DESCENDING)],
            name="users_by_status",
        ),
    ],
)


class DuplicateUserError(Exception):
    """The email or username is already taken; ``field`` says which."""

    def __init__(self, field: Literal["email", "username"]) -> None:
        super().__init__(f"duplicate {field}")
        self.field: Literal["email", "username"] = field


def users_query(is_active: bool | None) -> Document:
    """The administrators' list's filter. A function so its test can explain it."""
    return {} if is_active is None else {"is_active": is_active}


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

    async def get_many(self, user_ids: Iterable[ObjectId]) -> dict[ObjectId, User]:
        """The users with these ids, by id. Ids that match nobody are simply absent."""
        cursor = self._users.find({"_id": {"$in": list(user_ids)}})
        return {doc["_id"]: _to_user(doc) async for doc in cursor}

    async def get_by_email(self, email: str) -> User | None:
        doc = await self._users.find_one({"email": email})
        return _to_user(doc) if doc else None

    async def get_by_username(self, username: str) -> User | None:
        """
        For public lookups. Unlike ``get_by_login`` this never matches an
        email: usernames are public and emails aren't, and a public route that
        accepted either would reveal which emails have accounts.
        """
        field, value = normalize_login_identifier(username)
        # A username can't contain "@", so searching the username field for
        # an email could never match anyway. Refusing it outright states the
        # rule instead of relying on that.
        if field != "username":
            return None
        doc = await self._users.find_one({"username": value})
        return _to_user(doc) if doc else None

    async def get_by_login(self, identifier: str) -> User | None:
        """Find a user by what they typed in the login form: email or username."""
        field, value = normalize_login_identifier(identifier)
        doc = await self._users.find_one({field: value})
        return _to_user(doc) if doc else None

    async def replace_password_hash(
        self, user_id: ObjectId, *, expected: str, password_hash: str
    ) -> bool:
        """
        Store a new hash, if the stored one is still ``expected``. False if not.

        Whoever writes a new hash has just checked a password against the old
        one. Between that check and this write someone else may have changed
        the password; then the check was made against a password that no
        longer counts, and nothing must be written.
        """
        result = await self._users.update_one(
            {"_id": user_id, "password_hash": expected},
            {"$set": {"password_hash": password_hash, "updated_at": utcnow()}},
        )
        return result.modified_count == 1

    async def update_profile(self, user_id: ObjectId, changes: Document) -> User | None:
        """Change profile fields. None if the user is gone."""
        try:
            doc = await self._users.find_one_and_update(
                {"_id": user_id},
                {"$set": {**changes, "updated_at": utcnow()}},
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError as error:
            # Only the username can clash here: the email isn't editable.
            raise DuplicateUserError("username") from error
        return _to_user(doc) if doc else None

    async def set_avatar(self, user_id: ObjectId, avatar_key: str | None) -> User | None:
        """
        Point the user at a new avatar, or at none. Returns the user as they
        were *before*, so the caller knows which file is no longer needed.

        Reading the old key and writing the new one is a single operation:
        of two uploads at the same moment, each is handed a different old
        key, so every replaced file is removed once and none is forgotten.
        """
        doc = await self._users.find_one_and_update(
            {"_id": user_id},
            {"$set": {"avatar_key": avatar_key, "updated_at": utcnow()}},
            return_document=ReturnDocument.BEFORE,
        )
        return _to_user(doc) if doc else None

    async def set_active(self, user_id: ObjectId, active: bool, *, session: Session) -> User | None:
        doc = await self._users.find_one_and_update(
            {"_id": user_id},
            {"$set": {"is_active": active, "updated_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        return _to_user(doc) if doc else None

    async def list_page(
        self, *, is_active: bool | None, after: CursorPosition | None, limit: int
    ) -> tuple[list[User], str | None]:
        """Accounts, newest first, and the cursor for the next page."""
        query = users_query(is_active)
        if after is not None:
            query |= cursor_filter("created_at", after, descending=True)
        docs = await (
            self._users.find(query)
            .sort(cursor_sort("created_at", descending=True))
            .limit(limit + 1)
            .to_list()
        )
        docs, next_cursor = split_page(docs, limit, "created_at")
        return [_to_user(doc) for doc in docs], next_cursor

    async def set_roles(
        self, user_id: ObjectId, role_ids: Sequence[ObjectId], *, session: Session | None = None
    ) -> User | None:
        doc = await self._users.find_one_and_update(
            {"_id": user_id},
            {"$set": {"role_ids": list(role_ids), "updated_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
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
