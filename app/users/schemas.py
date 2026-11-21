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
    validate_email,
)

from app.core.types import PyObjectId


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


Email = Annotated[EmailStr, AfterValidator(str.lower)]

# Used in public URLs, hence the narrow alphabet. Normalized first, so "Jane"
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


class AuthorSummary(BaseModel):
    """A user as shown beside something they wrote: who, and nothing private."""

    id: PyObjectId
    username: str
    display_name: str | None


class RoleSummary(BaseModel):
    id: PyObjectId
    name: str


class UserAccount(BaseModel):
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


class UserAdmin(BaseModel):
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
