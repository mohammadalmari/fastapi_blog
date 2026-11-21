"""What the roles and permissions endpoints accept and return."""

from datetime import datetime
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
)

from app.core.types import PyObjectId
from app.rbac.permissions import Perm


def _normalize(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


def _unique_sorted(permissions: list[Perm]) -> list[Perm]:
    return sorted(set(permissions))


RoleName = Annotated[
    str,
    BeforeValidator(_normalize),
    StringConstraints(pattern=r"^[a-z0-9_]{2,40}$"),
    Field(description="2 to 40 letters, digits or underscores. Stored in lowercase."),
]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
# `list[Perm]` is what limits a role to permissions that exist: anything not
# in the catalog fails validation with a 422.
Permissions = Annotated[list[Perm], Field(max_length=len(Perm)), AfterValidator(_unique_sorted)]


class RoleCreate(BaseModel):
    # Unknown fields are refused rather than ignored, so that sending
    # `is_system` gets a clear error instead of silently doing nothing.
    model_config = ConfigDict(extra="forbid")

    name: RoleName
    description: Description = ""
    permissions: Permissions = Field(default_factory=list)


class RoleUpdate(BaseModel):
    """Every field is optional: only the ones sent are changed. Null counts as not sent."""

    model_config = ConfigDict(extra="forbid")

    name: RoleName | None = None
    description: Description | None = None
    permissions: Permissions | None = None
    is_default: bool | None = None


class RolePublic(BaseModel):
    id: PyObjectId
    name: str
    description: str
    permissions: list[Perm]
    is_system: bool
    is_default: bool
    created_at: datetime
    updated_at: datetime


class PermissionPublic(BaseModel):
    id: Perm
    resource: str = Field(examples=["posts"])
    description: str
