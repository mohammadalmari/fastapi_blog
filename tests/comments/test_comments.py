"""
The comments routes. Each follows the same checklist: success, invalid input,
not logged in, not allowed, not found, conflict, and what changed in the
database. Field rules are unit tests in test_comment_rules.py.
"""

from datetime import UTC, datetime, timedelta

import anyio
import pytest
from bson import ObjectId
from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.comments import repository as comments_repository
from app.comments.models import Comment
from app.comments.repository import CommentRepository
from app.core.database import Database
from app.posts.models import Post
from app.posts.repository import PostRepository
from app.rbac.permissions import Perm
from app.users.models import User
from tests.conftest import HeadersFor, MakeComment, MakePost, MakeRole, MakeUser
from tests.helpers import API, plan_stages

pytestmark = pytest.mark.anyio

Headers = dict[str, str]
MISSING = "5f0000000000000000000000"
FORBIDDEN = {"detail": "You don't have permission to do this"}
NO_COMMENT = {"detail": "Comment not found"}
NO_POST = {"detail": "Post not found"}


async def _contents(client: AsyncClient, path: str, **params: str) -> list[str | None]:
    response = await client.get(f"{API}{path}", params=params)
    assert response.status_code == 200, response.text
    return [item["content"] for item in response.json()["items"]]


async def _counts(
    db: Database, post: Post, comment: Comment | None = None
) -> tuple[int, int | None]:
    """(the post's comment count, the comment's reply count) as stored."""
    stored_post = await db.posts.find_one({"_id": post.id})
    assert stored_post is not None
    replies: int | None = None
    if comment is not None:
        stored = await db.comments.find_one({"_id": comment.id})
        assert stored is not None
        replies = int(stored["reply_count"])
    return int(stored_post["comment_count"]), replies


# --- every writing route refuses strangers --------------------------------------

WRITES = [
    ("POST", f"/posts/{MISSING}/comments"),
    ("POST", f"/comments/{MISSING}/replies"),
    ("PATCH", f"/comments/{MISSING}"),
    ("DELETE", f"/comments/{MISSING}"),
]


@pytest.mark.parametrize(("method", "path"), WRITES)
async def test_route_requires_a_login(client: AsyncClient, method: str, path: str) -> None:
    response = await client.request(method, f"{API}{path}", json={"content": "Hi"})

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path"), WRITES[:2])
async def test_commenting_needs_the_permission(
    client: AsyncClient,
    make_role: MakeRole,
    make_user: MakeUser,
    headers_for: HeadersFor,
    method: str,
    path: str,
) -> None:
    await make_role("lurker")
    lurker = await make_user("lena", "lurker")

    response = await client.request(
        method, f"{API}{path}", json={"content": "Hi"}, headers=headers_for(lurker)
    )

    assert (response.status_code, response.json()) == (403, FORBIDDEN)


# --- POST /posts/{id}/comments --------------------------------------------------


async def test_comment_on_a_post(
    client: AsyncClient, db: Database, post: Post, user: User, user_headers: Headers
) -> None:
    response = await client.post(
        f"{API}/posts/{post.id}/comments", json={"content": "  Nice post.  "}, headers=user_headers
    )

    assert response.status_code == 201
    body = response.json()
    assert body["content"] == "Nice post."
    assert (body["post_id"], body["parent_id"]) == (str(post.id), None)
    assert body["author"] == {
        "id": str(user.id),
        "username": "jane",
        "display_name": None,
        "avatar_url": None,
    }
    assert (body["is_deleted"], body["reply_count"], body["like_count"]) == (False, 0, 0)

    stored = await db.comments.find_one({})
    assert stored is not None
    assert str(stored["_id"]) == body["id"]
    assert (stored["post_id"], stored["author_id"], stored["parent_id"]) == (post.id, user.id, None)
    assert await _counts(db, post) == (1, None)


async def test_comment_with_invalid_input(
    client: AsyncClient, db: Database, post: Post, user_headers: Headers
) -> None:
    """That bad input is a 422 and stores nothing; test_comment_rules.py lists the rules."""
    response = await client.post(
        f"{API}/posts/{post.id}/comments",
        json={"content": "Hi", "author_id": MISSING},
        headers=user_headers,
    )

    assert response.status_code == 422
    assert await db.comments.count_documents({}) == 0
    assert await _counts(db, post) == (0, None)


@pytest.mark.parametrize(("post_id", "status"), [(MISSING, 404), ("not-an-id", 422)])
async def test_comment_on_a_post_that_does_not_exist(
    client: AsyncClient, user_headers: Headers, post_id: str, status: int
) -> None:
    response = await client.post(
        f"{API}/posts/{post_id}/comments", json={"content": "Hi"}, headers=user_headers
    )

    assert response.status_code == status


