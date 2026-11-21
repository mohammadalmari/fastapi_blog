"""What the posts endpoints accept and return."""

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

from app.core.pagination import PageParams
from app.core.text import has_hidden_characters
from app.core.types import PyObjectId
from app.posts.models import PostStatus
from app.posts.rules import TAG_MAX_LENGTH, normalize_tag, tag_problem
from app.users.schemas import AuthorSummary

TITLE_MAX_LENGTH = 200
CONTENT_MAX_LENGTH = 100_000
EXCERPT_MAX_LENGTH = 300
MAX_TAGS = 10
USERNAME_MAX_LENGTH = 30

_UNTRUSTED = "Written by a user: escape it when displaying it."


def _no_hidden_characters(text: str) -> str:
    if has_hidden_characters(text):
        raise ValueError("Control and invisible formatting characters are not allowed")
    return text


def _normalized_tag(tag: object) -> object:
    return normalize_tag(tag) if isinstance(tag, str) else tag


def _valid_tag(tag: str) -> str:
    problem = tag_problem(tag)
    if problem:
        raise ValueError(problem)
    return tag


def _unique(tags: list[str]) -> list[str]:
    return list(dict.fromkeys(tags))  # no duplicates, order kept


def _blank_is_absent(value: object) -> object:
    """An HTML form sends an empty box as ``?tag=``; that means "no filter"."""
    return None if isinstance(value, str) and not value.strip() else value


Title = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=TITLE_MAX_LENGTH),
    AfterValidator(_no_hidden_characters),
]
Content = Annotated[str, StringConstraints(min_length=1, max_length=CONTENT_MAX_LENGTH)]
Excerpt = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=EXCERPT_MAX_LENGTH),
    AfterValidator(_no_hidden_characters),
]
# Normalized first (lowercase, spaces to dashes), then checked.
Tag = Annotated[
    str,
    BeforeValidator(_normalized_tag),
    AfterValidator(_valid_tag),
    Field(
        min_length=1,
        max_length=TAG_MAX_LENGTH,
        description="Letters and digits of any script, and + # . - _ . Stored in lowercase.",
        examples=["python", "c++", "node.js"],
    ),
]
Tags = Annotated[list[Tag], Field(max_length=MAX_TAGS), AfterValidator(_unique)]


class PostCreate(BaseModel):
    # Unknown fields are refused, not ignored: the author, the slug, the
    # status and the counters are the server's to decide, and a client that
    # sends one should be told so.
    model_config = ConfigDict(extra="forbid")

    title: Title
    content: Content = Field(description="Markdown. Stored as written.")
    excerpt: Excerpt | None = Field(
        default=None,
        description="A short summary. Generated from the content when left out or empty.",
    )
    tags: Tags = Field(default_factory=list)


class PostUpdate(BaseModel):
    """Every field is optional: only the ones sent are changed. Null counts as not sent."""

    model_config = ConfigDict(extra="forbid")

    title: Title | None = None
    content: Content | None = None
    excerpt: Excerpt | None = Field(
        default=None, description="Send an empty string to go back to a generated excerpt."
    )
    tags: Tags | None = Field(default=None, description="The complete new list; [] clears it.")


class FeedParams(PageParams):
    """The feed's query parameters: paging, plus two optional filters."""

    # The length limit is on the string, inside the "or None": written on the
    # whole field it would be applied to None as well, and fail.
    tag: Annotated[
        Annotated[str, StringConstraints(max_length=TAG_MAX_LENGTH)] | None,
        BeforeValidator(_blank_is_absent),
        BeforeValidator(_normalized_tag),  # the last one listed runs first
        Field(description="Only posts with this tag."),
    ] = None
    author: Annotated[
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=USERNAME_MAX_LENGTH)]
        | None,
        BeforeValidator(_blank_is_absent),
        Field(description="Only posts by this username."),
    ] = None


class MyPostsParams(PageParams):
    status: PostStatus | None = Field(default=None, description="Only drafts, or only published.")


class PostSummary(BaseModel):
    """A post as it appears in a list: everything but the content."""

    id: PyObjectId
    slug: str
    title: str = Field(description=_UNTRUSTED)
    excerpt: str = Field(description=f"Plain text, but not cleaned of HTML. {_UNTRUSTED}")
    tags: list[str]
    status: PostStatus
    published_at: datetime | None = Field(
        description="When it was first published. The feed is ordered by this."
    )
    created_at: datetime
    updated_at: datetime = Field(description="When its text or tags last changed.")
    like_count: int
    comment_count: int
    author: AuthorSummary | None = Field(description="Null if the account no longer exists.")


class PostPublic(PostSummary):
    content: str = Field(description="Markdown, stored as written. Render it safely.")


class TagCount(BaseModel):
    name: str
    count: int = Field(description="How many published posts carry the tag.")
