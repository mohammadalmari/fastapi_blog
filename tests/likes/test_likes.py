"""
Likes on posts and on comments. Most tests run once for each, through the
`target` fixture: the two are meant to behave the same, and running the same
test on both is what proves it.
"""

from dataclasses import dataclass

import anyio
import pytest
from bson import ObjectId
from httpx import AsyncClient, Response

from app.comments.models import Comment
from app.comments.repository import CommentRepository
from app.core.database import Database
from app.likes import repository as likes_repository
from app.likes.repository import LikeRepository, LikeTarget
from app.posts.models import Post
from app.posts.repository import PostRepository
from app.rbac.permissions import Perm
from app.users.models import User
from tests.conftest import HeadersFor, MakeComment, MakePost, MakeRole, MakeUser
from tests.helpers import API, plan_stages

pytestmark = pytest.mark.anyio

Headers = dict[str, str]
MISSING = "5f0000000000000000000000"
NOT_FOUND = {"post": {"detail": "Post not found"}, "comment": {"detail": "Comment not found"}}
PLURAL = {"post": "posts", "comment": "comments"}


@dataclass(frozen=True)
class Target:
    """Something that can be liked, and where to look to see how it stands."""

    kind: LikeTarget
    id: ObjectId
    post: Post

    @property
    def path(self) -> str:
        return f"{API}/{PLURAL[self.kind]}/{self.id}/like"

    @property
    def collection(self) -> str:
        return PLURAL[self.kind]

    @property
    def repository(self) -> type[PostRepository] | type[CommentRepository]:
        return PostRepository if self.kind == "post" else CommentRepository


@pytest.fixture
async def comment(user: User, post: Post, make_comment: MakeComment) -> Comment:
    return await make_comment(user, post, "A comment to like")


@pytest.fixture(params=["post", "comment"])
def target(request: pytest.FixtureRequest, post: Post, comment: Comment) -> Target:
    if request.param == "post":
        return Target("post", post.id, post)
    return Target("comment", comment.id, post)


async def _standing(db: Database, target: Target) -> tuple[int | None, int]:
    """(the stored like count, the number of stored likes); the count is None if it is gone."""
    stored = await db[target.collection].find_one({"_id": target.id})
    likes = await db.likes.count_documents({"target_id": target.id})
    return (int(stored["like_count"]) if stored else None), likes


async def _liked_by_me(client: AsyncClient, target: Target, headers: Headers | None) -> bool:
    """What the API tells this caller about whether they like it."""
    if target.kind == "post":
        response = await client.get(f"{API}/posts/{target.post.slug}", headers=headers)
        return bool(response.json()["liked_by_me"])
    response = await client.get(f"{API}/posts/{target.post.id}/comments", headers=headers)
    (item,) = [item for item in response.json()["items"] if item["id"] == str(target.id)]
    return bool(item["liked_by_me"])


def _varies_by(response: Response) -> set[str]:
    """The request headers a cache must take into account, as `Vary` lists them."""
    return {name.strip().lower() for name in response.headers.get("vary", "").split(",")}


def _failing(*args: object, **kwargs: object) -> None:
    raise RuntimeError("the database is having a moment")


# --- who may ------------------------------------------------------------------

ROUTES = [
    ("PUT", "post"),
    ("DELETE", "post"),
    ("PUT", "comment"),
    ("DELETE", "comment"),
]


@pytest.mark.parametrize(("method", "kind"), ROUTES)
async def test_route_requires_a_login(client: AsyncClient, method: str, kind: str) -> None:
    response = await client.request(method, f"{API}/{PLURAL[kind]}/{MISSING}/like")

    assert response.status_code == 401


async def test_liking_needs_the_permission(
    client: AsyncClient,
    db: Database,
    target: Target,
    make_role: MakeRole,
    make_user: MakeUser,
    headers_for: HeadersFor,
) -> None:
    await make_role("commenter", Perm.COMMENTS_CREATE)
    commenter = await make_user("sam", "commenter")

    response = await client.put(target.path, headers=headers_for(commenter))

    assert response.status_code == 403
    assert await _standing(db, target) == (0, 0)


