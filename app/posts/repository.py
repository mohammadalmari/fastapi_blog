"""Every MongoDB query on the ``posts`` collection."""

import secrets
from collections.abc import Sequence

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, IndexModel, ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.database import Database, Document, Session, register_indexes, utcnow
from app.core.pagination import CursorPosition, cursor_filter, cursor_sort, split_page
from app.posts.models import Post, PostStatus, TagCount
from app.posts.rules import slugify

COLLECTION = "posts"

# Each list below is "filter on some fields, newest first", and newest first
# means sorting by a date and then by `_id` to break ties (see
# core.pagination). An index serves such a query in order, with no sorting,
# only if it holds the filter fields first and then *both* sort fields. Leave
# `_id` off the end and MongoDB must load every match and sort it in memory:
# the author feed once read 13,300 index entries to return 21 posts.
register_indexes(
    COLLECTION,
    [
        IndexModel("slug", unique=True),
        IndexModel(
            [("status", ASCENDING), ("published_at", DESCENDING), ("_id", DESCENDING)],
            name="feed",
        ),
        # `tags` is an array; MongoDB indexes each element.
        IndexModel(
            [
                ("tags", ASCENDING),
                ("status", ASCENDING),
                ("published_at", DESCENDING),
                ("_id", DESCENDING),
            ],
            name="feed_by_tag",
        ),
        IndexModel(
            [
                ("author_id", ASCENDING),
                ("status", ASCENDING),
                ("published_at", DESCENDING),
                ("_id", DESCENDING),
            ],
            name="feed_by_author",
        ),
        # An author's own list, drafts included.
        IndexModel(
            [("author_id", ASCENDING), ("created_at", DESCENDING), ("_id", DESCENDING)],
            name="posts_by_author",
        ),
    ],
)

# Three random bytes, written as six characters: sixteen million endings per
# title. A clash is then so unlikely that a few tries settle it.
_SLUG_ENDING_BYTES = 3
_SLUG_ATTEMPTS = 5
TAG_LIMIT = 100


class SlugUnavailableError(Exception):
    """No free slug was found for the title in the allowed number of tries."""


def _to_post(doc: Document) -> Post:
    return Post.model_validate({**doc, "id": doc["_id"]})


