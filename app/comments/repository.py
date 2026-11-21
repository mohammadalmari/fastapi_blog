"""Every MongoDB query on the ``comments`` collection."""

from bson import ObjectId
from pymongo import ASCENDING, IndexModel, ReturnDocument

from app.comments.models import Comment
from app.core.database import Database, Document, Session, register_indexes, utcnow
from app.core.pagination import CursorPosition, cursor_filter, cursor_sort, split_page

COLLECTION = "comments"

register_indexes(
    COLLECTION,
    [
        # One index serves both lists, oldest first: a post's own comments
        # (parent_id null) and the replies to one comment. It ends with the
        # sort fields, date and then `_id`, so neither list needs sorting.
        IndexModel(
            [
                ("post_id", ASCENDING),
                ("parent_id", ASCENDING),
                ("created_at", ASCENDING),
                ("_id", ASCENDING),
            ],
            name="thread",
        ),
    ],
)


# The two lists' filters. Nothing but the index's leading fields: every row
# that exists is one to show, so a list reads only what it returns. That holds
# because deleting removes a row unless replies hang from it (see `remove`).
# An earlier version kept every deleted row and filtered them out here, and a
# list then read thousands of rows to return twenty.


def top_level_query(post_id: ObjectId) -> Document:
    return {"post_id": post_id, "parent_id": None}


def replies_query(parent: Comment) -> Document:
    # `post_id` is here because the index starts with it. Without it MongoDB
    # scans the whole collection and sorts.
    return {"post_id": parent.post_id, "parent_id": parent.id}


def _to_comment(doc: Document) -> Comment:
    return Comment.model_validate({**doc, "id": doc["_id"]})


class CommentRepository:
    def __init__(self, db: Database) -> None:
        self._comments = db[COLLECTION]

    async def create(
        self,
        *,
        post_id: ObjectId,
        author_id: ObjectId,
        parent_id: ObjectId | None,
        content: str,
        session: Session,
    ) -> Comment:
        now = utcnow()
        doc: Document = {
            "post_id": post_id,
            "author_id": author_id,
            "parent_id": parent_id,
            "content": content,
            "is_deleted": False,
            "reply_count": 0,
            "like_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        await self._comments.insert_one(doc, session=session)  # adds "_id" to doc
        return _to_comment(doc)

    async def get(self, comment_id: ObjectId) -> Comment | None:
        doc = await self._comments.find_one({"_id": comment_id})
        return _to_comment(doc) if doc else None

    async def set_content(self, comment_id: ObjectId, content: str) -> Comment | None:
        """Change what a live comment says. None if it is gone or deleted."""
        doc = await self._comments.find_one_and_update(
            {"_id": comment_id, "is_deleted": False},
            {"$set": {"content": content, "updated_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
        return _to_comment(doc) if doc else None

    async def add_reply(self, parent_id: ObjectId, *, session: Session) -> bool:
        """
        Count one more reply on a comment, if it can take replies: it must be
        live and not itself a reply. False if it can't.

        Checked here, inside the transaction that inserts the reply, so that
        it holds at the moment of writing. (The service has already refused a
        reply to a reply, for its own message; that part of the condition is
        a second line of defence.)
        """
        result = await self._comments.update_one(
            {"_id": parent_id, "is_deleted": False, "parent_id": None},
            {"$inc": {"reply_count": 1}},
            session=session,
        )
        return result.modified_count == 1

    async def remove(self, comment: Comment, *, session: Session) -> bool:
        """
        Delete a comment. False if it was already deleted.

        What is kept depends on what hangs from it:

        - A reply, or a comment without replies: the row is removed.
        - A comment with replies: the row stays as a placeholder, its content
          erased, so the replies keep their place. It is removed when the
          last of them is.

        Every step carries its condition in its own filter, so of two
        requests deleting the same comment exactly one gets True, and the
        counters that follow are adjusted once.
        """
        if comment.parent_id is not None:
            removed = await self._comments.delete_one({"_id": comment.id}, session=session)
            if removed.deleted_count == 0:
                return False
            parent = await self._comments.find_one_and_update(
                {"_id": comment.parent_id},
                {"$inc": {"reply_count": -1}},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if parent and parent.get("is_deleted") and parent["reply_count"] <= 0:
                await self._comments.delete_one({"_id": parent["_id"]}, session=session)
            return True

        removed = await self._comments.delete_one(
            {"_id": comment.id, "reply_count": {"$lte": 0}}, session=session
        )
        if removed.deleted_count == 1:
            return True
        # It has replies: keep the row. `updated_at` is left alone, since
        # deleting is not editing.
        kept = await self._comments.update_one(
            {"_id": comment.id, "is_deleted": False},
            {"$set": {"is_deleted": True, "content": ""}},
            session=session,
        )
        return kept.modified_count == 1

    async def list_for_post(
        self, post_id: ObjectId, *, after: CursorPosition | None, limit: int
    ) -> tuple[list[Comment], str | None]:
        """A post's own comments, oldest first; replies are listed separately."""
        return await self._page(top_level_query(post_id), after, limit)

    async def list_replies(
        self, parent: Comment, *, after: CursorPosition | None, limit: int
    ) -> tuple[list[Comment], str | None]:
        return await self._page(replies_query(parent), after, limit)

    async def _page(
        self, query: Document, after: CursorPosition | None, limit: int
    ) -> tuple[list[Comment], str | None]:
        """One page in the order a conversation happened (see core.pagination)."""
        if after is not None:
            query |= cursor_filter("created_at", after, descending=False)
        docs = await (
            self._comments.find(query)
            .sort(cursor_sort("created_at", descending=False))
            .limit(limit + 1)
            .to_list()
        )
        docs, next_cursor = split_page(docs, limit, "created_at")
        return [_to_comment(doc) for doc in docs], next_cursor

    async def delete_for_post(self, post_id: ObjectId, *, session: Session) -> None:
        """Remove every comment of a post, for when the post itself is deleted."""
        await self._comments.delete_many({"post_id": post_id}, session=session)