async def test_taking_a_like_back_needs_no_permission(
    client: AsyncClient, db: Database, target: Target, user: User, user_headers: Headers
) -> None:
    """Losing the right to like must not trap the likes already given."""
    await client.put(target.path, headers=user_headers)
    await db.users.update_one({"_id": user.id}, {"$set": {"role_ids": []}})

    response = await client.delete(target.path, headers=user_headers)

    assert response.status_code == 204
    assert await _standing(db, target) == (0, 0)


# --- PUT .../like ---------------------------------------------------------------


async def test_a_like_is_stored_counted_and_shown_to_its_giver_alone(
    client: AsyncClient,
    db: Database,
    target: Target,
    user: User,
    user_headers: Headers,
    author_headers: Headers,
) -> None:
    response = await client.put(target.path, headers=user_headers)

    assert (response.status_code, response.content) == (204, b"")
    assert await _standing(db, target) == (1, 1)
    stored = await db.likes.find_one({})
    assert stored is not None
    assert {key: stored[key] for key in ("user_id", "target_type", "target_id", "post_id")} == {
        "user_id": user.id,
        "target_type": target.kind,
        "target_id": target.id,
        "post_id": target.post.id,
    }
    assert stored["created_at"] is not None
    # It is the liker's like, and nobody else's.
    assert await _liked_by_me(client, target, user_headers) is True
    assert await _liked_by_me(client, target, author_headers) is False
    assert await _liked_by_me(client, target, None) is False


async def test_liking_twice_is_liking_once(
    client: AsyncClient, db: Database, target: Target, user_headers: Headers
) -> None:
    await client.put(target.path, headers=user_headers)

    again = await client.put(target.path, headers=user_headers)

    assert again.status_code == 204
    assert await _standing(db, target) == (1, 1)


async def test_the_same_like_sent_many_times_at_once_counts_once(
    client: AsyncClient, db: Database, target: Target, user_headers: Headers
) -> None:
    """The attack: fire the request in parallel to be counted more than once."""
    responses: list[Response] = []

    async def like() -> None:
        responses.append(await client.put(target.path, headers=user_headers))

    async with anyio.create_task_group() as group:
        for _ in range(6):
            group.start_soon(like)

    assert [response.status_code for response in responses] == [204] * 6
    assert await _standing(db, target) == (1, 1)


async def test_likes_from_many_people_at_once_are_all_counted(
    client: AsyncClient,
    db: Database,
    target: Target,
    make_user: MakeUser,
    headers_for: HeadersFor,
) -> None:
    fans = [await make_user(f"fan{number}", "reader") for number in range(8)]
    responses: list[Response] = []

    async def like(fan: User) -> None:
        responses.append(await client.put(target.path, headers=headers_for(fan)))

    async with anyio.create_task_group() as group:
        for fan in fans:
            group.start_soon(like, fan)

    assert [response.status_code for response in responses] == [204] * 8
    assert await _standing(db, target) == (8, 8)


@pytest.mark.parametrize("kind", ["post", "comment"])
async def test_liking_what_does_not_exist_is_not_found(
    client: AsyncClient, db: Database, user_headers: Headers, kind: str
) -> None:
    response = await client.put(f"{API}/{PLURAL[kind]}/{MISSING}/like", headers=user_headers)

    assert (response.status_code, response.json()) == (404, NOT_FOUND[kind])
    assert await db.likes.count_documents({}) == 0


# --- DELETE .../like ------------------------------------------------------------


async def test_taking_a_like_back_removes_your_own_and_no_other(
    client: AsyncClient,
    db: Database,
    target: Target,
    user_headers: Headers,
    author_headers: Headers,
) -> None:
    await client.put(target.path, headers=user_headers)
    await client.put(target.path, headers=author_headers)

    response = await client.delete(target.path, headers=user_headers)

    assert (response.status_code, response.content) == (204, b"")
    assert await _standing(db, target) == (1, 1)
    assert await _liked_by_me(client, target, user_headers) is False
    # Only the caller's own like went.
    assert await _liked_by_me(client, target, author_headers) is True


async def test_taking_back_a_like_never_given_changes_nothing(
    client: AsyncClient,
    db: Database,
    target: Target,
    user_headers: Headers,
    author_headers: Headers,
) -> None:
    await client.put(target.path, headers=author_headers)

    response = await client.delete(target.path, headers=user_headers)

    assert response.status_code == 204
    assert await _standing(db, target) == (1, 1)


