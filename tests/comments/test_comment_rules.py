"""Unit tests for what the comments endpoints accept, and the own-only check. No app."""

from datetime import UTC, datetime

import pytest
from bson import ObjectId
from pydantic import ValidationError

from app.comments.schemas import CommentCreate, CommentUpdate
from app.core.errors import ForbiddenError
from app.rbac.permissions import Perm
from app.rbac.policy import Principal, authorize
from app.users.models import User

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ME = ObjectId()


def test_a_comment_is_trimmed_and_otherwise_kept_as_written() -> None:
    comment = CommentCreate.model_validate({"content": "  First line.\n\nSecond *line*.  "})

    assert comment.content == "First line.\n\nSecond *line*."


@pytest.mark.parametrize(
    "fields",
    [
        {"content": ""},
        {"content": "   \n  "},
        {"content": "x" * 5_001},
        {"content": None},
        {},
        # Not the client's to set.
        {"content": "ok", "author_id": "5f0000000000000000000000"},
        {"content": "ok", "post_id": "5f0000000000000000000000"},
        {"content": "ok", "parent_id": "5f0000000000000000000000"},
        {"content": "ok", "is_deleted": False},
        {"content": "ok", "like_count": 99},
    ],
)
def test_invalid_comments_are_refused(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CommentCreate.model_validate(fields)
    with pytest.raises(ValidationError):
        CommentUpdate.model_validate(fields)


@pytest.mark.parametrize(
    "content",
    [
        "\u200b",  # a zero-width space: one character, nothing to see
        "\u200b\u200c\u2060",
        "\u0301",  # an accent with no letter under it
        "a\u0000b",  # NUL
        "a\u001bb",  # ESC, as terminal escape sequences start
        "\u202eevil",  # the right-to-left override, which reverses what follows
        "a\u2028b",  # a line separator that isn't a line break
    ],
)
def test_comments_with_hidden_or_no_visible_text_are_refused(content: str) -> None:
    with pytest.raises(ValidationError):
        CommentCreate.model_validate({"content": content})


@pytest.mark.parametrize(
    "content",
    [
        "Two\nlines",
        "Windows\r\nlines",
        "A\ttab",
        "1",
        "!",
        "مرحبا",
        "\u0915\u093f",  # a Hindi letter with its vowel sign
        "\U0001f44b",  # an emoji
        "\U0001f468\u200d\U0001f469\u200d\U0001f467",  # a family: emoji held by joiners
    ],
)
def test_comments_people_actually_write_are_accepted(content: str) -> None:
    assert CommentCreate.model_validate({"content": content}).content == content


def test_a_comment_of_the_longest_allowed_length_is_accepted() -> None:
    assert len(CommentCreate.model_validate({"content": "x" * 5_000}).content) == 5_000


@pytest.mark.parametrize(
    ("permissions", "owner", "allowed"),
    [
        ([Perm.COMMENTS_UPDATE_OWN], ME, True),
        ([Perm.COMMENTS_UPDATE_OWN], ObjectId(), False),
        ([], ME, False),
        # No permission lets you edit someone else's words, not even the ones
        # that let you delete them.
        ([Perm.COMMENTS_DELETE_ANY, Perm.COMMENTS_DELETE_OWN], ObjectId(), False),
    ],
)
def test_an_action_with_no_any_form_is_for_the_owner_only(
    permissions: list[Perm], owner: ObjectId, allowed: bool
) -> None:
    user = User(
        id=ME,
        email="jane@example.com",
        username="jane",
        password_hash="x",
        created_at=NOW,
        updated_at=NOW,
    )
    who = Principal(user=user, roles=[], permissions=frozenset(permissions))

    assert who.can(own=Perm.COMMENTS_UPDATE_OWN, owner_id=owner) is allowed
    if not allowed:
        with pytest.raises(ForbiddenError):
            authorize(who, own=Perm.COMMENTS_UPDATE_OWN, owner_id=owner)


def test_a_permission_check_with_no_permission_is_a_mistake_not_a_refusal() -> None:
    """
    Asking "may they?" without naming a permission can only be a forgotten
    argument. A quiet "no" would turn it into a route nobody can use.
    """
    user = User(
        id=ME,
        email="jane@example.com",
        username="jane",
        password_hash="x",
        created_at=NOW,
        updated_at=NOW,
    )
    who = Principal(user=user, roles=[], permissions=frozenset(Perm))

    with pytest.raises(TypeError):
        who.can(owner_id=ME)
    with pytest.raises(TypeError):
        authorize(who, owner_id=ME)
