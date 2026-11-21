from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel

from app.core.types import PyObjectId
from app.users.models import User


class Comment(BaseModel):
    """
    A comment as stored. Every stored comment is read through this model, so
    a field added later needs a default (or a backfill of existing documents).
    """

    id: PyObjectId
    post_id: PyObjectId
    author_id: PyObjectId
    # None for a comment on the post itself; otherwise the comment it replies
    # to, which is always one with no parent of its own: threads are one
    # level deep.
    parent_id: PyObjectId | None = None
    content: str  # as written; emptied when the comment is deleted
    # A deleted comment stays as a row so that its replies still have
    # something to hang from. What it said is gone.
    is_deleted: bool = False
    reply_count: int = 0  # live replies only
    like_count: int = 0
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CommentView:
    """A comment together with its author, which is how the API shows one."""

    comment: Comment
    author: User | None
    # Whether the person asking has liked it. False for a visitor.
    liked_by_me: bool = False
