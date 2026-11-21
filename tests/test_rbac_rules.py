"""Unit tests for the permission catalog and the ownership rule. No app, no database."""

import re
from datetime import UTC, datetime

import pytest
from bson import ObjectId

from app.core.errors import ForbiddenError
from app.rbac.permissions import DESCRIPTIONS, OPTIONAL_ROLES, READER_PERMISSIONS, Perm
from app.rbac.policy import Principal, authorize, ensure_can_grant
from app.users.models import User

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ME = ObjectId()
SOMEONE_ELSE = ObjectId()


def principal(*permissions: Perm) -> Principal:
    user = User(
        id=ME,
        email="jane@example.com",
        username="jane",
        password_hash="x",
        created_at=NOW,
        updated_at=NOW,
    )
    return Principal(user=user, roles=[], permissions=frozenset(permissions))


def test_every_permission_has_a_description() -> None:
    assert set(DESCRIPTIONS) == set(Perm)
    assert all(DESCRIPTIONS[perm].strip() for perm in Perm)


def test_permission_names_follow_one_shape() -> None:
    shape = re.compile(r"^[a-z_]+:[a-z_]+(:(own|any))?$")

    assert all(shape.fullmatch(perm) for perm in Perm)


def test_the_resources_are_the_ones_the_plan_names() -> None:
    assert {perm.resource for perm in Perm} == {"posts", "comments", "likes", "users", "roles"}


def test_the_built_in_role_definitions_only_use_catalog_permissions() -> None:
    assert set(READER_PERMISSIONS) <= set(Perm)
    for definition in OPTIONAL_ROLES.values():
        assert set(definition.permissions) <= set(Perm)


def test_has_checks_one_permission() -> None:
    reader = principal(Perm.COMMENTS_CREATE)

    assert reader.has(Perm.COMMENTS_CREATE)
    assert not reader.has(Perm.ROLES_CREATE)


@pytest.mark.parametrize(
    ("permissions", "owner", "allowed"),
    [
        ([Perm.POSTS_UPDATE_ANY], SOMEONE_ELSE, True),
        ([Perm.POSTS_UPDATE_ANY], ME, True),
        ([Perm.POSTS_UPDATE_OWN], ME, True),
        ([Perm.POSTS_UPDATE_OWN], SOMEONE_ELSE, False),
        ([], ME, False),
        # Holding the permission for another action doesn't help.
        ([Perm.POSTS_DELETE_ANY, Perm.POSTS_DELETE_OWN], ME, False),
    ],
)
def test_authorize_applies_own_and_any(
    permissions: list[Perm], owner: ObjectId, allowed: bool
) -> None:
    who = principal(*permissions)

    def check() -> None:
        authorize(who, own=Perm.POSTS_UPDATE_OWN, any_=Perm.POSTS_UPDATE_ANY, owner_id=owner)

    if allowed:
        check()
    else:
        with pytest.raises(ForbiddenError):
            check()


@pytest.mark.parametrize(
    ("permissions", "owner", "allowed"),
    [
        ([Perm.POSTS_READ_DRAFTS_ANY], SOMEONE_ELSE, True),
        ([], SOMEONE_ELSE, False),
        ([], ME, False),
    ],
)
def test_can_works_for_a_permission_without_an_own_form(
    permissions: list[Perm], owner: ObjectId, allowed: bool
) -> None:
    """`can` answers yes or no, for code that filters a list instead of refusing."""
    who = principal(*permissions)

    assert who.can(any_=Perm.POSTS_READ_DRAFTS_ANY, owner_id=owner) is allowed


def test_granting_is_limited_to_what_the_granter_holds() -> None:
    editor = principal(Perm.ROLES_UPDATE, Perm.POSTS_CREATE)

    ensure_can_grant(editor, [Perm.POSTS_CREATE])
    ensure_can_grant(editor, [])
    with pytest.raises(ForbiddenError) as refused:
        ensure_can_grant(editor, [Perm.POSTS_CREATE, Perm.ROLES_DELETE, Perm.LIKES_CREATE])

    # It names what is missing, so the caller knows what to ask for.
    assert "likes:create" in refused.value.detail
    assert "roles:delete" in refused.value.detail
    assert "posts:create" not in refused.value.detail