async def test_a_draft_cannot_be_commented_on(
    client: AsyncClient,
    db: Database,
    author: User,
    user_headers: Headers,
    make_post: MakePost,
    headers_for: HeadersFor,
) -> None:
    draft = await make_post(author, "Not yet")

    by_a_stranger = await client.post(
        f"{API}/posts/{draft.id}/comments", json={"content": "Hi"}, headers=user_headers
    )
    by_its_author = await client.post(
        f"{API}/posts/{draft.id}/comments", json={"content": "Hi"}, headers=headers_for(author)
    )

    # To a stranger the draft doesn't exist. Its author can see it, so they
    # are told why not.
    assert (by_a_stranger.status_code, by_a_stranger.json()) == (404, NO_POST)
    assert by_its_author.status_code == 409
    assert by_its_author.json() == {"detail": "Only published posts can be commented on"}
    assert await db.comments.count_documents({}) == 0


# --- GET /posts/{id}/comments ---------------------------------------------------


async def test_anyone_can_read_a_posts_comments_oldest_first(
    client: AsyncClient, post: Post, user: User, author: User, make_comment: MakeComment
) -> None:
    first = await make_comment(user, post, "First")
    await make_comment(author, post, "Second")
    await make_comment(author, post, "A reply", reply_to=first)

    response = await client.get(f"{API}/posts/{post.id}/comments")

    assert response.status_code == 200
    body = response.json()
    # Replies are not in this list; the comment they belong to counts them.
    assert [(item["content"], item["reply_count"]) for item in body["items"]] == [
        ("First", 1),
        ("Second", 0),
    ]
    assert body["items"][0]["author"]["username"] == "jane"
    assert body["next_cursor"] is None


