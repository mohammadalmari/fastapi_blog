"""People who write, and posts that already exist, for the posts tests."""

from collections.abc import Awaitable, Sequence
from typing import Protocol

import pytest

from app.core.database import Database
from app.posts.models import Post
from app.posts.repository import PostRepository
from app.posts.rules import make_excerpt
from app.rbac.permissions import OPTIONAL_ROLES
from app.users.models import User
from tests.conftest import HeadersFor, MakeRole, MakeUser


class MakePost(Protocol):
    def __call__(
        self,
        author: User,
        title: str = "A post",
        *,
        content: str = "Some **content**.",
        tags: Sequence[str] = (),
        published: bool = False,
    ) -> Awaitable[Post]: ...


@pytest.fixture
async def author_role(make_role: MakeRole) -> None:
    """May write, edit, publish and delete their own posts, as the seeded author role may."""
    await make_role("author", *OPTIONAL_ROLES["author"].permissions)


@pytest.fixture
async def author(author_role: None, make_user: MakeUser) -> User:
    return await make_user("ali", "author")


@pytest.fixture
async def other_author(author_role: None, make_user: MakeUser) -> User:
    return await make_user("omar", "author")


@pytest.fixture
async def editor(make_role: MakeRole, make_user: MakeUser) -> User:
    """May edit, publish and delete anyone's posts and read drafts; may not write new ones."""
    await make_role("editor", *OPTIONAL_ROLES["editor"].permissions)
    return await make_user("eve", "editor")


@pytest.fixture
def author_headers(author: User, headers_for: HeadersFor) -> dict[str, str]:
    return headers_for(author)


@pytest.fixture
def make_post(db: Database) -> MakePost:
    """``await make_post(author, "Title", published=True)`` stores a post directly."""

    async def make(
        author: User,
        title: str = "A post",
        *,
        content: str = "Some **content**.",
        tags: Sequence[str] = (),
        published: bool = False,
    ) -> Post:
        repository = PostRepository(db)
        post = await repository.create(
            author_id=author.id,
            title=title,
            content=content,
            excerpt=make_excerpt(content),
            tags=list(tags),
        )
        if published:
            published_post = await repository.set_published(post.id, published=True)
            assert published_post is not None
            post = published_post
        return post

    return make