async def test_a_like_taken_back_three_times_at_once_counts_down_once(
    client: AsyncClient,
    db: Database,
    target: Target,
    user_headers: Headers,
    author_headers: Headers,
) -> None:
    await client.put(target.path, headers=author_headers)
    await client.put(target.path, headers=user_headers)
    responses: list[Response] = []

    async def unlike() -> None:
        responses.append(await client.delete(target.path, headers=user_headers))

    async with anyio.create_task_group() as group:
        for _ in range(3):
            group.start_soon(unlike)

    assert [response.status_code for response in responses] == [204] * 3
    # The author's like is still there, and still counted.
    assert await _standing(db, target) == (1, 1)


# --- what is hidden stays hidden ------------------------------------------------


async def test_a_like_on_what_you_cannot_see_finds_nothing(
    client: AsyncClient,
    db: Database,
    target: Target,
    make_user: MakeUser,
    headers_for: HeadersFor,
) -> None:
    """
    The attack: like ids until one answers differently, to learn that a draft
    exists. A hidden post, and a comment under one, answer exactly as an id
    that was never used does.
    """
    await db.posts.update_one({"_id": target.post.id}, {"$set": {"status": "draft"}})
    stranger = headers_for(await make_user("sam", "reader"))

    response = await client.put(target.path, headers=stranger)

    assert (response.status_code, response.json()) == (404, NOT_FOUND[target.kind])
    assert await _standing(db, target) == (0, 0)


async def test_whoever_can_see_a_draft_can_like_it(
    client: AsyncClient, db: Database, target: Target, author_headers: Headers
) -> None:
    await db.posts.update_one({"_id": target.post.id}, {"$set": {"status": "draft"}})

    response = await client.put(target.path, headers=author_headers)

    assert response.status_code == 204
    assert await _standing(db, target) == (1, 1)


async def test_your_like_can_be_taken_back_after_the_post_is_hidden(
    client: AsyncClient, db: Database, target: Target, user_headers: Headers
) -> None:
    await client.put(target.path, headers=user_headers)
    await db.posts.update_one({"_id": target.post.id}, {"$set": {"status": "draft"}})

    response = await client.delete(target.path, headers=user_headers)

    assert response.status_code == 204
    assert await _standing(db, target) == (0, 0)


async def test_a_deleted_comment_cannot_be_liked(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    user_headers: Headers,
    author_headers: Headers,
    make_comment: MakeComment,
) -> None:
    """A placeholder still has a row; it is "not found" all the same."""
    placeholder = await make_comment(user, post, "Soon gone")
    await make_comment(user, post, "A reply", reply_to=placeholder)
    await client.delete(f"{API}/comments/{placeholder.id}", headers=user_headers)

    response = await client.put(f"{API}/comments/{placeholder.id}/like", headers=author_headers)

    assert (response.status_code, response.json()) == (404, NOT_FOUND["comment"])
    assert await db.likes.count_documents({}) == 0


