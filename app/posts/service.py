"""Writing, publishing and reading posts, and who may do which."""

from collections.abc import Awaitable, Collection, Sequence
from typing import Protocol

from bson import ObjectId

from app.core.database import Database, Session, in_transaction
from app.core.errors import ConflictError, NotFoundError
from app.core.pagination import CursorPosition
from app.core.ttl import Remembered
from app.posts.models import Post, PostStatus, PostView, TagCount
from app.posts.policy import may_see
from app.posts.repository import PostRepository, SlugUnavailableError
from app.posts.rules import make_excerpt
from app.posts.schemas import PostCreate, PostUpdate
from app.rbac.permissions import Perm
from app.rbac.policy import Principal, authorize
from app.users.repository import UserRepository

_NOT_FOUND = "Post not found"


class PostCleanup(Protocol):
    """Something to run when a post is deleted, inside the same transaction."""

    def __call__(self, post_id: ObjectId, *, session: Session) -> Awaitable[None]: ...


class LikedLookup(Protocol):
    """Which of these posts a user has liked."""

    def __call__(
        self, user_id: ObjectId, ids: Collection[ObjectId]
    ) -> Awaitable[set[ObjectId]]: ...


class PostService:
    def __init__(
        self,
        db: Database,
        posts: PostRepository,
        users: UserRepository,
        on_delete: Sequence[PostCleanup] = (),
        liked: LikedLookup | None = None,
        tag_cache: Remembered[list[TagCount]] | None = None,
    ) -> None:
        self._db = db
        self._posts = posts
        self._users = users
        # What hangs from a post (its comments, its likes) is removed
        # with it. The features that own those things register how; this
        # service knows only that there may be something to clean up, and so
        # depends on none of them.
        self._on_delete = on_delete
        # Likes are another feature's too. It registers how to ask "which of
        # these has this user liked?"; with nothing registered, none are.
        self._liked = liked
        # Counting tags reads every published post. The list is the same for
        # everyone and changes slowly, so it is remembered for a short while.
        # The service is built for each request, so what is remembered lives
        # outside it and is handed in.
        self._tag_cache = tag_cache

    # --- reading ------------------------------------------------------------

    async def get_by_slug(self, principal: Principal | None, slug: str) -> PostView:
        post = await self._posts.get_by_slug(slug)
        if post is None or not may_see(principal, post):
            raise NotFoundError(_NOT_FOUND)
        return await self._view(principal, post)

    async def feed(
        self,
        principal: Principal | None,
        *,
        tag: str | None,
        author: str | None,
        after: CursorPosition | None,
        limit: int,
    ) -> tuple[list[PostView], str | None]:
        """Published posts, newest first, optionally of one tag or one author."""
        author_id = None
        if author is not None:
            user = await self._users.get_by_username(author)
            if user is None:
                return [], None  # nobody by that name has posts
            author_id = user.id
        posts, next_cursor = await self._posts.list_published(
            tag=tag, author_id=author_id, after=after, limit=limit
        )
        return await self._views(principal, posts), next_cursor

    async def mine(
        self,
        principal: Principal,
        *,
        status: PostStatus | None,
        after: CursorPosition | None,
        limit: int,
    ) -> tuple[list[PostView], str | None]:
        """The caller's own posts, drafts included."""
        posts, next_cursor = await self._posts.list_by_author(
            principal.user.id, status=status, after=after, limit=limit
        )
        liked = await self._liked_among(principal, posts)
        return [PostView(post, principal.user, post.id in liked) for post in posts], next_cursor

    async def tags(self) -> list[TagCount]:
        """The most used tags. May be a little behind: see ``tags_cache_seconds``."""
        if self._tag_cache is None:
            return await self._posts.tag_counts()
        return await self._tag_cache.get(self._posts.tag_counts)

    # --- writing ------------------------------------------------------------

    async def create(self, principal: Principal, data: PostCreate) -> PostView:
        """A new post is always a draft, and always the caller's."""
        try:
            post = await self._posts.create(
                author_id=principal.user.id,
                title=data.title,
                content=data.content,
                excerpt=data.excerpt or make_excerpt(data.content),
                tags=data.tags,
            )
        except SlugUnavailableError as error:
            raise ConflictError("Couldn't find a free address for this title; try again") from error
        return PostView(post, principal.user)

    async def update(self, principal: Principal, post_id: ObjectId, data: PostUpdate) -> PostView:
        post = await self._load(principal, post_id)
        authorize(
            principal,
            any_=Perm.POSTS_UPDATE_ANY,
            own=Perm.POSTS_UPDATE_OWN,
            owner_id=post.author_id,
        )
        if post.status == "published":
            # Changing what is public is publishing. Without this, a writer
            # whose posts someone else approves could get a harmless post
            # published and then replace its text.
            authorize(
                principal,
                any_=Perm.POSTS_PUBLISH_ANY,
                own=Perm.POSTS_PUBLISH_OWN,
                owner_id=post.author_id,
            )

        sent = data.model_dump(exclude_none=True)
        content = sent.get("content", post.content)
        # No flag records whether an excerpt was generated: it was, if it is
        # what generating gives for the content it belongs to.
        was_generated = post.excerpt == make_excerpt(post.content)
        if sent.get("excerpt") == "" or ("excerpt" not in sent and was_generated):
            # Asked for a generated excerpt again, or it was generated all
            # along: keep it in step with the content. An excerpt the author
            # wrote is left alone.
            sent["excerpt"] = make_excerpt(content)

        # Only what differs is written, so that a request which changes
        # nothing doesn't move the "last edited" date.
        changes = {field: value for field, value in sent.items() if value != getattr(post, field)}
        if changes:
            updated = await self._posts.update(post_id, changes)
            if updated is None:  # deleted since we loaded it
                raise NotFoundError(_NOT_FOUND)
            post = updated
        return await self._view(principal, post)

    async def set_published(
        self, principal: Principal, post_id: ObjectId, *, published: bool
    ) -> PostView:
        """Publish or unpublish. Asking for the state it is already in changes nothing."""
        post = await self._load(principal, post_id)
        authorize(
            principal,
            any_=Perm.POSTS_PUBLISH_ANY,
            own=Perm.POSTS_PUBLISH_OWN,
            owner_id=post.author_id,
        )
        if (post.status == "published") != published:
            updated = await self._posts.set_published(post_id, published=published)
            if updated is None:
                raise NotFoundError(_NOT_FOUND)
            post = updated
        return await self._view(principal, post)

    async def delete(self, principal: Principal, post_id: ObjectId) -> None:
        post = await self._load(principal, post_id)
        authorize(
            principal,
            any_=Perm.POSTS_DELETE_ANY,
            own=Perm.POSTS_DELETE_OWN,
            owner_id=post.author_id,
        )

        async def work(session: Session) -> None:
            # The post and what hangs from it go together or not at all, so
            # nothing is left pointing at a post that isn't there. The post
            # first: it is the document other requests are most likely to be
            # writing, so a clash is found before the larger work is done.
            await self._posts.delete(post_id, session=session)
            for clean_up in self._on_delete:
                await clean_up(post_id, session=session)

        await in_transaction(self._db, work)

    # --- helpers ------------------------------------------------------------

    async def _load(self, principal: Principal, post_id: ObjectId) -> Post:
        """
        The post, if the caller may know that it exists.

        Two different refusals follow from this. A post the caller can't see
        (someone else's draft) is "not found", because "forbidden" would
        confirm that it is there. A post they can see but not change is
        "forbidden", which the `authorize` call after this decides.
        """
        post = await self._posts.get(post_id)
        if post is None or not may_see(principal, post):
            raise NotFoundError(_NOT_FOUND)
        return post

    async def _view(self, principal: Principal | None, post: Post) -> PostView:
        liked = await self._liked_among(principal, [post])
        return PostView(post, await self._users.get(post.author_id), post.id in liked)

    async def _views(self, principal: Principal | None, posts: list[Post]) -> list[PostView]:
        # One query for all the authors of a page, not one per post, and one
        # more for which of them the caller has liked.
        authors = await self._users.get_many({post.author_id for post in posts})
        liked = await self._liked_among(principal, posts)
        return [PostView(post, authors.get(post.author_id), post.id in liked) for post in posts]

    async def _liked_among(self, principal: Principal | None, posts: list[Post]) -> set[ObjectId]:
        if principal is None or self._liked is None or not posts:
            return set()
        return await self._liked(principal.user.id, [post.id for post in posts])