async def test_comments_page_through_every_comment_once(
    client: AsyncClient, post: Post, user: User, make_comment: MakeComment
) -> None:
    # Made in a loop, so several share a millisecond: their ids decide the order.
    for number in range(5):
        await make_comment(user, post, f"Comment {number}")

    contents: list[str | None] = []
    cursor = None
    for _ in range(4):
        params = {"limit": "2"} | ({"cursor": cursor} if cursor else {})
        page = (await client.get(f"{API}/posts/{post.id}/comments", params=params)).json()
        contents += [item["content"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert contents == [f"Comment {number}" for number in range(5)]


async def test_comments_of_a_post_that_does_not_exist(client: AsyncClient) -> None:
    response = await client.get(f"{API}/posts/{MISSING}/comments")

    assert (response.status_code, response.json()) == (404, NO_POST)


async def test_comments_are_hidden_with_their_post(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    author: User,
    make_comment: MakeComment,
    headers_for: HeadersFor,
) -> None:
    """The attack: read the discussion of a post that was pulled."""
    comment = await make_comment(user, post, "Seen by all")
    await PostRepository(db).set_published(post.id, published=False)

    as_a_visitor = await client.get(f"{API}/posts/{post.id}/comments")
    replies_as_a_visitor = await client.get(f"{API}/comments/{comment.id}/replies")
    as_the_author = await client.get(f"{API}/posts/{post.id}/comments", headers=headers_for(author))

    assert as_a_visitor.status_code == 404
    assert replies_as_a_visitor.status_code == 404
    assert as_the_author.status_code == 200


async def test_a_bad_token_on_the_comment_lists_is_refused(client: AsyncClient, post: Post) -> None:
    response = await client.get(
        f"{API}/posts/{post.id}/comments", headers={"Authorization": "Bearer nonsense"}
    )

    assert response.status_code == 401


async def test_a_comment_outlives_its_authors_account(
    client: AsyncClient, db: Database, post: Post, user: User, make_comment: MakeComment
) -> None:
    await make_comment(user, post, "Still here")
    await db.users.delete_one({"_id": user.id})

    items = (await client.get(f"{API}/posts/{post.id}/comments")).json()["items"]

    assert [(item["content"], item["author"]) for item in items] == [("Still here", None)]


async def test_a_list_that_depends_on_who_asks_is_never_cached(
    client: AsyncClient, post: Post, user: User, user_headers: Headers, make_comment: MakeComment
) -> None:
    comment = await make_comment(user, post)

    for path in (f"/posts/{post.id}/comments", f"/comments/{comment.id}/replies"):
        logged_in = await client.get(f"{API}{path}", headers=user_headers)
        anonymous = await client.get(f"{API}{path}")

        assert logged_in.headers["cache-control"] == "private, no-store"
        assert "cache-control" not in anonymous.headers


# --- POST /comments/{id}/replies ------------------------------------------------


async def test_reply_to_a_comment(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    author: User,
    make_comment: MakeComment,
    headers_for: HeadersFor,
) -> None:
    comment = await make_comment(user, post, "A question")

    response = await client.post(
        f"{API}/comments/{comment.id}/replies",
        json={"content": "An answer"},
        headers=headers_for(author),
    )

    assert response.status_code == 201
    body = response.json()
    assert (body["content"], body["parent_id"]) == ("An answer", str(comment.id))
    assert body["post_id"] == str(post.id)
    assert body["author"]["username"] == "ali"
    assert await _counts(db, post, comment) == (2, 1)


async def test_reply_to_a_comment_that_does_not_exist(
    client: AsyncClient, user_headers: Headers
) -> None:
    response = await client.post(
        f"{API}/comments/{MISSING}/replies", json={"content": "Hi"}, headers=user_headers
    )

    assert (response.status_code, response.json()) == (404, NO_COMMENT)


async def test_a_reply_cannot_be_replied_to(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    user_headers: Headers,
    make_comment: MakeComment,
) -> None:
    """One level only: a thread is a comment and its replies."""
    comment = await make_comment(user, post)
    reply = await make_comment(user, post, "A reply", reply_to=comment)

    response = await client.post(
        f"{API}/comments/{reply.id}/replies", json={"content": "Deeper"}, headers=user_headers
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Replies can't be replied to"}
    assert await _counts(db, post, reply) == (2, 0)


async def test_a_deleted_comment_cannot_be_replied_to(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    user_headers: Headers,
    make_comment: MakeComment,
) -> None:
    comment = await make_comment(user, post)
    await make_comment(user, post, "A reply", reply_to=comment)
    await client.delete(f"{API}/comments/{comment.id}", headers=user_headers)

    response = await client.post(
        f"{API}/comments/{comment.id}/replies", json={"content": "Too late"}, headers=user_headers
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "This comment was deleted"}
    assert await db.comments.count_documents({"content": "Too late"}) == 0


async def test_a_comment_on_a_pulled_post_cannot_be_replied_to(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    author: User,
    make_comment: MakeComment,
    headers_for: HeadersFor,
) -> None:
    comment = await make_comment(user, post)
    await PostRepository(db).set_published(post.id, published=False)

    # The post's author still sees the thread, so they are told why not.
    response = await client.post(
        f"{API}/comments/{comment.id}/replies",
        json={"content": "Hi"},
        headers=headers_for(author),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Only published posts can be commented on"}
    assert await _counts(db, post, comment) == (1, 0)


# --- GET /comments/{id}/replies -------------------------------------------------


async def test_read_the_replies_to_a_comment(
    client: AsyncClient, post: Post, user: User, author: User, make_comment: MakeComment
) -> None:
    comment = await make_comment(user, post, "A question")
    other = await make_comment(user, post, "Another thread")
    for number in range(3):
        await make_comment(author, post, f"Reply {number}", reply_to=comment)
    await make_comment(author, post, "Elsewhere", reply_to=other)

    first = (await client.get(f"{API}/comments/{comment.id}/replies", params={"limit": "2"})).json()
    second = (
        await client.get(
            f"{API}/comments/{comment.id}/replies",
            params={"limit": "2", "cursor": first["next_cursor"]},
        )
    ).json()

    assert [item["content"] for item in first["items"]] == ["Reply 0", "Reply 1"]
    assert [item["content"] for item in second["items"]] == ["Reply 2"]
    assert second["next_cursor"] is None


async def test_replies_of_what_cannot_have_any(
    client: AsyncClient, post: Post, user: User, make_comment: MakeComment
) -> None:
    comment = await make_comment(user, post)
    reply = await make_comment(user, post, "A reply", reply_to=comment)

    of_a_reply = await client.get(f"{API}/comments/{reply.id}/replies")
    of_nothing = await client.get(f"{API}/comments/{MISSING}/replies")

    assert (of_a_reply.status_code, of_a_reply.json()["items"]) == (200, [])
    assert (of_nothing.status_code, of_nothing.json()) == (404, NO_COMMENT)


# --- PATCH /comments/{id} -------------------------------------------------------


async def test_edit_your_comment(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    user_headers: Headers,
    make_comment: MakeComment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comment = await make_comment(user, post, "Frist")
    later = comment.created_at + timedelta(hours=1)
    monkeypatch.setattr(comments_repository, "utcnow", lambda: later)

    response = await client.patch(
        f"{API}/comments/{comment.id}", json={"content": "First"}, headers=user_headers
    )

    assert response.status_code == 200
    assert response.json()["content"] == "First"
    stored = await db.comments.find_one({"_id": comment.id})
    assert stored is not None
    assert (stored["content"], stored["updated_at"]) == ("First", later)
    assert stored["created_at"] == comment.created_at
    assert await _counts(db, post) == (1, None)


async def test_an_edit_that_changes_nothing_writes_nothing(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    user_headers: Headers,
    make_comment: MakeComment,
) -> None:
    comment = await make_comment(user, post, "Same")

    response = await client.patch(
        f"{API}/comments/{comment.id}", json={"content": " Same "}, headers=user_headers
    )

    assert response.status_code == 200
    stored = await db.comments.find_one({"_id": comment.id})
    assert stored is not None
    assert stored["updated_at"] == comment.updated_at


async def test_nobody_else_can_edit_your_comment(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    author: User,
    make_role: MakeRole,
    make_user: MakeUser,
    make_comment: MakeComment,
    headers_for: HeadersFor,
) -> None:
    """
    The attack: change the id in the URL. And the abuse: a moderator may
    remove what you said, but may not put words in your mouth.
    """
    comment = await make_comment(user, post, "My words")
    await make_role("moderator", Perm.COMMENTS_DELETE_ANY, Perm.COMMENTS_UPDATE_OWN)
    moderator = await make_user("mod", "moderator")

    for intruder in (author, moderator):
        response = await client.patch(
            f"{API}/comments/{comment.id}",
            json={"content": "Not my words"},
            headers=headers_for(intruder),
        )
        assert (response.status_code, response.json()) == (403, FORBIDDEN)

    stored = await db.comments.find_one({"_id": comment.id})
    assert stored is not None
    assert stored["content"] == "My words"


# --- DELETE /comments/{id} ------------------------------------------------------


async def test_delete_your_comment(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    user_headers: Headers,
    make_comment: MakeComment,
) -> None:
    comment = await make_comment(user, post, "Regrettable")

    response = await client.delete(f"{API}/comments/{comment.id}", headers=user_headers)

    assert response.status_code == 204
    assert await _contents(client, f"/posts/{post.id}/comments") == []
    assert await _counts(db, post) == (0, None)
    # Nothing hung from it, so nothing is kept: not the words, not the row.
    assert await db.comments.count_documents({}) == 0


async def test_deleting_a_reply_updates_both_counts(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    user_headers: Headers,
    make_comment: MakeComment,
) -> None:
    comment = await make_comment(user, post)
    await make_comment(user, post, "Stays", reply_to=comment)
    reply = await make_comment(user, post, "Goes", reply_to=comment)

    response = await client.delete(f"{API}/comments/{reply.id}", headers=user_headers)

    assert response.status_code == 204
    assert await _contents(client, f"/comments/{comment.id}/replies") == ["Stays"]
    assert await _counts(db, post, comment) == (2, 1)
    assert await db.comments.count_documents({}) == 2  # the reply's row is gone


async def test_a_deleted_comment_with_replies_leaves_a_placeholder(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    author: User,
    user_headers: Headers,
    make_comment: MakeComment,
) -> None:
    """So that the replies still have something to hang from."""
    comment = await make_comment(user, post, "The original")
    await make_comment(author, post, "A reply", reply_to=comment)

    await client.delete(f"{API}/comments/{comment.id}", headers=user_headers)

    listed = (await client.get(f"{API}/posts/{post.id}/comments")).json()["items"]
    assert len(listed) == 1
    placeholder = listed[0]
    assert (placeholder["id"], placeholder["is_deleted"]) == (str(comment.id), True)
    # Neither what was said nor who said it.
    assert (placeholder["content"], placeholder["author"]) == (None, None)
    assert placeholder["reply_count"] == 1
    assert await _contents(client, f"/comments/{comment.id}/replies") == ["A reply"]
    assert await _counts(db, post, comment) == (1, 1)
    stored = await db.comments.find_one({"_id": comment.id})
    assert stored is not None
    assert (stored["is_deleted"], stored["content"]) == (True, "")  # the words are erased
    # Deleting is not editing: the placeholder must not look edited.
    assert stored["updated_at"] == comment.updated_at


async def test_delete_a_placeholder_again(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    author: User,
    user_headers: Headers,
    make_comment: MakeComment,
    headers_for: HeadersFor,
) -> None:
    """It is already deleted: it can't be deleted or edited a second time."""
    comment = await make_comment(user, post, "The original")
    await make_comment(author, post, "A reply", reply_to=comment)
    await client.delete(f"{API}/comments/{comment.id}", headers=user_headers)

    again = await client.delete(f"{API}/comments/{comment.id}", headers=user_headers)
    edit = await client.patch(
        f"{API}/comments/{comment.id}", json={"content": "Back"}, headers=user_headers
    )
    # "Not found" for someone else as well, where an ordinary comment of
    # another user would be "forbidden".
    others = [
        await client.delete(f"{API}/comments/{comment.id}", headers=headers_for(author)),
        await client.patch(
            f"{API}/comments/{comment.id}", json={"content": "Mine"}, headers=headers_for(author)
        ),
    ]

    assert (again.status_code, again.json()) == (404, NO_COMMENT)
    assert (edit.status_code, edit.json()) == (404, NO_COMMENT)
    assert [(other.status_code, other.json()) for other in others] == [(404, NO_COMMENT)] * 2
    stored = await db.comments.find_one({"_id": comment.id})
    assert stored is not None
    assert (stored["is_deleted"], stored["content"]) == (True, "")
    assert await _counts(db, post, comment) == (1, 1)


async def test_the_placeholder_goes_when_its_last_reply_does(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    user_headers: Headers,
    make_comment: MakeComment,
) -> None:
    comment = await make_comment(user, post)
    reply = await make_comment(user, post, "A reply", reply_to=comment)
    await client.delete(f"{API}/comments/{comment.id}", headers=user_headers)

    await client.delete(f"{API}/comments/{reply.id}", headers=user_headers)

    assert await _contents(client, f"/posts/{post.id}/comments") == []
    # With nothing left to hold in place, the placeholder's row goes as well.
    assert await db.comments.count_documents({}) == 0
    assert await _counts(db, post) == (0, None)


async def test_delete_a_comment_that_is_not_there(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    author: User,
    user_headers: Headers,
    make_comment: MakeComment,
    headers_for: HeadersFor,
) -> None:
    """Including one already deleted: asking twice must not count down twice."""
    comment = await make_comment(user, post)
    await make_comment(user, post, "Another")

    unknown = await client.delete(f"{API}/comments/{MISSING}", headers=user_headers)
    first = await client.delete(f"{API}/comments/{comment.id}", headers=user_headers)
    again = await client.delete(f"{API}/comments/{comment.id}", headers=user_headers)
    edit = await client.patch(
        f"{API}/comments/{comment.id}", json={"content": "Back"}, headers=user_headers
    )
    # To someone else it is "not found" as well, not "forbidden": a refusal
    # would say that a comment used to be there.
    by_another = await client.delete(f"{API}/comments/{comment.id}", headers=headers_for(author))

    assert (unknown.status_code, first.status_code) == (404, 204)
    assert (again.status_code, again.json()) == (404, NO_COMMENT)
    assert edit.status_code == 404
    assert (by_another.status_code, by_another.json()) == (404, NO_COMMENT)
    assert await _counts(db, post) == (1, None)


async def test_only_the_author_or_a_moderator_deletes_a_comment(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    author: User,
    make_role: MakeRole,
    make_user: MakeUser,
    make_comment: MakeComment,
    headers_for: HeadersFor,
) -> None:
    comment = await make_comment(user, post)
    await make_role("moderator", Perm.COMMENTS_DELETE_ANY)
    moderator = await make_user("mod", "moderator")

    # Not even the author of the post it is on.
    refused = await client.delete(f"{API}/comments/{comment.id}", headers=headers_for(author))
    assert (refused.status_code, refused.json()) == (403, FORBIDDEN)
    assert await _counts(db, post) == (1, None)

    allowed = await client.delete(f"{API}/comments/{comment.id}", headers=headers_for(moderator))
    assert allowed.status_code == 204
    assert await _counts(db, post) == (0, None)


# --- the counts stay true -------------------------------------------------------


async def test_comments_arriving_together_are_all_counted(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    user_headers: Headers,
    make_comment: MakeComment,
) -> None:
    """No update is lost: twelve writers, twelve counted, however they interleave."""
    comment = await make_comment(user, post, "The thread")
    responses: list[Response] = []

    async def write(path: str, number: int) -> None:
        responses.append(
            await client.post(
                f"{API}{path}", json={"content": f"Comment {number}"}, headers=user_headers
            )
        )

    async with anyio.create_task_group() as group:
        for number in range(6):
            group.start_soon(write, f"/posts/{post.id}/comments", number)
            group.start_soon(write, f"/comments/{comment.id}/replies", number)

    assert [response.status_code for response in responses] == [201] * 12
    assert await db.comments.count_documents({}) == 13
    assert await _counts(db, post, comment) == (13, 6)


async def test_a_comment_deleted_twice_at_once_counts_down_once(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    user_headers: Headers,
    make_comment: MakeComment,
) -> None:
    for _ in range(5):
        comment = await make_comment(user, post)
        responses: list[Response] = []

        async def delete(target: Comment = comment, out: list[Response] = responses) -> None:
            out.append(await client.delete(f"{API}/comments/{target.id}", headers=user_headers))

        async with anyio.create_task_group() as group:
            group.start_soon(delete)
            group.start_soon(delete)

        assert sorted(response.status_code for response in responses) == [204, 404]
        assert await _counts(db, post) == (0, None)


async def test_a_reply_that_cannot_be_finished_leaves_no_trace(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    user_headers: Headers,
    make_comment: MakeComment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A reply is three writes: the post's count, the comment's count, the reply
    itself. Here the second one is made to fail, as it does when the comment
    is deleted at that very moment. The first must then be undone, which is
    what the transaction is for.
    """
    comment = await make_comment(user, post)

    async def refuse(self: CommentRepository, *args: object, **kwargs: object) -> bool:
        return False

    monkeypatch.setattr(CommentRepository, "add_reply", refuse)

    response = await client.post(
        f"{API}/comments/{comment.id}/replies", json={"content": "Hi"}, headers=user_headers
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "This comment was deleted"}
    assert await db.comments.count_documents({}) == 1
    assert await _counts(db, post, comment) == (1, 0)


async def test_a_comment_with_replies_deleted_twice_at_once_counts_down_once(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    author: User,
    user_headers: Headers,
    make_comment: MakeComment,
) -> None:
    """The same race, for the kind of comment that is kept as a placeholder."""
    for _ in range(5):
        comment = await make_comment(user, post)
        await make_comment(author, post, "A reply", reply_to=comment)
        before, _ = await _counts(db, post)
        responses: list[Response] = []

        async def delete(target: Comment = comment, out: list[Response] = responses) -> None:
            out.append(await client.delete(f"{API}/comments/{target.id}", headers=user_headers))

        async with anyio.create_task_group() as group:
            group.start_soon(delete)
            group.start_soon(delete)

        assert sorted(response.status_code for response in responses) == [204, 404]
        assert await _counts(db, post, comment) == (before - 1, 1)


# --- deleting a post ------------------------------------------------------------


async def test_deleting_a_post_deletes_its_comments(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    author: User,
    make_post: MakePost,
    make_comment: MakeComment,
    headers_for: HeadersFor,
) -> None:
    comment = await make_comment(user, post)
    await make_comment(author, post, "A reply", reply_to=comment)
    other_post = await make_post(author, "Another post", published=True)
    await make_comment(user, other_post, "Elsewhere")

    response = await client.delete(f"{API}/posts/{post.id}", headers=headers_for(author))

    assert response.status_code == 204
    assert await db.comments.count_documents({"post_id": post.id}) == 0
    assert await db.comments.count_documents({}) == 1  # the other post's comment is untouched
    assert (await client.get(f"{API}/comments/{comment.id}/replies")).status_code == 404


# --- how the lists are read and described ---------------------------------------


async def test_the_api_docs_show_which_comment_routes_need_a_login(app: FastAPI) -> None:
    paths = app.openapi()["paths"]
    optional = [{"OAuth2PasswordBearer": []}, {}]
    required = [{"OAuth2PasswordBearer": []}]

    assert paths[f"{API}/posts/{{post_id}}/comments"]["get"]["security"] == optional
    assert paths[f"{API}/comments/{{comment_id}}/replies"]["get"]["security"] == optional
    assert paths[f"{API}/posts/{{post_id}}/comments"]["post"]["security"] == required
    assert paths[f"{API}/comments/{{comment_id}}"]["delete"]["security"] == required


# --- more states a comment can be in --------------------------------------------


@pytest.mark.parametrize(("comment_id", "status"), [(MISSING, 404), ("not-an-id", 422)])
async def test_edit_a_comment_that_does_not_exist(
    client: AsyncClient, user_headers: Headers, comment_id: str, status: int
) -> None:
    response = await client.patch(
        f"{API}/comments/{comment_id}", json={"content": "x"}, headers=user_headers
    )

    assert response.status_code == status


async def test_edit_a_comment_with_invalid_input(
    client: AsyncClient, post: Post, user: User, user_headers: Headers, make_comment: MakeComment
) -> None:
    comment = await make_comment(user, post)

    for body in ({}, {"content": "\u200b"}, {"content": "ok", "is_deleted": True}):
        response = await client.patch(
            f"{API}/comments/{comment.id}", json=body, headers=user_headers
        )
        assert response.status_code == 422, body


async def test_a_deleted_comment_looks_exactly_like_one_that_never_was(
    client: AsyncClient,
    post: Post,
    user: User,
    user_headers: Headers,
    make_comment: MakeComment,
) -> None:
    """
    The attack: hold on to a comment's id and ask about it after it is gone.
    Any answer other than "not found" says that something used to be there.
    """
    alone = await make_comment(user, post, "Alone")
    parent = await make_comment(user, post, "Parent")
    reply = await make_comment(user, post, "Reply", reply_to=parent)
    await client.delete(f"{API}/comments/{alone.id}", headers=user_headers)
    await client.delete(f"{API}/comments/{reply.id}", headers=user_headers)

    for gone in (alone.id, reply.id, MISSING):
        answers = [
            await client.get(f"{API}/comments/{gone}/replies"),
            await client.post(
                f"{API}/comments/{gone}/replies", json={"content": "Hi"}, headers=user_headers
            ),
            await client.patch(
                f"{API}/comments/{gone}", json={"content": "Hi"}, headers=user_headers
            ),
            await client.delete(f"{API}/comments/{gone}", headers=user_headers),
        ]
        assert [(answer.status_code, answer.json()) for answer in answers] == [
            (404, NO_COMMENT)
        ] * 4, gone


async def test_your_own_comment_needs_the_own_permissions(
    client: AsyncClient,
    db: Database,
    post: Post,
    make_role: MakeRole,
    make_user: MakeUser,
    make_comment: MakeComment,
    headers_for: HeadersFor,
) -> None:
    """Being the author isn't enough: the role must allow editing and deleting."""
    await make_role("write_only", Perm.COMMENTS_CREATE)
    writer = await make_user("wren", "write_only")
    comment = await make_comment(writer, post, "Final")

    edit = await client.patch(
        f"{API}/comments/{comment.id}", json={"content": "Changed"}, headers=headers_for(writer)
    )
    delete = await client.delete(f"{API}/comments/{comment.id}", headers=headers_for(writer))

    assert (edit.status_code, delete.status_code) == (403, 403)
    assert await db.comments.count_documents({"content": "Final"}) == 1


async def test_you_can_take_back_your_words_even_when_the_post_is_hidden(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    user_headers: Headers,
    make_comment: MakeComment,
) -> None:
    """
    Otherwise pulling a post would freeze everyone's comments under it, and
    put them back unchanged if the post returned. Deleting only: editing a
    comment nobody can see has no use.
    """
    comment = await make_comment(user, post, "Regrettable")
    await PostRepository(db).set_published(post.id, published=False)

    edit = await client.patch(
        f"{API}/comments/{comment.id}", json={"content": "Edited"}, headers=user_headers
    )
    delete = await client.delete(f"{API}/comments/{comment.id}", headers=user_headers)

    assert (edit.status_code, delete.status_code) == (404, 204)
    assert await db.comments.count_documents({}) == 0
    assert await _counts(db, post) == (0, None)


async def test_a_strangers_writes_on_a_hidden_post_find_nothing(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    make_role: MakeRole,
    make_user: MakeUser,
    make_comment: MakeComment,
    headers_for: HeadersFor,
) -> None:
    """Not even a moderator, who could delete it if they could see it."""
    comment = await make_comment(user, post, "Under a pulled post")
    await make_role("moderator", Perm.COMMENTS_CREATE, Perm.COMMENTS_DELETE_ANY)
    moderator = headers_for(await make_user("mod", "moderator"))
    await PostRepository(db).set_published(post.id, published=False)

    answers = [
        await client.post(
            f"{API}/comments/{comment.id}/replies", json={"content": "Hi"}, headers=moderator
        ),
        await client.patch(
            f"{API}/comments/{comment.id}", json={"content": "Hi"}, headers=moderator
        ),
        await client.delete(f"{API}/comments/{comment.id}", headers=moderator),
    ]

    assert [(answer.status_code, answer.json()) for answer in answers] == [(404, NO_COMMENT)] * 3
    assert await db.comments.count_documents({"content": "Under a pulled post"}) == 1


async def test_a_comment_with_nothing_to_see_is_refused(
    client: AsyncClient, db: Database, post: Post, user_headers: Headers
) -> None:
    """A zero-width space is one character and shows as none."""
    response = await client.post(
        f"{API}/posts/{post.id}/comments", json={"content": "\u200b"}, headers=user_headers
    )

    assert response.status_code == 422
    assert await db.comments.count_documents({}) == 0


# --- a post that changes while a comment is on its way ---------------------------


async def _when_the_count_is_written(
    monkeypatch: pytest.MonkeyPatch, db: Database, post: Post, change: dict[str, object] | None
) -> None:
    """Make the post change, or vanish, between being checked and being written to."""
    real = PostRepository.add_comment

    async def meddle(self: PostRepository, post_id: ObjectId, **kwargs: object) -> bool:
        if change is None:
            await db.posts.delete_one({"_id": post.id})
        else:
            await db.posts.update_one({"_id": post.id}, {"$set": change})
        return await real(self, post_id, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(PostRepository, "add_comment", meddle)


async def test_a_post_deleted_at_the_last_moment_is_not_found(
    client: AsyncClient,
    db: Database,
    post: Post,
    user_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _when_the_count_is_written(monkeypatch, db, post, None)

    response = await client.post(
        f"{API}/posts/{post.id}/comments", json={"content": "Hi"}, headers=user_headers
    )

    assert (response.status_code, response.json()) == (404, NO_POST)
    assert await db.comments.count_documents({}) == 0


async def test_a_post_pulled_at_the_last_moment_takes_no_comment(
    client: AsyncClient,
    db: Database,
    post: Post,
    author: User,
    user_headers: Headers,
    headers_for: HeadersFor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _when_the_count_is_written(monkeypatch, db, post, {"status": "draft"})

    by_a_reader = await client.post(
        f"{API}/posts/{post.id}/comments", json={"content": "Hi"}, headers=user_headers
    )
    by_its_author = await client.post(
        f"{API}/posts/{post.id}/comments", json={"content": "Hi"}, headers=headers_for(author)
    )

    # To the reader the post has just stopped existing; its author still sees it.
    assert (by_a_reader.status_code, by_a_reader.json()) == (404, NO_POST)
    assert by_its_author.status_code == 409
    assert await db.comments.count_documents({}) == 0


# --- all or nothing --------------------------------------------------------------
# Each of these makes one step of a transaction fail after an earlier step has
# been written, and checks that the earlier step was undone.


def _failing(*args: object, **kwargs: object) -> None:
    raise RuntimeError("the database is having a moment")


async def test_a_comment_that_cannot_be_stored_is_not_counted(
    client: AsyncClient,
    db: Database,
    post: Post,
    user_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(CommentRepository, "create", _failing)

    with pytest.raises(RuntimeError):
        await client.post(
            f"{API}/posts/{post.id}/comments", json={"content": "Hi"}, headers=user_headers
        )

    assert await _counts(db, post) == (0, None)


async def test_a_delete_that_cannot_finish_deletes_nothing(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    user_headers: Headers,
    make_comment: MakeComment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comment = await make_comment(user, post, "Still here")
    monkeypatch.setattr(PostRepository, "remove_comment", _failing)

    with pytest.raises(RuntimeError):
        await client.delete(f"{API}/comments/{comment.id}", headers=user_headers)

    assert await db.comments.count_documents({"content": "Still here"}) == 1
    assert await _counts(db, post) == (1, None)


async def test_a_post_whose_comments_cannot_be_removed_is_not_deleted(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    author: User,
    make_comment: MakeComment,
    headers_for: HeadersFor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await make_comment(user, post)
    monkeypatch.setattr(CommentRepository, "delete_for_post", _failing)

    with pytest.raises(RuntimeError):
        await client.delete(f"{API}/posts/{post.id}", headers=headers_for(author))

    assert await db.posts.count_documents({"_id": post.id}) == 1
    assert await db.comments.count_documents({}) == 1


# --- how the lists are read ------------------------------------------------------


async def test_comments_written_at_the_same_instant_are_ordered_by_id(
    client: AsyncClient, db: Database, post: Post, user: User
) -> None:
    """Stored in the wrong order on purpose, so that only the sort can put them right."""
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    ids = sorted(ObjectId() for _ in range(4))
    await db.comments.insert_many(
        [
            {
                "_id": comment_id,
                "post_id": post.id,
                "author_id": user.id,
                "parent_id": None,
                "content": f"Comment {ids.index(comment_id)}",
                "created_at": instant,
                "updated_at": instant,
            }
            for comment_id in reversed(ids)
        ]
    )

    first = (await client.get(f"{API}/posts/{post.id}/comments", params={"limit": "3"})).json()
    second = (
        await client.get(
            f"{API}/posts/{post.id}/comments",
            params={"limit": "3", "cursor": first["next_cursor"]},
        )
    ).json()

    contents = [item["content"] for item in first["items"] + second["items"]]
    assert contents == ["Comment 0", "Comment 1", "Comment 2", "Comment 3"]


@pytest.mark.parametrize("thread", ["top level", "replies"])
async def test_a_list_reads_only_what_it_returns(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    user_headers: Headers,
    make_comment: MakeComment,
    thread: str,
) -> None:
    """
    The attack: post and delete comments in a loop. When deleting kept every
    row, a list read all of them to find the few still showing: 5,021 rows
    read to return 21. Now a deleted comment with nothing under it is gone,
    and the list is read straight off its index.
    """
    parent = await make_comment(user, post, "The thread")
    target = parent if thread == "replies" else None
    made = [await make_comment(user, post, f"Comment {n}", reply_to=target) for n in range(12)]
    for comment in made[:9]:
        await client.delete(f"{API}/comments/{comment.id}", headers=user_headers)

    query = (
        comments_repository.replies_query(parent)
        if thread == "replies"
        else comments_repository.top_level_query(post.id)
    )
    explained = await db.command(
        "explain",
        {"find": "comments", "filter": query, "sort": {"created_at": 1, "_id": 1}, "limit": 21},
        verbosity="executionStats",
    )

    stats = explained["executionStats"]
    stages = plan_stages(explained["queryPlanner"]["winningPlan"])
    assert "IXSCAN" in stages
    assert "SORT" not in stages, stages
    assert stats["totalDocsExamined"] == stats["nReturned"]
    assert stats["nReturned"] == (3 if thread == "replies" else 4)
