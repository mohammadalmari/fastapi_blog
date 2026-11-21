"""Unit tests for what the roles endpoints accept. No app, no database."""

import pytest
from pydantic import ValidationError

from app.rbac.permissions import Perm
from app.rbac.schemas import RoleCreate, RoleUpdate
from app.users.schemas import AssignRolesRequest


def test_a_role_name_is_normalized_and_permissions_are_tidied() -> None:
    role = RoleCreate.model_validate(
        {
            "name": " Author ",
            "permissions": ["posts:update:own", "posts:create", "posts:create"],
        }
    )

    assert role.name == "author"
    assert role.description == ""
    assert role.permissions == [Perm.POSTS_CREATE, Perm.POSTS_UPDATE_OWN]


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "a"},
        {"name": "x" * 41},
        {"name": "has space"},
        {"name": "né"},
        {"permissions": ["posts:fly"]},
        {"permissions": "posts:create"},
        {"permissions": ["posts:create"] * (len(Perm) + 1)},
        {"description": "x" * 201},
        # Not settable through the API: a client can't mint a system role,
        # nor create one that is already the default.
        {"is_system": True},
        {"is_default": True},
    ],
)
def test_invalid_roles_are_refused(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RoleCreate.model_validate({"name": "author", **fields})


@pytest.mark.parametrize(
    "fields",
    [{"name": "a"}, {"permissions": ["posts:fly"]}, {"is_system": True}, {"id": "x"}],
)
def test_invalid_role_changes_are_refused(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RoleUpdate.model_validate(fields)


def test_a_role_change_may_send_any_subset_of_fields() -> None:
    assert RoleUpdate.model_validate({}).model_dump(exclude_none=True) == {}
    assert RoleUpdate.model_validate({"is_default": True}).is_default is True


@pytest.mark.parametrize(
    "role_ids", ["reader", None, ["not-an-id"], ["5f0000000000000000000000"] * 51]
)
def test_invalid_role_assignments_are_refused(role_ids: object) -> None:
    with pytest.raises(ValidationError):
        AssignRolesRequest.model_validate({"role_ids": role_ids})