class PostRepository:
    def __init__(self, db: Database) -> None:
        self._posts = db[COLLECTION]

    async def create(
        self, *, author_id: ObjectId, title: str, content: str, excerpt: str, tags: Sequence[str]
    ) -> Post:
        now = utcnow()
        fields: Document = {
            "author_id": author_id,
            "title": title,
            "content": content,
            "excerpt": excerpt,
            "tags": list(tags),
            "status": "draft",
            "published_at": None,
            "like_count": 0,
            "comment_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        base = slugify(title)
        for _ in range(_SLUG_ATTEMPTS):
            # Every slug gets a random ending, not only one whose plain form
            # is taken. Drafts hold slugs too, so "you got an ending" would
            # otherwise tell an author that someone has an unpublished post
            # with that title.
            doc = {**fields, "slug": f"{base}-{secrets.token_hex(_SLUG_ENDING_BYTES)}"}
            try:
                # The unique index decides whether the slug is free. Asking
                # first would leave a gap between asking and inserting.
                await self._posts.insert_one(doc)  # adds "_id" to doc
            except DuplicateKeyError:
                continue
            return _to_post(doc)
        raise SlugUnavailableError(base)

    async def get(self, post_id: ObjectId, *, session: Session | None = None) -> Post | None:
        doc = await self._posts.find_one({"_id": post_id}, session=session)
        return _to_post(doc) if doc else None

    async def get_by_slug(self, slug: str) -> Post | None:
        doc = await self._posts.find_one({"slug": slug})
        return _to_post(doc) if doc else None

    async def update(self, post_id: ObjectId, changes: Document) -> Post | None:
        """Change the post's text or tags. None if the post is gone."""
        doc = await self._posts.find_one_and_update(
            {"_id": post_id},
            {"$set": {**changes, "updated_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
        return _to_post(doc) if doc else None

    async def set_published(self, post_id: ObjectId, *, published: bool) -> Post | None:
        # `updated_at` is left alone: it says when the text last changed.
        changes: Document = {"status": "published" if published else "draft"}
        if published:
            # Keep the date of the first publication if there is one. Written
            # as an expression so that it is decided inside the update itself.
            changes["published_at"] = {"$ifNull": ["$published_at", utcnow()]}
        doc = await self._posts.find_one_and_update(
            {"_id": post_id},
            [{"$set": changes}],  # a list makes this a pipeline, where "$published_at" is a field
            return_document=ReturnDocument.AFTER,
        )
        return _to_post(doc) if doc else None

    async def delete(self, post_id: ObjectId, *, session: Session) -> None:
        await self._posts.delete_one({"_id": post_id}, session=session)

    async def add_comment(self, post_id: ObjectId, *, session: Session) -> bool:
        """
        Count one more comment on a post, if it takes comments: it must be
        published. False if it doesn't.

        The condition is part of the update, so it holds at the moment of
        writing and not only when the service looked a moment earlier.
        """
        result = await self._posts.update_one(
            {"_id": post_id, "status": "published"},
            {"$inc": {"comment_count": 1}},
            session=session,
        )
        return result.modified_count == 1

    async def remove_comment(self, post_id: ObjectId, *, session: Session) -> None:
        await self._posts.update_one(
            {"_id": post_id}, {"$inc": {"comment_count": -1}}, session=session
        )

    async def add_like(self, post_id: ObjectId, *, published_only: bool, session: Session) -> bool:
        """
        Count one more like on a post. False if the post is gone, or is a
        draft when only a published post will do.

        As with `add_comment`, the condition is part of the update, so it
        holds at the moment of writing.
        """
        query: Document = {"_id": post_id}
        if published_only:
            query["status"] = "published"
        result = await self._posts.update_one(query, {"$inc": {"like_count": 1}}, session=session)
        return result.modified_count == 1

    async def remove_like(self, post_id: ObjectId, *, session: Session) -> None:
        await self._posts.update_one(
            {"_id": post_id}, {"$inc": {"like_count": -1}}, session=session
        )

    async def list_published(
        self,
        *,
        tag: str | None,
        author_id: ObjectId | None,
        after: CursorPosition | None,
        limit: int,
    ) -> tuple[list[Post], str | None]:
        query: Document = {"status": "published"}
        if tag is not None:
            query["tags"] = tag
        if author_id is not None:
            query["author_id"] = author_id
        return await self._page(query, "published_at", after, limit)

    async def list_by_author(
        self,
        author_id: ObjectId,
        *,
        status: PostStatus | None,
        after: CursorPosition | None,
        limit: int,
    ) -> tuple[list[Post], str | None]:
        query: Document = {"author_id": author_id}
        if status is not None:
            query["status"] = status
        # By creation, not publication: drafts have no publication date.
        return await self._page(query, "created_at", after, limit)

    async def _page(
        self, query: Document, sort_field: str, after: CursorPosition | None, limit: int
    ) -> tuple[list[Post], str | None]:
        """One page, newest first, and the cursor for the next (see core.pagination)."""
        if after is not None:
            query |= cursor_filter(sort_field, after, descending=True)
        docs = await (
            self._posts.find(query)
            .sort(cursor_sort(sort_field, descending=True))
            .limit(limit + 1)
            .to_list()
        )
        docs, next_cursor = split_page(docs, limit, sort_field)
        return [_to_post(doc) for doc in docs], next_cursor

    async def tag_counts(self) -> list[TagCount]:
        """The most used tags of published posts, most used first, at most TAG_LIMIT."""
        cursor = await self._posts.aggregate(
            [
                {"$match": {"status": "published"}},
                {"$unwind": "$tags"},  # one document per (post, tag) pair
                {"$group": {"_id": "$tags", "count": {"$sum": 1}}},
                {"$sort": {"count": -1, "_id": 1}},
                {"$limit": TAG_LIMIT},
            ]
        )
        return [TagCount(name=doc["_id"], count=doc["count"]) async for doc in cursor]
