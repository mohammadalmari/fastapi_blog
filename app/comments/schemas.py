"""What the comments endpoints accept and return."""

from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from app.core.text import LINE_BREAKS_AND_TABS, has_hidden_characters, has_visible_characters
from app.core.types import PyObjectId
from app.users.schemas import AuthorSummary

CONTENT_MAX_LENGTH = 5_000


def _readable(text: str) -> str:
    if has_hidden_characters(text, allowed=LINE_BREAKS_AND_TABS):
        raise ValueError("Control and invisible formatting characters are not allowed")
    if not has_visible_characters(text):
        # A zero-width space is one character and shows as none.
        raise ValueError("A comment needs something to read")
    return text


Content = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=CONTENT_MAX_LENGTH),
    AfterValidator(_readable),
]


class CommentCreate(BaseModel):
    # Unknown fields are refused: the author, the post and the parent come
    # from who is asking and from the URL, never from the body.
    model_config = ConfigDict(extra="forbid")

    content: Content = Field(description="Stored as written. May span several lines.")


class CommentUpdate(CommentCreate):
    """The same single field today; its own name so the two can grow apart."""


class CommentPublic(BaseModel):
    id: PyObjectId
    post_id: PyObjectId
    parent_id: PyObjectId | None = Field(description="Null for a comment on the post itself.")
    content: str | None = Field(
        description="Null once deleted. Written by a user: escape it when displaying it."
    )
    author: AuthorSummary | None = Field(
        description="Null once deleted, or if the account no longer exists."
    )
    is_deleted: bool = Field(
        description="True for a placeholder: a deleted comment kept because it has replies."
    )
    reply_count: int
    like_count: int
    created_at: datetime
    updated_at: datetime = Field(description="Later than `created_at` if it was edited.")
