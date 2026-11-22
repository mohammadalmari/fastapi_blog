"""Profiles, the administrators' list of accounts, and avatars."""

from app.core.errors import ConflictError, FieldError, NotFoundError, PayloadTooLargeError
from app.core.pagination import CursorPosition
from app.core.settings.security import AVATAR_MAX_BYTES
from app.core.storage import Storage
from app.users.avatars import InvalidImageError, make_avatar_async, new_avatar_key
from app.users.models import User
from app.users.repository import DuplicateUserError, UserRepository
from app.users.schemas import ProfileUpdate

_NOT_FOUND = "User not found"
_AVATAR_FOLDER = "avatars"


def _storage_key(avatar_key: str) -> str:
    return f"{_AVATAR_FOLDER}/{avatar_key}"


class UserService:
    def __init__(self, users: UserRepository, storage: Storage) -> None:
        self._users = users
        self._storage = storage

    # --- profiles -----------------------------------------------------------

    async def get_public(self, username: str) -> User:
        """
        The account behind a public profile page.

        A deactivated account has no profile: to a visitor it is "not found",
        exactly like a name nobody has. What its owner wrote stays where it
        is, under their name.
        """
        user = await self._users.get_by_username(username)
        if user is None or not user.is_active:
            raise NotFoundError(_NOT_FOUND)
        return user

    async def update_profile(self, user: User, data: ProfileUpdate) -> User:
        """Change the fields sent. An empty display name or bio clears it."""
        sent = data.model_dump(exclude_none=True)
        for field in ("display_name", "bio"):
            if sent.get(field) == "":
                sent[field] = None
        # Only what differs is written, so that a request which changes
        # nothing doesn't move the "last changed" date.
        changes = {field: value for field, value in sent.items() if value != getattr(user, field)}
        if not changes:
            return user
        try:
            updated = await self._users.update_profile(user.id, changes)
        except DuplicateUserError as error:
            # The unique index decides, not a lookup beforehand: of two people
            # asking for the same name at once, one gets it.
            raise ConflictError("Username already taken") from error
        if updated is None:
            raise NotFoundError(_NOT_FOUND)
        return updated

    async def list_accounts(
        self, *, is_active: bool | None, after: CursorPosition | None, limit: int
    ) -> tuple[list[User], str | None]:
        return await self._users.list_page(is_active=is_active, after=after, limit=limit)

    # --- avatars ------------------------------------------------------------

    async def set_avatar(self, user: User, upload: bytes) -> User:
        """Make the upload the user's avatar. Returns the user with the new one."""
        if len(upload) > AVATAR_MAX_BYTES:
            megabytes = AVATAR_MAX_BYTES // (1024 * 1024)
            raise PayloadTooLargeError(f"The picture must be at most {megabytes} MB")
        try:
            picture = await make_avatar_async(upload)
        except InvalidImageError as error:
            raise FieldError("file", [str(error)]) from error

        avatar_key = new_avatar_key()
        # The file first, then the pointer to it, then the old file away. At
        # no moment does the account point at a file that isn't there. If a
        # step fails, the worst left behind is a file nothing points at.
        await self._storage.save(_storage_key(avatar_key), picture)
        before = await self._users.set_avatar(user.id, avatar_key)
        if before is None:
            await self._storage.delete(_storage_key(avatar_key))
            raise NotFoundError(_NOT_FOUND)
        if before.avatar_key:
            await self._storage.delete(_storage_key(before.avatar_key))
        return before.model_copy(update={"avatar_key": avatar_key})

    async def remove_avatar(self, user: User) -> None:
        """Go back to having no avatar. Fine to call when there is none."""
        before = await self._users.set_avatar(user.id, None)
        if before is not None and before.avatar_key:
            await self._storage.delete(_storage_key(before.avatar_key))

    async def read_avatar(self, avatar_key: str) -> bytes:
        picture = await self._storage.read(_storage_key(avatar_key))
        if picture is None:
            raise NotFoundError("Avatar not found")
        return picture