async def test_a_post_pulled_at_the_last_moment_takes_no_like_from_a_stranger(
    client: AsyncClient,
    db: Database,
    target: Target,
    user_headers: Headers,
    author_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The post is unpublished after the like was let in and before it is
    written. "You may see it" has to hold when the like is stored, not only
    when the request arrived.
    """
    real = LikeRepository.add

    async def pull_first(self: LikeRepository, **kwargs: object) -> None:
        await db.posts.update_one({"_id": target.post.id}, {"$set": {"status": "draft"}})
        await real(self, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(LikeRepository, "add", pull_first)

    by_a_reader = await client.put(target.path, headers=user_headers)
    reader_left = await _standing(db, target)
    by_its_author = await client.put(target.path, headers=author_headers)

    # To the reader it has just stopped existing; its author still sees it.
    assert (by_a_reader.status_code, by_a_reader.json()) == (404, NOT_FOUND[target.kind])
    assert reader_left == (0, 0)
    assert by_its_author.status_code == 204
    assert await _standing(db, target) == (1, 1)


async def test_taking_a_like_back_never_says_what_became_of_the_thing(
    client: AsyncClient,
    db: Database,
    post: Post,
    author: User,
    user_headers: Headers,
    author_headers: Headers,
    make_post: MakePost,
) -> None:
    """
    The attack: like things, then unlike them later and compare the answers,
    to learn which were pulled and which deleted. Every unlike answers alike:
    liked and hidden, liked and deleted, never liked, never existed.
    """
    doomed = await make_post(author, "Soon deleted", published=True)
    never_liked = await make_post(author, "Never liked", published=True)
    for liked in (post, doomed):
        await client.put(f"{API}/posts/{liked.id}/like", headers=user_headers)
    await client.post(f"{API}/posts/{post.id}/unpublish", headers=author_headers)
    await client.delete(f"{API}/posts/{doomed.id}", headers=author_headers)

    answers = [
        await client.delete(f"{API}/posts/{post_id}/like", headers=user_headers)
        for post_id in (post.id, doomed.id, never_liked.id, MISSING)
    ]

    assert [(answer.status_code, answer.content) for answer in answers] == [(204, b"")] * 4
    assert await db.likes.count_documents({}) == 0


# --- liked_by_me ----------------------------------------------------------------


async def test_the_lists_of_posts_say_which_you_liked(
    client: AsyncClient,
    post: Post,
    author: User,
    author_headers: Headers,
    user_headers: Headers,
    make_post: MakePost,
) -> None:
    await make_post(author, "Another post", published=True)
    await client.put(f"{API}/posts/{post.id}/like", headers=author_headers)

    def liked(response: Response) -> dict[str, bool]:
        return {item["title"]: item["liked_by_me"] for item in response.json()["items"]}

    feed = await client.get(f"{API}/posts", headers=author_headers)
    own_posts = await client.get(f"{API}/users/me/posts", headers=author_headers)
    feed_of_another = await client.get(f"{API}/posts", headers=user_headers)
    feed_of_a_visitor = await client.get(f"{API}/posts")

    expected = {"A post to discuss": True, "Another post": False}
    nobody = dict.fromkeys(expected, False)
    assert liked(feed) == expected
    assert liked(own_posts) == expected
    assert liked(feed_of_another) == nobody
    assert liked(feed_of_a_visitor) == nobody


async def test_a_post_you_change_still_says_you_like_it(
    client: AsyncClient, post: Post, author_headers: Headers
) -> None:
    await client.put(f"{API}/posts/{post.id}/like", headers=author_headers)

    edited = await client.patch(
        f"{API}/posts/{post.id}", json={"title": "Renamed"}, headers=author_headers
    )
    pulled = await client.post(f"{API}/posts/{post.id}/unpublish", headers=author_headers)

    assert (edited.json()["liked_by_me"], edited.json()["like_count"]) == (True, 1)
    assert (pulled.json()["liked_by_me"], pulled.json()["like_count"]) == (True, 1)


async def test_replies_and_edited_comments_say_whether_you_liked_them(
    client: AsyncClient,
    post: Post,
    user: User,
    user_headers: Headers,
    author_headers: Headers,
    comment: Comment,
    make_comment: MakeComment,
) -> None:
    reply = await make_comment(user, post, "A reply", reply_to=comment)
    await make_comment(user, post, "Another reply", reply_to=comment)
    await client.put(f"{API}/comments/{reply.id}/like", headers=user_headers)

    mine = await client.get(f"{API}/comments/{comment.id}/replies", headers=user_headers)
    theirs = await client.get(f"{API}/comments/{comment.id}/replies", headers=author_headers)
    edited = await client.patch(
        f"{API}/comments/{reply.id}", json={"content": "A better reply"}, headers=user_headers
    )

    assert [item["liked_by_me"] for item in mine.json()["items"]] == [True, False]
    assert [item["liked_by_me"] for item in theirs.json()["items"]] == [False, False]
    assert (edited.json()["liked_by_me"], edited.json()["like_count"]) == (True, 1)


PERSONAL_LISTS = ["/posts", "/posts/{post_id}/comments", "/comments/{comment_id}/replies"]


@pytest.mark.parametrize("path", PERSONAL_LISTS)
async def test_a_public_list_read_with_a_login_is_kept_from_shared_caches(
    client: AsyncClient, post: Post, comment: Comment, user_headers: Headers, path: str
) -> None:
    """
    Each now says what the reader liked. A shared cache must neither keep
    that, nor hand a logged-in reader the copy it kept for visitors.
    """
    url = API + path.format(post_id=post.id, comment_id=comment.id)

    as_a_visitor = await client.get(url)
    logged_in = await client.get(url, headers=user_headers)

    assert "cache-control" not in as_a_visitor.headers
    assert "authorization" in _varies_by(as_a_visitor)
    assert logged_in.headers["cache-control"] == "private, no-store"
    assert "authorization" in _varies_by(logged_in)


async def test_your_own_list_of_posts_is_never_cached(
    client: AsyncClient, author_headers: Headers
) -> None:
    response = await client.get(f"{API}/users/me/posts", headers=author_headers)

    assert response.headers["cache-control"] == "private, no-store"


async def test_a_bad_token_on_the_feed_is_refused_not_ignored(client: AsyncClient) -> None:
    """Answering as if for a visitor would hide from a client that its login had lapsed."""
    response = await client.get(f"{API}/posts", headers={"Authorization": "Bearer nope"})

    assert response.status_code == 401


# --- what goes when the thing liked goes ----------------------------------------


async def test_deleting_a_comment_deletes_its_likes_and_no_others(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    user_headers: Headers,
    author_headers: Headers,
    comment: Comment,
    make_comment: MakeComment,
) -> None:
    other = await make_comment(user, post, "Another comment")
    for liked in (post, comment, other):
        path = "posts" if liked is post else "comments"
        for headers in (user_headers, author_headers):
            await client.put(f"{API}/{path}/{liked.id}/like", headers=headers)

    response = await client.delete(f"{API}/comments/{comment.id}", headers=user_headers)

    assert response.status_code == 204
    assert await db.likes.count_documents({"target_id": comment.id}) == 0
    assert await db.likes.count_documents({"target_id": other.id}) == 2
    assert await db.likes.count_documents({"target_id": post.id}) == 2


async def test_a_placeholder_keeps_no_likes(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    user_headers: Headers,
    author_headers: Headers,
    comment: Comment,
    make_comment: MakeComment,
) -> None:
    reply = await make_comment(user, post, "A reply", reply_to=comment)
    await client.put(f"{API}/comments/{comment.id}/like", headers=author_headers)
    await client.put(f"{API}/comments/{reply.id}/like", headers=author_headers)

    await client.delete(f"{API}/comments/{comment.id}", headers=user_headers)

    stored = await db.comments.find_one({"_id": comment.id})
    assert stored is not None
    assert (stored["is_deleted"], stored["like_count"]) == (True, 0)
    assert await db.likes.count_documents({"target_id": comment.id}) == 0
    # The reply under it is untouched.
    assert await db.likes.count_documents({"target_id": reply.id}) == 1


async def test_deleting_a_post_deletes_every_like_under_it(
    client: AsyncClient,
    db: Database,
    post: Post,
    author: User,
    user: User,
    user_headers: Headers,
    author_headers: Headers,
    comment: Comment,
    make_post: MakePost,
    make_comment: MakeComment,
) -> None:
    reply = await make_comment(user, post, "A reply", reply_to=comment)
    other_post = await make_post(author, "Another post", published=True)
    other_comment = await make_comment(user, other_post)
    await client.put(f"{API}/posts/{post.id}/like", headers=user_headers)
    await client.put(f"{API}/posts/{other_post.id}/like", headers=user_headers)
    for liked in (comment, reply, other_comment):
        await client.put(f"{API}/comments/{liked.id}/like", headers=user_headers)

    response = await client.delete(f"{API}/posts/{post.id}", headers=author_headers)

    assert response.status_code == 204
    left = {like["target_id"] async for like in db.likes.find({})}
    assert left == {other_post.id, other_comment.id}


# --- all or nothing --------------------------------------------------------------
# Each of these makes one step of a transaction fail, or find its target gone,
# after an earlier step has been written, and checks that step was undone.


async def test_something_deleted_at_the_last_moment_is_not_found(
    client: AsyncClient,
    db: Database,
    target: Target,
    user_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = target.repository.add_like

    async def vanish_first(self: object, target_id: ObjectId, **kwargs: object) -> bool:
        await db[target.collection].delete_one({"_id": target.id})
        return await real(self, target_id, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(target.repository, "add_like", vanish_first)

    response = await client.put(target.path, headers=user_headers)

    assert (response.status_code, response.json()) == (404, NOT_FOUND[target.kind])
    assert await _standing(db, target) == (None, 0)


async def test_a_comment_deleted_at_the_last_moment_takes_no_like(
    client: AsyncClient,
    db: Database,
    post: Post,
    user: User,
    user_headers: Headers,
    author_headers: Headers,
    comment: Comment,
    make_comment: MakeComment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleted but kept for its replies: the row is still there, and takes nothing."""
    await make_comment(user, post, "A reply", reply_to=comment)
    real = CommentRepository.add_like

    async def delete_first(self: CommentRepository, comment_id: ObjectId, **kwargs: object) -> bool:
        await client.delete(f"{API}/comments/{comment.id}", headers=user_headers)
        return await real(self, comment_id, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(CommentRepository, "add_like", delete_first)

    response = await client.put(f"{API}/comments/{comment.id}/like", headers=author_headers)

    assert (response.status_code, response.json()) == (404, NOT_FOUND["comment"])
    stored = await db.comments.find_one({"_id": comment.id})
    assert stored is not None
    assert (stored["is_deleted"], stored["like_count"]) == (True, 0)
    assert await db.likes.count_documents({}) == 0


async def test_a_like_that_cannot_be_counted_is_not_stored(
    client: AsyncClient,
    db: Database,
    target: Target,
    user_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(target.repository, "add_like", _failing)

    with pytest.raises(RuntimeError):
        await client.put(target.path, headers=user_headers)

    assert await _standing(db, target) == (0, 0)


async def test_a_like_that_cannot_be_uncounted_stays(
    client: AsyncClient,
    db: Database,
    target: Target,
    user_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await client.put(target.path, headers=user_headers)
    monkeypatch.setattr(target.repository, "remove_like", _failing)

    with pytest.raises(RuntimeError):
        await client.delete(target.path, headers=user_headers)

    assert await _standing(db, target) == (1, 1)


@pytest.mark.parametrize(
    ("cleanup", "path"), [("delete_for_post", "posts"), ("delete_for_comment", "comments")]
)
async def test_what_cannot_be_cleaned_up_after_is_not_deleted(
    client: AsyncClient,
    db: Database,
    post: Post,
    comment: Comment,
    admin_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
    cleanup: str,
    path: str,
) -> None:
    monkeypatch.setattr(LikeRepository, cleanup, _failing)
    doomed = post if path == "posts" else comment

    with pytest.raises(RuntimeError):
        await client.delete(f"{API}/{path}/{doomed.id}", headers=admin_headers)

    assert await db[path].count_documents({"_id": doomed.id}) == 1
    stored_post = await db.posts.find_one({"_id": post.id})
    assert stored_post is not None
    assert stored_post["comment_count"] == 1


# --- how the likes are read -------------------------------------------------------


@pytest.mark.parametrize("query", ["liked", "under_post", "on_comment"])
async def test_every_query_on_likes_is_read_off_an_index(
    client: AsyncClient,
    db: Database,
    post: Post,
    author: User,
    user: User,
    comment: Comment,
    make_post: MakePost,
    make_user: MakeUser,
    headers_for: HeadersFor,
    query: str,
) -> None:
    """Likes are the largest collection there will be; nothing may scan it."""
    other_post = await make_post(author, "Another post", published=True)
    for number in range(6):
        fan = headers_for(await make_user(f"fan{number}", "reader"))
        await client.put(f"{API}/posts/{post.id}/like", headers=fan)
        await client.put(f"{API}/posts/{other_post.id}/like", headers=fan)
        await client.put(f"{API}/comments/{comment.id}/like", headers=fan)
    fan0 = await db.users.find_one({"username": "fan0"})
    assert fan0 is not None
    filters = {
        "liked": likes_repository.liked_query(fan0["_id"], "post", [post.id, other_post.id]),
        "under_post": likes_repository.under_post_query(post.id),
        "on_comment": likes_repository.on_comment_query(comment),
    }
    expected = {"liked": 2, "under_post": 12, "on_comment": 6}

    explained = await db.command(
        "explain", {"find": "likes", "filter": filters[query]}, verbosity="executionStats"
    )

    stats = explained["executionStats"]
    stages = plan_stages(explained["queryPlanner"]["winningPlan"])
    assert "IXSCAN" in stages, stages
    assert stats["nReturned"] == expected[query]
    assert stats["totalDocsExamined"] == stats["nReturned"]
