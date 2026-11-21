from dataclasses import dataclass
from datetime import datetime
from typing import Literal, NamedTuple

from pydantic import BaseModel, Field

from app.core.types import PyObjectId
from app.users.models import User

type PostStatus = Literal["draft", "published"]


class Post(BaseModel):
    """
    A post as stored. Every stored post is read through this model, so a
    field added later needs a default (or a backfill of existing documents).
    """

    id: PyObjectId
    author_id: PyObjectId
    title: str
    # Fixed when the post is created, so links to it keep working when the
    # title changes.
    slug: str
    content: str  # Markdown, exactly as written; clients render it
    excerpt: str
    tags: list[str] = Field(default_factory=list)
    status: PostStatus = "draft"
    # Set the first time the post is published and kept after that, so a post
    # that is unpublished and published again doesn't jump to the top.
    published_at: datetime | None = None
    like_count: int = 0
    comment_count: int = 0
    created_at: datetime
    # When the post's text or tags last changed. Publishing doesn't move it.
    updated_at: datetime


@dataclass(frozen=True)
class PostView:
    """A post together with its author, which is how the API shows one."""

    post: Post
    # None when the account has been removed; the post stays.
    author: User | None
    # Whether the person asking has liked it. False for a visitor.
    liked_by_me: bool = False


class TagCount(NamedTuple):
    name: str
    count: int
