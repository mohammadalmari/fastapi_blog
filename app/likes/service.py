"""Liking posts and comments, and taking a like back."""

from collections.abc import Awaitable, Callable

from bson import ObjectId

from app.comments.repository import CommentRepository
from app.core.database import Database, Session, in_transaction
from app.core.errors import NotFoundError
from app.likes.repository import AlreadyLikedError, LikeRepository, LikeTarget
from app.posts.policy import may_see, may_see_unpublished
from app.posts.repository import PostRepository
from app.rbac.policy import Principal

_NO_POST = "Post not found"
_NO_COMMENT = "Comment not found"

# The two halves of keeping a count, each run inside the transaction that
# stores or removes the like. Counting up answers whether the thing was still
# there, and still the caller's to see, at the moment of writing.
type _CountUp = Callable[[Session], Awaitable[bool]]
type _CountDown = Callable[[Session], Awaitable[None]]


class _TargetGoneError(Exception):
    """Inside a transaction: the thing being liked was gone at the moment of writing."""


class LikeService:
    def __init__(
        self,
        db: Database,
        likes: LikeRepository,
        posts: PostRepository,
        comments: CommentRepository,
    ) -> None:
        self._db = db
        self._likes = likes
        self._posts = posts
        self._comments = comments

    async def like_post(self, principal: Principal, post_id: ObjectId) -> None:
        post = await self._posts.get(post_id)
        if post is None:
            raise NotFoundError(_NO_POST)
        # "You may see it" is decided in one place, the counting update: the
        # post must be published, unless this caller would see it as a draft
        # too. Whether they would never changes during the request; whether
        # it is published can, which is why that part is in the write.
        published_only = not may_see_unpublished(principal, post)

        async def count_up(session: Session) -> bool:
            return await self._posts.add_like(
                post_id, published_only=published_only, session=session
            )

        if not await self._like(principal, "post", post_id, post_id, count_up):
            raise NotFoundError(_NO_POST)

    async def unlike_post(self, principal: Principal, post_id: ObjectId) -> None:
        async def count_down(session: Session) -> None:
            await self._posts.remove_like(post_id, session=session)

        await self._unlike(principal, "post", post_id, count_down)

    async def like_comment(self, principal: Principal, comment_id: ObjectId) -> None:
        comment = await self._comments.get(comment_id)
        if comment is None:
            raise NotFoundError(_NO_COMMENT)

        async def count_up(session: Session) -> bool:
            # A comment is as visible as its post. The post is another
            # document, so it can't be a condition of the update below; it is
            # read inside the transaction instead, in the same view of the
            # data the like is written in. The update's own condition is that
            # the comment is still there and not a placeholder: there is
            # nothing left of one to like.
            post = await self._posts.get(comment.post_id, session=session)
            if post is None or not may_see(principal, post):
                return False
            return await self._comments.add_like(comment_id, session=session)

        if not await self._like(principal, "comment", comment_id, comment.post_id, count_up):
            raise NotFoundError(_NO_COMMENT)

    async def unlike_comment(self, principal: Principal, comment_id: ObjectId) -> None:
        async def count_down(session: Session) -> None:
            await self._comments.remove_like(comment_id, session=session)

        await self._unlike(principal, "comment", comment_id, count_down)

    # --- helpers ------------------------------------------------------------

    async def _like(
        self,
        principal: Principal,
        target: LikeTarget,
        target_id: ObjectId,
        post_id: ObjectId,
        count_up: _CountUp,
    ) -> bool:
        """Like it, once. False if it was gone, or hidden, by the time of writing."""

        async def work(session: Session) -> None:
            # The like and the count that includes it are written together or
            # not at all. The like goes first: a repeated like stops at the
            # unique index without touching the document everyone else is
            # writing to.
            await self._likes.add(
                user_id=principal.user.id,
                target=target,
                target_id=target_id,
                post_id=post_id,
                session=session,
            )
            if not await count_up(session):
                raise _TargetGoneError

        try:
            await in_transaction(self._db, work)
        except AlreadyLikedError:
            pass  # asking again for what is already so is not an error
        except _TargetGoneError:
            return False
        return True

    async def _unlike(
        self, principal: Principal, target: LikeTarget, target_id: ObjectId, count_down: _CountDown
    ) -> None:
        """
        Take the caller's like back, if they have one.

        Nothing is looked up about the thing itself, and there is one answer
        whatever has become of it. A "not found" for some ids would let
        someone who liked a post learn later whether it was pulled (their
        like is still there) or deleted (it went with the post).
        """

        async def work(session: Session) -> None:
            removed = await self._likes.remove(
                user_id=principal.user.id, target=target, target_id=target_id, session=session
            )
            if removed:
                # Only a like that was really there lowers the count, which
                # is what keeps a repeated unlike from lowering it twice.
                await count_down(session)

        await in_transaction(self._db, work)
