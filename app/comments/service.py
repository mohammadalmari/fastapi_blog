"""Commenting on posts, replying, editing and deleting, and who may do which."""

from bson import ObjectId

from app.comments.models import Comment, CommentView
from app.comments.repository import CommentRepository
from app.comments.schemas import CommentCreate, CommentUpdate
from app.core.database import Database, Session, in_transaction
from app.core.errors import ConflictError, NotFoundError
from app.core.pagination import CursorPosition
from app.posts.models import Post
from app.posts.policy import may_see
from app.posts.repository import PostRepository
from app.rbac.permissions import Perm
from app.rbac.policy import Principal, authorize
from app.users.repository import UserRepository

_NO_COMMENT = "Comment not found"
_NO_POST = "Post not found"
_ONLY_PUBLISHED = "Only published posts can be commented on"
_DELETED = "This comment was deleted"


class _PostClosedError(Exception):
    """Inside a transaction: the post took no comment at the moment of writing."""


class _ParentClosedError(Exception):
    """Inside a transaction: the comment took no reply at the moment of writing."""


class CommentService:
    def __init__(
        self,
        db: Database,
        comments: CommentRepository,
        posts: PostRepository,
        users: UserRepository,
    ) -> None:
        self._db = db
        self._comments = comments
        self._posts = posts
        self._users = users

    # --- reading ------------------------------------------------------------

    async def list_for_post(
        self,
        principal: Principal | None,
        post_id: ObjectId,
        *,
        after: CursorPosition | None,
        limit: int,
    ) -> tuple[list[CommentView], str | None]:
        """A post's own comments, oldest first. Hidden when the post is."""
        post = await self._visible_post(principal, post_id)
        comments, next_cursor = await self._comments.list_for_post(
            post.id, after=after, limit=limit
        )
        return await self._views(comments), next_cursor

    async def list_replies(
        self,
        principal: Principal | None,
        comment_id: ObjectId,
        *,
        after: CursorPosition | None,
        limit: int,
    ) -> tuple[list[CommentView], str | None]:
        # A placeholder may be asked for its replies: holding them in place
        # is the reason its row was kept.
        parent, _ = await self._load(principal, comment_id, placeholder_too=True)
        if parent.parent_id is not None:
            return [], None  # a reply has no replies
        comments, next_cursor = await self._comments.list_replies(parent, after=after, limit=limit)
        return await self._views(comments), next_cursor

    # --- writing ------------------------------------------------------------

    async def create(
        self, principal: Principal, post_id: ObjectId, data: CommentCreate
    ) -> CommentView:
        post = await self._visible_post(principal, post_id)
        return await self._insert(principal, post, None, data)

    async def reply(
        self, principal: Principal, comment_id: ObjectId, data: CommentCreate
    ) -> CommentView:
        parent, post = await self._load(principal, comment_id, placeholder_too=True)
        if parent.parent_id is not None:
            # One level only: a thread is a comment and its replies. Checked
            # here and not in the transaction because it can never change.
            raise ConflictError("Replies can't be replied to")
        return await self._insert(principal, post, parent, data)

    async def _insert(
        self, principal: Principal, post: Post, parent: Comment | None, data: CommentCreate
    ) -> CommentView:
        async def work(session: Session) -> Comment:
            # The comment and the counts that include it are written together
            # or not at all, so a count can't drift from what it counts.
            #
            # Each count goes before the comment because it carries a
            # condition, "the post is published" and "the comment being
            # replied to is still there", checked at the moment of writing.
            # That is the only place these are checked: a check before the
            # transaction would say the same thing a moment too early.
            if not await self._posts.add_comment(post.id, session=session):
                raise _PostClosedError
            if parent is not None and not await self._comments.add_reply(
                parent.id, session=session
            ):
                raise _ParentClosedError
            return await self._comments.create(
                post_id=post.id,
                author_id=principal.user.id,
                parent_id=parent.id if parent else None,
                content=data.content,
                session=session,
            )

        try:
            comment = await in_transaction(self._db, work)
        except _PostClosedError:
            # Why not? If the post is gone, or hidden from this caller now,
            # the answer is "not found", as it would have been a moment
            # earlier. Otherwise they can see it and it isn't published.
            await self._visible_post(principal, post.id)
            raise ConflictError(_ONLY_PUBLISHED) from None
        except _ParentClosedError:
            if parent is None or await self._comments.get(parent.id) is None:
                raise NotFoundError(_NO_COMMENT) from None
            raise ConflictError(_DELETED) from None
        return CommentView(comment, principal.user)

    async def update(
        self, principal: Principal, comment_id: ObjectId, data: CommentUpdate
    ) -> CommentView:
        comment, _ = await self._load(principal, comment_id)
        # There is no "edit any comment" permission, on purpose: a moderator
        # may remove what someone said, but not change it.
        authorize(principal, own=Perm.COMMENTS_UPDATE_OWN, owner_id=comment.author_id)
        if data.content != comment.content:
            updated = await self._comments.set_content(comment.id, data.content)
            if updated is None:  # deleted since we loaded it
                raise NotFoundError(_NO_COMMENT)
            comment = updated
        # The check above passed, so the author is the caller.
        return CommentView(comment, principal.user)

    async def delete(self, principal: Principal, comment_id: ObjectId) -> None:
        comment = await self._comments.get(comment_id)
        if comment is None or comment.is_deleted:
            raise NotFoundError(_NO_COMMENT)
        if comment.author_id != principal.user.id:
            # Someone else's comment is as visible as its post. Your own you
            # may always take back, even from under a post that was pulled:
            # otherwise pulling a post would freeze what others said there.
            post = await self._posts.get(comment.post_id)
            if post is None or not may_see(principal, post):
                raise NotFoundError(_NO_COMMENT)
        authorize(
            principal,
            any_=Perm.COMMENTS_DELETE_ANY,
            own=Perm.COMMENTS_DELETE_OWN,
            owner_id=comment.author_id,
        )

        async def work(session: Session) -> None:
            if not await self._comments.remove(comment, session=session):
                # Someone deleted it first. Stopping here is what keeps the
                # counts from going down twice.
                raise NotFoundError(_NO_COMMENT)
            await self._posts.remove_comment(comment.post_id, session=session)

        await in_transaction(self._db, work)

    # --- helpers ------------------------------------------------------------

    async def _visible_post(self, principal: Principal | None, post_id: ObjectId) -> Post:
        post = await self._posts.get(post_id)
        if post is None or not may_see(principal, post):
            raise NotFoundError(_NO_POST)
        return post

    async def _load(
        self, principal: Principal | None, comment_id: ObjectId, *, placeholder_too: bool = False
    ) -> tuple[Comment, Post]:
        """
        The comment and its post, if the caller may know the comment exists.

        A comment is as visible as the post it is on: when the post is hidden
        from the caller, so is everything said under it, and the answer is
        "not found" rather than a refusal that would confirm it is there.

        A placeholder (a deleted comment kept for its replies) is found only
        by the routes about replies. A deleted comment with no replies has no
        row at all, so it is "not found" like one that never existed.
        """
        comment = await self._comments.get(comment_id)
        if comment is None or (comment.is_deleted and not placeholder_too):
            raise NotFoundError(_NO_COMMENT)
        post = await self._posts.get(comment.post_id)
        if post is None or not may_see(principal, post):
            raise NotFoundError(_NO_COMMENT)
        return comment, post

    async def _views(self, comments: list[Comment]) -> list[CommentView]:
        # One query for all the authors of a page, not one per comment. A
        # placeholder shows no author: its author is left out of the query,
        # so the lookup below finds nobody for it.
        live = [comment for comment in comments if not comment.is_deleted]
        authors = await self._users.get_many({comment.author_id for comment in live})
        return [CommentView(comment, authors.get(comment.author_id)) for comment in comments]
