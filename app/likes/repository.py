"""Every MongoDB query on the ``likes`` collection."""

from collections.abc import Collection
from typing import Literal

from bson import ObjectId
from pymongo import ASCENDING, IndexModel
from pymongo.errors import DuplicateKeyError

from app.comments.models import Comment
from app.core.database import Database, Document, Session, register_indexes, utcnow

COLLECTION = "likes"

type LikeTarget = Literal["post", "comment"]

register_indexes(
    COLLECTION,
    [
        # One like per person per thing. This index is what makes liking
        # twice the same as liking once: the second insert is refused by the
        # database, however the two requests were timed. It also answers
        # "which of these has this person liked?".
        IndexModel(
            [("user_id", ASCENDING), ("target_type", ASCENDING), ("target_id", ASCENDING)],
            unique=True,
            name="one_like_each",
        ),
        # For cleaning up. Every like records the post it sits under (the
        # post itself, or the post of the comment), so "everything under
        # this post" and "everything on this comment" are both read here.
        IndexModel([("post_id", ASCENDING), ("target_id", ASCENDING)], name="likes_under_post"),
    ],
)


class AlreadyLikedError(Exception):
    """This person's like of this thing is already stored."""


def liked_query(user_id: ObjectId, target: LikeTarget, ids: Collection[ObjectId]) -> Document:
    return {"user_id": user_id, "target_type": target, "target_id": {"$in": list(ids)}}


def under_post_query(post_id: ObjectId) -> Document:
    return {"post_id": post_id}


def on_comment_query(comment: Comment) -> Document:
    # `post_id` is here because the index starts with it.
    return {"post_id": comment.post_id, "target_id": comment.id, "target_type": "comment"}


class LikeRepository:
    def __init__(self, db: Database) -> None:
        self._likes = db[COLLECTION]

    async def add(
        self,
        *,
        user_id: ObjectId,
        target: LikeTarget,
        target_id: ObjectId,
        post_id: ObjectId,
        session: Session,
    ) -> None:
        """Store a like. Raises AlreadyLikedError if it is already there."""
        try:
            await self._likes.insert_one(
                {
                    "user_id": user_id,
                    "target_type": target,
                    "target_id": target_id,
                    "post_id": post_id,
                    "created_at": utcnow(),
                },
                session=session,
            )
        except DuplicateKeyError as error:
            raise AlreadyLikedError from error

    async def remove(
        self, *, user_id: ObjectId, target: LikeTarget, target_id: ObjectId, session: Session
    ) -> bool:
        """Take a like back. False if there was none."""
        result = await self._likes.delete_one(
            {"user_id": user_id, "target_type": target, "target_id": target_id}, session=session
        )
        return result.deleted_count == 1

    async def liked_posts(self, user_id: ObjectId, ids: Collection[ObjectId]) -> set[ObjectId]:
        return await self._liked(user_id, "post", ids)

    async def liked_comments(self, user_id: ObjectId, ids: Collection[ObjectId]) -> set[ObjectId]:
        return await self._liked(user_id, "comment", ids)

    async def _liked(
        self, user_id: ObjectId, target: LikeTarget, ids: Collection[ObjectId]
    ) -> set[ObjectId]:
        """Which of these the user has liked: one query for a whole page."""
        cursor = self._likes.find(liked_query(user_id, target, ids), {"target_id": 1})
        return {doc["target_id"] async for doc in cursor}

    async def delete_for_post(self, post_id: ObjectId, *, session: Session) -> None:
        """Remove the likes on a post and on every comment under it."""
        await self._likes.delete_many(under_post_query(post_id), session=session)

    async def delete_for_comment(self, comment: Comment, *, session: Session) -> None:
        await self._likes.delete_many(on_comment_query(comment), session=session)
