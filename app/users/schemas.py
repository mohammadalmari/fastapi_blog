"""What the users endpoints accept and return."""

import unicodedata
from datetime import datetime
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    computed_field,
    validate_email,
)

from app.core.pagination import PageParams
from app.core.text import LINE_BREAKS_AND_TABS, has_hidden_characters
from app.core.types import PyObjectId

DISPLAY_NAME_MAX_LENGTH = 50
BIO_MAX_LENGTH = 500

# Where avatars are served from (see the media router in users.router). Not
# under the API prefix: these are files, and their address must not change
# with the API's version.
AVATAR_URL_PREFIX = "/media/avatars"

_UNTRUSTED = "Written by a user: escape it when displaying it."


def normalize_email(email: str) -> str:
    """
    The one stored spelling of an email: validated, Unicode-normalized, lowercase.

    ``validate_email`` is what ``EmailStr`` uses, so sign-up and login agree.
    Raises ``ValueError`` if it isn't an email.
    """
    return validate_email(email)[1].lower()


def _normalize_username(value: object) -> object:
    if not isinstance(value, str):
        return value
    return unicodedata.normalize("NFC", value.strip()).lower()


def normalize_login_identifier(identifier: str) -> tuple[str, str]:
    """
    What someone typed in the login form, as ``(field, value)`` to look up.

    It is an email if it has an "@" (usernames can't), and gets the same
    normalization the value got at sign-up, or the two would never match.
    """
    if "@" in identifier:
        try:
            return "email", normalize_email(identifier)
        except ValueError:
            # Not a valid email, so no account has it; the lookup just misses.
            return "email", identifier.strip().lower()
    return "username", str(_normalize_username(identifier))


def _no_hidden_characters(text: str) -> str:
    if has_hidden_characters(text):
        raise ValueError("Control and invisible formatting characters are not allowed")
    return text


def _no_hidden_characters_but_line_breaks(text: str) -> str:
    if has_hidden_characters(text, allowed=LINE_BREAKS_AND_TABS):
        raise ValueError("Control and invisible formatting characters are not allowed")
    return text


Email = Annotated[EmailStr, AfterValidator(str.lower)]

# Used in public URLs, hence the narrow alphabet. At least three characters,
# which also keeps a username from ever being "me": /users/me is the caller's
# own account, and a user by that name would have no profile page. Normalized first, so "Jane"
# is accepted and stored as "jane"; the pattern then checks the result.
Username = Annotated[
    str,
    BeforeValidator(_normalize_username),
    StringConstraints(pattern=r"^[a-z0-9_]{3,30}$"),
    Field(
        description="3 to 30 letters, digits or underscores. Stored in lowercase.",
        examples=["jane_doe"],
    ),
]


# An empty string is accepted for both: it means "clear this".
DisplayName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=DISPLAY_NAME_MAX_LENGTH),
    AfterValidator(_no_hidden_characters),
]
Bio = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=BIO_MAX_LENGTH),
    AfterValidator(_no_hidden_characters_but_line_breaks),
]


class _WithAvatar(BaseModel):
    """
    Turns the stored `avatar_key` into the address the picture is served at.

    The key is read from the internal user and left out of the response; the
    address is worked out from it.
    """

    avatar_key: str | None = Field(default=None, exclude=True)

    @computed_field(description="Where the avatar is served, or null if there is none.")
    @property
    def avatar_url(self) -> str | None:
        return f"{AVATAR_URL_PREFIX}/{self.avatar_key}" if self.avatar_key else None


class AuthorSummary(_WithAvatar):
    """A user as shown beside something they wrote: who, and nothing private."""

    id: PyObjectId
    username: str
    display_name: str | None


class UserPublic(_WithAvatar):
    """A public profile. No email: usernames are public, emails are not."""

    id: PyObjectId
    username: str
    display_name: str | None = Field(description=_UNTRUSTED)
    bio: str | None = Field(description=_UNTRUSTED)
    created_at: datetime


class ProfileUpdate(BaseModel):
    """Every field is optional: only the ones sent are changed. Null counts as not sent."""

    # Unknown fields are refused. The email, the roles, the password and
    # whether the account is active are not changed here, and a client that
    # sends one should be told so rather than believe it worked.
    model_config = ConfigDict(extra="forbid")

    username: Username | None = None
    display_name: DisplayName | None = Field(
        default=None, description="Send an empty string to clear it."
    )
    bio: Bio | None = Field(default=None, description="Send an empty string to clear it.")


class StatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_active: bool = Field(description="False deactivates the account; true brings it back.")


class UserListParams(PageParams):
    is_active: bool | None = Field(default=None, description="Only active, or only deactivated.")


class RoleSummary(BaseModel):
    id: PyObjectId
    name: str


class UserAccount(_WithAvatar):
    """A user's own account. Notably without ``password_hash``."""

    id: PyObjectId
    email: str
    username: str
    display_name: str | None
    bio: str | None
    is_active: bool
    created_at: datetime


class UserMe(UserAccount):
    """The account, plus what it is allowed to do."""

    roles: list[RoleSummary]
    # What the roles add up to, so a client can decide which buttons to show.
    # The server still checks every action itself.
    permissions: list[str]


class UserAdmin(_WithAvatar):
    """A user as an administrator sees them."""

    id: PyObjectId
    email: str
    username: str
    display_name: str | None
    is_active: bool
    created_at: datetime
    roles: list[RoleSummary]


class AssignRolesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_ids: list[PyObjectId] = Field(max_length=50, description="The user's complete new set.")
