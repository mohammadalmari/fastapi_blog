"""Writing, publishing and reading posts, and who may do which."""

from bson import ObjectId

from app.core.errors import ConflictError, NotFoundError
from app.core.pagination import CursorPosition
from app.posts.models import Post, PostStatus, PostView, TagCount
from app.posts.repository import PostRepository, SlugUnavailableError
from app.posts.rules import make_excerpt
from app.posts.schemas import PostCreate, PostUpdate
from app.rbac.permissions import Perm
from app.rbac.policy import Principal, authorize
from app.users.repository import UserRepository

_NOT_FOUND = "Post not found"

# Any of these lets its holder see other people's drafts. Reading them is its
# own permission, but so is each way of acting on them: someone trusted to
# publish any post has to be able to see the one they are publishing.
_DRAFT_ACCESS = (
    Perm.POSTS_READ_DRAFTS_ANY,
    Perm.POSTS_UPDATE_ANY,
    Perm.POSTS_PUBLISH_ANY,
    Perm.POSTS_DELETE_ANY,
)


class PostService:
    def __init__(self, posts: PostRepository, users: UserRepository) -> None:
        self._posts = posts
        self._users = users

    # --- reading ------------------------------------------------------------

    async def get_by_slug(self, principal: Principal | None, slug: str) -> PostView:
        post = await self._posts.get_by_slug(slug)
        if post is None or not _may_see(principal, post):
            raise NotFoundError(_NOT_FOUND)
        return await self._view(post)

    async def feed(
        self, *, tag: str | None, author: str | None, after: CursorPosition | None, limit: int
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
        return await self._views(posts), next_cursor

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
        return [PostView(post, principal.user) for post in posts], next_cursor

    async def tags(self) -> list[TagCount]:
        return await self._posts.tag_counts()

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
        return await self._view(post)

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
        return await self._view(post)

    async def delete(self, principal: Principal, post_id: ObjectId) -> None:
        post = await self._load(principal, post_id)
        authorize(
            principal,
            any_=Perm.POSTS_DELETE_ANY,
            own=Perm.POSTS_DELETE_OWN,
            owner_id=post.author_id,
        )
        await self._posts.delete(post_id)

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
        if post is None or not _may_see(principal, post):
            raise NotFoundError(_NOT_FOUND)
        return post

    async def _view(self, post: Post) -> PostView:
        return PostView(post, await self._users.get(post.author_id))

    async def _views(self, posts: list[Post]) -> list[PostView]:
        # One query for all the authors of a page, not one per post.
        authors = await self._users.get_many({post.author_id for post in posts})
        return [PostView(post, authors.get(post.author_id)) for post in posts]


def _may_see(principal: Principal | None, post: Post) -> bool:
    """Everyone sees a published post. A draft: its author, and whoever may act on drafts."""
    if post.status == "published":
        return True
    if principal is None:
        return False
    if post.author_id == principal.user.id:
        return True
    return any(principal.has(permission) for permission in _DRAFT_ACCESS)
