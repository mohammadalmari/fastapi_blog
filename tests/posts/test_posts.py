"""
The posts routes. Each follows the same checklist: success, invalid input,
not logged in, not allowed, not found, conflict, and what changed in the
database. The pure rules (slugs, excerpts, field limits) are unit tests in
test_post_rules.py.
"""

import re
from datetime import timedelta

import pytest
from bson import ObjectId
from fastapi import FastAPI
from httpx import AsyncClient, Response
from pymongo.errors import DuplicateKeyError

from app.core.database import Database
from app.posts import repository as posts_repository
from app.posts.models import Post
from app.posts.repository import PostRepository
from app.rbac.permissions import Perm
from app.users.models import User
from tests.conftest import HeadersFor, MakePost, MakeRole, MakeUser
from tests.helpers import API, plan_stages

pytestmark = pytest.mark.anyio

Headers = dict[str, str]
MISSING = "5f0000000000000000000000"
FORBIDDEN = {"detail": "You don't have permission to do this"}
NOT_FOUND = {"detail": "Post not found"}
NEW_POST = {"title": "Hello World", "content": "Some **content**.", "tags": ["FastAPI"]}
# Every slug ends in six random characters, whatever else exists.
HELLO_WORLD_SLUG = re.compile(r"hello-world-[0-9a-f]{6}")


async def _feed_titles(client: AsyncClient, **params: str) -> list[str]:
    response = await client.get(f"{API}/posts", params=params)
    assert response.status_code == 200
    return [post["title"] for post in response.json()["items"]]


# --- every writing route refuses strangers and readers --------------------------

WRITES = [
    ("POST", "/posts"),
    ("PATCH", f"/posts/{MISSING}"),
    ("DELETE", f"/posts/{MISSING}"),
    ("POST", f"/posts/{MISSING}/publish"),
    ("POST", f"/posts/{MISSING}/unpublish"),
]


@pytest.mark.parametrize(("method", "path"), [*WRITES, ("GET", "/users/me/posts")])
async def test_route_requires_a_login(client: AsyncClient, method: str, path: str) -> None:
    response = await client.request(method, f"{API}{path}", json=NEW_POST)

    assert response.status_code == 401


async def test_a_reader_cannot_write_a_post(
    client: AsyncClient, db: Database, user_headers: Headers
) -> None:
    response = await client.post(f"{API}/posts", json=NEW_POST, headers=user_headers)

    assert response.status_code == 403
    assert response.json() == FORBIDDEN
    assert await db.posts.count_documents({}) == 0


# --- POST /posts ----------------------------------------------------------------


async def test_create_a_post(
    client: AsyncClient, db: Database, author: User, author_headers: Headers
) -> None:
    response = await client.post(f"{API}/posts", json=NEW_POST, headers=author_headers)

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Hello World"
    assert HELLO_WORLD_SLUG.fullmatch(body["slug"])
    assert body["content"] == "Some **content**."  # Markdown is stored as written
    assert body["excerpt"] == "Some content."
    assert body["tags"] == ["fastapi"]
    assert (body["status"], body["published_at"]) == ("draft", None)
    assert (body["like_count"], body["comment_count"]) == (0, 0)
    assert body["author"] == {"id": str(author.id), "username": "ali", "display_name": None}

    stored = await db.posts.find_one({"slug": body["slug"]})
    assert stored is not None
    assert str(stored["_id"]) == body["id"]
    assert stored["author_id"] == author.id
    assert stored["status"] == "draft"


async def test_create_a_post_with_its_own_excerpt(
    client: AsyncClient, author_headers: Headers
) -> None:
    response = await client.post(
        f"{API}/posts", json={**NEW_POST, "excerpt": "In short."}, headers=author_headers
    )

    assert response.json()["excerpt"] == "In short."


async def test_create_a_post_with_invalid_input(
    client: AsyncClient, db: Database, author_headers: Headers
) -> None:
    """That bad input is a 422 and creates nothing; test_post_rules.py lists the rules."""
    response = await client.post(
        f"{API}/posts", json={**NEW_POST, "status": "published"}, headers=author_headers
    )

    assert response.status_code == 422
    assert await db.posts.count_documents({}) == 0


async def test_two_posts_with_one_title_get_different_slugs(
    client: AsyncClient, author_headers: Headers
) -> None:
    first = await client.post(f"{API}/posts", json=NEW_POST, headers=author_headers)
    second = await client.post(f"{API}/posts", json=NEW_POST, headers=author_headers)

    assert (first.status_code, second.status_code) == (201, 201)
    assert HELLO_WORLD_SLUG.fullmatch(first.json()["slug"])
    assert HELLO_WORLD_SLUG.fullmatch(second.json()["slug"])
    assert second.json()["slug"] != first.json()["slug"]


async def test_a_slug_says_nothing_about_other_peoples_posts(
    client: AsyncClient,
    author: User,
    other_author: User,
    make_post: MakePost,
    headers_for: HeadersFor,
) -> None:
    """
    The attack: guess the title of someone's unpublished post and create one
    with the same title. When only a *taken* slug got a random ending, the
    ending gave the draft away. Now every slug has one.
    """
    await make_post(author, "Hello World")  # a draft the other author can't see

    guess = await client.post(f"{API}/posts", json=NEW_POST, headers=headers_for(other_author))
    fresh = await client.post(
        f"{API}/posts",
        json={**NEW_POST, "title": "Nobody wrote this"},
        headers=headers_for(other_author),
    )

    assert HELLO_WORLD_SLUG.fullmatch(guess.json()["slug"])
    assert re.fullmatch(r"nobody-wrote-this-[0-9a-f]{6}", fresh.json()["slug"])


async def test_running_out_of_slugs_is_a_conflict_not_a_crash(
    client: AsyncClient, db: Database, author_headers: Headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulated by making the random ending always the same."""
    monkeypatch.setattr(posts_repository.secrets, "token_hex", lambda size: "0" * (size * 2))
    await client.post(f"{API}/posts", json=NEW_POST, headers=author_headers)

    response = await client.post(f"{API}/posts", json=NEW_POST, headers=author_headers)

    assert response.status_code == 409
    assert await db.posts.count_documents({}) == 1


# --- GET /posts/{slug} ----------------------------------------------------------


async def test_anyone_can_read_a_published_post(
    client: AsyncClient, author: User, make_post: MakePost
) -> None:
    post = await make_post(author, "Out now", published=True)

    response = await client.get(f"{API}/posts/{post.slug}")

    assert response.status_code == 200
    body = response.json()
    assert (body["title"], body["status"]) == ("Out now", "published")
    assert body["published_at"] is not None
    assert body["author"]["username"] == "ali"
    assert "author_id" not in body


async def test_a_slug_in_another_alphabet_works(
    client: AsyncClient, author: User, make_post: MakePost
) -> None:
    post = await make_post(author, "مرحبا بالعالم", published=True)

    response = await client.get(f"{API}/posts/{post.slug}")

    assert post.slug.startswith("مرحبا-بالعالم-")
    assert response.status_code == 200


@pytest.mark.parametrize("reader", ["author", "editor"])
async def test_a_draft_is_readable_by_its_author_and_by_those_who_may_read_drafts(
    client: AsyncClient,
    author: User,
    editor: User,
    make_post: MakePost,
    headers_for: HeadersFor,
    reader: str,
) -> None:
    post = await make_post(author, "Not yet")
    who = author if reader == "author" else editor

    response = await client.get(f"{API}/posts/{post.slug}", headers=headers_for(who))

    assert response.status_code == 200
    assert response.json()["status"] == "draft"


@pytest.mark.parametrize("stranger", ["anonymous", "reader", "another author"])
async def test_a_draft_does_not_exist_for_anyone_else(
    client: AsyncClient,
    author: User,
    other_author: User,
    user: User,
    make_post: MakePost,
    headers_for: HeadersFor,
    stranger: str,
) -> None:
    """404, not 403: a refusal would confirm that the draft is there."""
    post = await make_post(author, "Not yet")
    headers = {
        "anonymous": {},
        "reader": headers_for(user),
        "another author": headers_for(other_author),
    }[stranger]

    response = await client.get(f"{API}/posts/{post.slug}", headers=headers)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


async def test_read_a_post_that_does_not_exist(client: AsyncClient) -> None:
    response = await client.get(f"{API}/posts/no-such-post")

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


async def test_a_bad_token_on_a_public_route_is_refused_not_ignored(
    client: AsyncClient, author: User, make_post: MakePost
) -> None:
    """So that a client with an expired token learns to refresh it."""
    post = await make_post(author, "Out now", published=True)

    response = await client.get(
        f"{API}/posts/{post.slug}", headers={"Authorization": "Bearer nonsense"}
    )

    assert response.status_code == 401


# --- PATCH /posts/{id} ----------------------------------------------------------


async def test_an_author_edits_their_post(
    client: AsyncClient, db: Database, author: User, author_headers: Headers, make_post: MakePost
) -> None:
    post = await make_post(author, "First title", tags=["old"])

    response = await client.patch(
        f"{API}/posts/{post.id}",
        json={"title": "Second title", "content": "New _words_.", "tags": ["New"]},
        headers=author_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["title"], body["content"], body["tags"]) == (
        "Second title",
        "New _words_.",
        ["new"],
    )
    assert body["excerpt"] == "New words."  # it was generated, so it follows the content
    assert body["slug"] == post.slug  # links to the post keep working
    stored = await db.posts.find_one({"_id": post.id})
    assert stored is not None
    assert stored["title"] == "Second title"


async def test_an_excerpt_the_author_wrote_survives_a_content_change(
    client: AsyncClient, author: User, author_headers: Headers
) -> None:
    created = await client.post(
        f"{API}/posts", json={**NEW_POST, "excerpt": "In short."}, headers=author_headers
    )

    response = await client.patch(
        f"{API}/posts/{created.json()['id']}",
        json={"content": "Rewritten."},
        headers=author_headers,
    )

    assert response.json()["excerpt"] == "In short."


async def test_a_change_that_changes_nothing_writes_nothing(
    client: AsyncClient,
    author: User,
    author_headers: Headers,
    make_post: MakePost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """So that "last edited" doesn't move when nothing was edited."""
    post = await make_post(author, "Same", tags=["one"])
    writes: list[object] = []
    real = PostRepository.update

    async def spy(self: PostRepository, *args: object, **kwargs: object) -> object:
        writes.append(args)
        return await real(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(PostRepository, "update", spy)

    empty = await client.patch(f"{API}/posts/{post.id}", json={}, headers=author_headers)
    same = await client.patch(
        f"{API}/posts/{post.id}", json={"title": "Same", "tags": ["one"]}, headers=author_headers
    )

    assert (empty.status_code, same.status_code) == (200, 200)
    assert writes == []


async def test_editing_moves_the_edit_date_and_publishing_does_not(
    client: AsyncClient,
    db: Database,
    author: User,
    author_headers: Headers,
    make_post: MakePost,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = await make_post(author)
    later = post.updated_at + timedelta(hours=1)
    monkeypatch.setattr(posts_repository, "utcnow", lambda: later)
    status_writes: list[bool] = []
    real = PostRepository.set_published

    async def spy(self: PostRepository, post_id: ObjectId, *, published: bool) -> Post | None:
        status_writes.append(published)
        return await real(self, post_id, published=published)

    monkeypatch.setattr(PostRepository, "set_published", spy)

    await client.post(f"{API}/posts/{post.id}/publish", headers=author_headers)
    await client.post(f"{API}/posts/{post.id}/publish", headers=author_headers)  # twice: no change
    await client.post(f"{API}/posts/{post.id}/unpublish", headers=author_headers)
    after_publishing = await db.posts.find_one({"_id": post.id})
    await client.patch(f"{API}/posts/{post.id}", json={"title": "Edited"}, headers=author_headers)
    after_editing = await db.posts.find_one({"_id": post.id})

    assert after_publishing is not None and after_editing is not None
    assert status_writes == [True, False]  # the second publish found nothing to do
    assert after_publishing["updated_at"] == post.updated_at
    assert after_publishing["published_at"] == later
    assert after_editing["updated_at"] == later


async def test_changing_only_the_excerpt_or_clearing_the_tags(
    client: AsyncClient, author: User, author_headers: Headers, make_post: MakePost
) -> None:
    post = await make_post(author, tags=["one", "two"])

    response = await client.patch(
        f"{API}/posts/{post.id}", json={"excerpt": "In short.", "tags": []}, headers=author_headers
    )

    assert response.status_code == 200
    assert (response.json()["excerpt"], response.json()["tags"]) == ("In short.", [])
    assert response.json()["content"] == post.content


async def test_an_empty_excerpt_asks_for_a_generated_one_again(
    client: AsyncClient, author_headers: Headers
) -> None:
    """Otherwise an author who once wrote an excerpt could never go back."""
    created = await client.post(
        f"{API}/posts", json={**NEW_POST, "excerpt": "Mine."}, headers=author_headers
    )
    post_id = created.json()["id"]

    cleared = await client.patch(
        f"{API}/posts/{post_id}", json={"excerpt": ""}, headers=author_headers
    )
    followed = await client.patch(
        f"{API}/posts/{post_id}", json={"content": "New _words_."}, headers=author_headers
    )

    assert cleared.json()["excerpt"] == "Some content."
    assert followed.json()["excerpt"] == "New words."  # and it follows the content again


async def test_edit_a_post_with_invalid_input(
    client: AsyncClient, author: User, author_headers: Headers, make_post: MakePost
) -> None:
    post = await make_post(author)

    response = await client.patch(
        f"{API}/posts/{post.id}", json={"slug": "hijacked"}, headers=author_headers
    )

    assert response.status_code == 422


@pytest.mark.parametrize(("post_id", "status"), [(MISSING, 404), ("not-an-id", 422)])
async def test_edit_a_post_that_does_not_exist(
    client: AsyncClient, author_headers: Headers, post_id: str, status: int
) -> None:
    response = await client.patch(
        f"{API}/posts/{post_id}", json={"title": "x"}, headers=author_headers
    )

    assert response.status_code == status


async def test_an_author_cannot_edit_someone_elses_post(
    client: AsyncClient,
    db: Database,
    author: User,
    other_author: User,
    make_post: MakePost,
    headers_for: HeadersFor,
) -> None:
    """The attack on any ownership rule: change the id in the URL."""
    published = await make_post(author, "Published", published=True)
    draft = await make_post(author, "Draft")
    intruder = headers_for(other_author)

    on_published = await client.patch(
        f"{API}/posts/{published.id}", json={"title": "Defaced"}, headers=intruder
    )
    on_draft = await client.patch(
        f"{API}/posts/{draft.id}", json={"title": "Defaced"}, headers=intruder
    )

    # They can see the published one, so they are told no. The draft, as far
    # as they may know, isn't there.
    assert (on_published.status_code, on_published.json()) == (403, FORBIDDEN)
    assert (on_draft.status_code, on_draft.json()) == (404, NOT_FOUND)
    assert await db.posts.count_documents({"title": "Defaced"}) == 0


async def test_an_editor_edits_anyones_post(
    client: AsyncClient, author: User, editor: User, make_post: MakePost, headers_for: HeadersFor
) -> None:
    draft = await make_post(author, "Draft")

    response = await client.patch(
        f"{API}/posts/{draft.id}", json={"title": "Edited"}, headers=headers_for(editor)
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Edited"
    assert response.json()["author"]["username"] == "ali"  # still the author's post


async def test_owning_a_post_is_not_enough_without_the_permission(
    client: AsyncClient,
    db: Database,
    author: User,
    author_headers: Headers,
    make_post: MakePost,
) -> None:
    """Roles change: an author demoted to reader keeps their posts but not the right to edit."""
    post = await make_post(author, "Mine", published=True)
    await db.roles.update_one({"name": "author"}, {"$set": {"permissions": []}})

    response = await client.patch(
        f"{API}/posts/{post.id}", json={"title": "Still mine?"}, headers=author_headers
    )

    assert response.status_code == 403


# --- DELETE /posts/{id} ---------------------------------------------------------


async def test_an_author_deletes_their_post(
    client: AsyncClient, db: Database, author: User, author_headers: Headers, make_post: MakePost
) -> None:
    post = await make_post(author, published=True)

    response = await client.delete(f"{API}/posts/{post.id}", headers=author_headers)

    assert response.status_code == 204
    assert await db.posts.count_documents({}) == 0
    assert (await client.get(f"{API}/posts/{post.slug}")).status_code == 404


async def test_delete_a_post_that_does_not_exist(
    client: AsyncClient, author_headers: Headers
) -> None:
    response = await client.delete(f"{API}/posts/{MISSING}", headers=author_headers)

    assert response.status_code == 404


async def test_an_author_cannot_delete_someone_elses_post_but_an_editor_can(
    client: AsyncClient,
    db: Database,
    author: User,
    other_author: User,
    editor: User,
    make_post: MakePost,
    headers_for: HeadersFor,
) -> None:
    post = await make_post(author, published=True)

    refused = await client.delete(f"{API}/posts/{post.id}", headers=headers_for(other_author))
    assert refused.status_code == 403
    assert await db.posts.count_documents({}) == 1

    allowed = await client.delete(f"{API}/posts/{post.id}", headers=headers_for(editor))
    assert allowed.status_code == 204
    assert await db.posts.count_documents({}) == 0


# --- publish and unpublish ------------------------------------------------------


async def test_publishing_puts_a_post_in_the_feed(
    client: AsyncClient, db: Database, author: User, author_headers: Headers, make_post: MakePost
) -> None:
    post = await make_post(author, "Ready")
    assert await _feed_titles(client) == []

    response = await client.post(f"{API}/posts/{post.id}/publish", headers=author_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "published"
    assert response.json()["published_at"] is not None
    assert await _feed_titles(client) == ["Ready"]
    stored = await db.posts.find_one({"_id": post.id})
    assert stored is not None
    assert stored["status"] == "published"


async def test_unpublishing_hides_a_post_again(
    client: AsyncClient, author: User, author_headers: Headers, make_post: MakePost
) -> None:
    post = await make_post(author, "Oops", published=True)

    response = await client.post(f"{API}/posts/{post.id}/unpublish", headers=author_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "draft"
    assert await _feed_titles(client) == []
    assert (await client.get(f"{API}/posts/{post.slug}")).status_code == 404


async def test_republishing_keeps_the_original_date_and_doing_it_twice_is_fine(
    client: AsyncClient, author: User, author_headers: Headers, make_post: MakePost
) -> None:
    post = await make_post(author, published=True)
    assert post.published_at is not None
    first_published = post.published_at.isoformat().replace("+00:00", "Z")

    again = await client.post(f"{API}/posts/{post.id}/publish", headers=author_headers)
    await client.post(f"{API}/posts/{post.id}/unpublish", headers=author_headers)
    await client.post(f"{API}/posts/{post.id}/unpublish", headers=author_headers)
    back = await client.post(f"{API}/posts/{post.id}/publish", headers=author_headers)

    assert (again.status_code, back.status_code) == (200, 200)
    assert again.json()["published_at"] == first_published
    assert back.json()["published_at"] == first_published


@pytest.mark.parametrize("action", ["publish", "unpublish"])
async def test_publish_a_post_that_does_not_exist(
    client: AsyncClient, author_headers: Headers, action: str
) -> None:
    response = await client.post(f"{API}/posts/{MISSING}/{action}", headers=author_headers)

    assert response.status_code == 404


async def test_publishing_needs_its_own_permission(
    client: AsyncClient,
    db: Database,
    make_role: MakeRole,
    make_user: MakeUser,
    make_post: MakePost,
    headers_for: HeadersFor,
) -> None:
    """A contributor may write and edit, while someone else decides what goes out."""
    await make_role("contributor", Perm.POSTS_CREATE, Perm.POSTS_UPDATE_OWN)
    contributor = await make_user("carl", "contributor")
    post = await make_post(contributor, "Submitted")

    response = await client.post(f"{API}/posts/{post.id}/publish", headers=headers_for(contributor))

    assert response.status_code == 403
    stored = await db.posts.find_one({"_id": post.id})
    assert stored is not None
    assert stored["status"] == "draft"


async def test_an_author_cannot_publish_someone_elses_draft_but_an_editor_can(
    client: AsyncClient,
    author: User,
    other_author: User,
    editor: User,
    make_post: MakePost,
    headers_for: HeadersFor,
) -> None:
    post = await make_post(author, "Draft")

    refused = await client.post(f"{API}/posts/{post.id}/publish", headers=headers_for(other_author))
    allowed = await client.post(f"{API}/posts/{post.id}/publish", headers=headers_for(editor))

    assert refused.status_code == 404  # a draft that isn't theirs doesn't exist for them
    assert allowed.status_code == 200
    assert await _feed_titles(client) == ["Draft"]


# --- GET /posts (the feed) ------------------------------------------------------


async def test_the_feed_lists_published_posts_newest_first(
    client: AsyncClient, author: User, make_post: MakePost
) -> None:
    await make_post(author, "First", published=True)
    await make_post(author, "A draft")
    await make_post(author, "Second", published=True)

    response = await client.get(f"{API}/posts")

    assert response.status_code == 200
    body = response.json()
    assert [post["title"] for post in body["items"]] == ["Second", "First"]
    assert body["next_cursor"] is None
    # A list carries summaries: the excerpt, not the whole text.
    assert "content" not in body["items"][0]
    assert body["items"][0]["author"]["username"] == "ali"
    assert body["items"][0]["excerpt"] == "Some content."


async def test_the_feed_pages_through_every_post_once(
    client: AsyncClient, author: User, make_post: MakePost
) -> None:
    for number in range(5):
        await make_post(author, f"Post {number}", published=True)

    titles: list[str] = []
    cursor = None
    for _ in range(4):
        params = {"limit": "2"} | ({"cursor": cursor} if cursor else {})
        page = (await client.get(f"{API}/posts", params=params)).json()
        titles += [post["title"] for post in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert titles == ["Post 4", "Post 3", "Post 2", "Post 1", "Post 0"]


async def test_the_feed_filters_by_tag_and_by_author(
    client: AsyncClient, author: User, other_author: User, make_post: MakePost
) -> None:
    await make_post(author, "Ali on FastAPI", tags=["fastapi", "python"], published=True)
    await make_post(author, "Ali on Mongo", tags=["mongodb"], published=True)
    await make_post(other_author, "Omar on FastAPI", tags=["fastapi"], published=True)

    assert await _feed_titles(client, tag="FastAPI") == ["Omar on FastAPI", "Ali on FastAPI"]
    assert await _feed_titles(client, author="Ali") == ["Ali on Mongo", "Ali on FastAPI"]
    assert await _feed_titles(client, tag="fastapi", author="omar") == ["Omar on FastAPI"]
    assert await _feed_titles(client, tag="rust") == []
    assert await _feed_titles(client, author="nobody") == []


async def test_the_author_filter_takes_a_username_never_an_email(
    client: AsyncClient, author: User, make_post: MakePost
) -> None:
    """
    The attack: usernames are public, emails are not. If the filter accepted
    an email, the feed would tell anyone whether an address has an account.
    """
    await make_post(author, "By Ali", published=True)

    assert await _feed_titles(client, author="ali") == ["By Ali"]
    assert await _feed_titles(client, author=author.email) == []


async def test_the_feed_refuses_a_bad_cursor_or_limit(client: AsyncClient) -> None:
    bad_cursor = await client.get(f"{API}/posts", params={"cursor": "nonsense"})
    bad_limit = await client.get(f"{API}/posts", params={"limit": "0"})

    assert (bad_cursor.status_code, bad_limit.status_code) == (422, 422)


async def test_a_draft_never_leaks_through_a_public_list(
    client: AsyncClient, author: User, make_post: MakePost
) -> None:
    """The attack: ask for it sideways, by its tag, by its author, or by counting."""
    await make_post(author, "Secret plans", tags=["secret"])

    assert await _feed_titles(client) == []
    assert await _feed_titles(client, tag="secret") == []
    assert await _feed_titles(client, author="ali") == []
    assert (await client.get(f"{API}/tags")).json() == []


# --- GET /users/me/posts --------------------------------------------------------


async def test_my_posts_lists_my_drafts_and_published_posts(
    client: AsyncClient,
    author: User,
    other_author: User,
    author_headers: Headers,
    make_post: MakePost,
) -> None:
    await make_post(author, "My published", published=True)
    await make_post(author, "My draft")
    await make_post(other_author, "Not mine", published=True)

    everything = await client.get(f"{API}/users/me/posts", headers=author_headers)
    drafts = await client.get(
        f"{API}/users/me/posts", params={"status": "draft"}, headers=author_headers
    )
    published = await client.get(
        f"{API}/users/me/posts", params={"status": "published"}, headers=author_headers
    )

    assert everything.status_code == 200
    assert [post["title"] for post in everything.json()["items"]] == ["My draft", "My published"]
    assert [post["title"] for post in drafts.json()["items"]] == ["My draft"]
    assert [post["title"] for post in published.json()["items"]] == ["My published"]


async def test_my_posts_pages_and_refuses_an_unknown_status(
    client: AsyncClient, author: User, author_headers: Headers, make_post: MakePost
) -> None:
    for number in range(3):
        await make_post(author, f"Draft {number}")

    first = await client.get(f"{API}/users/me/posts", params={"limit": "2"}, headers=author_headers)
    second = await client.get(
        f"{API}/users/me/posts",
        params={"limit": "2", "cursor": first.json()["next_cursor"]},
        headers=author_headers,
    )
    bad = await client.get(
        f"{API}/users/me/posts", params={"status": "deleted"}, headers=author_headers
    )

    assert [post["title"] for post in first.json()["items"]] == ["Draft 2", "Draft 1"]
    assert [post["title"] for post in second.json()["items"]] == ["Draft 0"]
    assert second.json()["next_cursor"] is None
    assert bad.status_code == 422


# --- GET /tags ------------------------------------------------------------------


async def test_tags_are_counted_over_published_posts(
    client: AsyncClient, author: User, make_post: MakePost
) -> None:
    await make_post(author, "One", tags=["python", "fastapi"], published=True)
    await make_post(author, "Two", tags=["python"], published=True)
    await make_post(author, "Three", tags=["mongodb"], published=True)
    await make_post(author, "Draft", tags=["python", "secret"])

    response = await client.get(f"{API}/tags")

    assert response.status_code == 200
    # Most used first; ties by name.
    assert response.json() == [
        {"name": "python", "count": 2},
        {"name": "fastapi", "count": 1},
        {"name": "mongodb", "count": 1},
    ]


# --- what the database guarantees -----------------------------------------------


async def test_a_post_survives_its_authors_account_being_removed(
    client: AsyncClient, db: Database, author: User, make_post: MakePost
) -> None:
    post = await make_post(author, "Orphan", published=True)
    await db.users.delete_one({"_id": author.id})

    response = await client.get(f"{API}/posts/{post.slug}")

    assert response.status_code == 200
    assert response.json()["author"] is None


async def test_the_database_itself_refuses_a_second_post_with_a_slug(
    db: Database, author: User, make_post: MakePost
) -> None:
    """The last line of defence, whatever the code above it does."""
    post = await make_post(author, "Same")

    with pytest.raises(DuplicateKeyError):
        await db.posts.insert_one({"slug": post.slug, "title": "An impostor"})


# --- one permission at a time ---------------------------------------------------
# The seeded roles bundle permissions, which hides how each behaves alone. An
# administrator can build a role from any of them, so each is tested alone.


async def _holder_of(
    make_role: MakeRole, make_user: MakeUser, headers_for: HeadersFor, *permissions: Perm
) -> Headers:
    await make_role("single", *permissions)
    return headers_for(await make_user("solo", "single"))


async def _act(client: AsyncClient, action: str, post_id: object, headers: Headers) -> Response:
    if action == "update":
        return await client.patch(
            f"{API}/posts/{post_id}", json={"title": "Changed"}, headers=headers
        )
    if action == "delete":
        return await client.delete(f"{API}/posts/{post_id}", headers=headers)
    return await client.post(f"{API}/posts/{post_id}/{action}", headers=headers)


@pytest.mark.parametrize(
    ("permission", "action", "expected"),
    [
        (Perm.POSTS_UPDATE_ANY, "update", 200),
        (Perm.POSTS_PUBLISH_ANY, "publish", 200),
        (Perm.POSTS_DELETE_ANY, "delete", 204),
    ],
)
async def test_an_any_permission_reaches_other_peoples_drafts(
    client: AsyncClient,
    author: User,
    make_role: MakeRole,
    make_user: MakeUser,
    make_post: MakePost,
    headers_for: HeadersFor,
    permission: Perm,
    action: str,
    expected: int,
) -> None:
    """Without also needing `posts:read_drafts:any`: a publisher has to see what they publish."""
    draft = await make_post(author, "Draft")
    headers = await _holder_of(make_role, make_user, headers_for, permission)

    seen = await client.get(f"{API}/posts/{draft.slug}", headers=headers)
    done = await _act(client, action, draft.id, headers)

    assert seen.status_code == 200
    assert done.status_code == expected


async def test_an_any_permission_covers_its_own_action_only(
    client: AsyncClient,
    db: Database,
    author: User,
    make_role: MakeRole,
    make_user: MakeUser,
    make_post: MakePost,
    headers_for: HeadersFor,
) -> None:
    draft = await make_post(author, "Draft")
    publisher = await _holder_of(make_role, make_user, headers_for, Perm.POSTS_PUBLISH_ANY)

    edit = await _act(client, "update", draft.id, publisher)
    delete = await _act(client, "delete", draft.id, publisher)

    # They can see the draft, so the answer is "no", not "not found".
    assert (edit.status_code, delete.status_code) == (403, 403)
    assert await db.posts.count_documents({"title": "Draft"}) == 1


async def test_a_publisher_can_undo_an_unpublish(
    client: AsyncClient,
    author: User,
    make_role: MakeRole,
    make_user: MakeUser,
    make_post: MakePost,
    headers_for: HeadersFor,
) -> None:
    """Once, pulling a post made it invisible to the one who pulled it."""
    post = await make_post(author, "Live", published=True)
    publisher = await _holder_of(make_role, make_user, headers_for, Perm.POSTS_PUBLISH_ANY)

    pulled = await _act(client, "unpublish", post.id, publisher)
    restored = await _act(client, "publish", post.id, publisher)

    assert (pulled.status_code, restored.status_code) == (200, 200)
    assert await _feed_titles(client) == ["Live"]


async def test_a_published_post_cannot_be_rewritten_without_the_right_to_publish(
    client: AsyncClient,
    db: Database,
    make_role: MakeRole,
    make_user: MakeUser,
    make_post: MakePost,
    headers_for: HeadersFor,
) -> None:
    """
    The attack: a writer whose posts someone else approves gets a harmless
    post published, then replaces its text. Changing what is public is
    publishing, so it needs that permission too.
    """
    writer = await _holder_of(
        make_role, make_user, headers_for, Perm.POSTS_CREATE, Perm.POSTS_UPDATE_OWN
    )
    solo = await db.users.find_one({"username": "solo"})
    assert solo is not None
    author_of = User.model_validate({**solo, "id": solo["_id"]})
    draft = await make_post(author_of, "My draft")
    live = await make_post(author_of, "Approved", published=True)

    on_draft = await _act(client, "update", draft.id, writer)
    on_live = await _act(client, "update", live.id, writer)

    assert (on_draft.status_code, on_live.status_code) == (200, 403)
    assert await db.posts.count_documents({"title": "Approved"}) == 1


async def test_an_editor_cannot_write_new_posts(
    client: AsyncClient, editor: User, headers_for: HeadersFor
) -> None:
    response = await client.post(f"{API}/posts", json=NEW_POST, headers=headers_for(editor))

    assert response.status_code == 403


async def test_an_author_cannot_unpublish_someone_elses_post(
    client: AsyncClient,
    author: User,
    other_author: User,
    make_post: MakePost,
    headers_for: HeadersFor,
) -> None:
    post = await make_post(author, "Live", published=True)

    response = await _act(client, "unpublish", post.id, headers_for(other_author))

    assert response.status_code == 403
    assert await _feed_titles(client) == ["Live"]


# --- more ways to ask the public routes -----------------------------------------


async def test_blank_filters_mean_no_filter(
    client: AsyncClient, author: User, make_post: MakePost
) -> None:
    await make_post(author, "Out", published=True)

    assert await _feed_titles(client, tag="", author="") == ["Out"]


@pytest.mark.parametrize(
    "params",
    [
        {"tag[$ne]": "x"},  # an attempt at a MongoDB operator
        {"bogus": "1"},
        {"cursor": "WyIyMDI0LTAxLTAxVDAwOjAwOjAwKzAwOjAwIiwgInp6eiJd"},  # a forged cursor
    ],
)
async def test_the_feed_refuses_what_it_does_not_understand(
    client: AsyncClient, params: dict[str, str]
) -> None:
    response = await client.get(f"{API}/posts", params=params)

    assert response.status_code == 422


async def test_the_feed_shows_a_post_whose_author_is_gone(
    client: AsyncClient, db: Database, author: User, make_post: MakePost
) -> None:
    await make_post(author, "Orphan", published=True)
    await db.users.delete_one({"_id": author.id})

    page = (await client.get(f"{API}/posts")).json()

    assert [(post["title"], post["author"]) for post in page["items"]] == [("Orphan", None)]


async def test_a_deactivated_users_token_is_refused_on_a_public_route(
    client: AsyncClient, db: Database, author: User, author_headers: Headers, make_post: MakePost
) -> None:
    post = await make_post(author, "Out", published=True)
    await db.users.update_one({"_id": author.id}, {"$set": {"is_active": False}})

    response = await client.get(f"{API}/posts/{post.slug}", headers=author_headers)

    assert response.status_code == 401


async def test_a_response_that_depends_on_who_asks_is_never_cached(
    client: AsyncClient, author: User, author_headers: Headers, make_post: MakePost
) -> None:
    """A draft served to its author must not be stored and replayed to someone else."""
    draft = await make_post(author, "Draft")
    public = await make_post(author, "Public", published=True)

    as_author = await client.get(f"{API}/posts/{draft.slug}", headers=author_headers)
    logged_in = await client.get(f"{API}/posts/{public.slug}", headers=author_headers)
    anonymous = await client.get(f"{API}/posts/{public.slug}")

    assert as_author.headers["cache-control"] == "private, no-store"
    assert logged_in.headers["cache-control"] == "private, no-store"
    assert "cache-control" not in anonymous.headers


# --- the indexes really serve the queries ---------------------------------------


@pytest.mark.parametrize(
    ("index", "match", "sort_field"),
    [
        ("feed", {"status": "published"}, "published_at"),
        ("feed_by_tag", {"status": "published", "tags": "python"}, "published_at"),
        ("feed_by_author", {"status": "published", "author_id": None}, "published_at"),
        ("posts_by_author", {"author_id": None}, "created_at"),
    ],
)
async def test_each_list_is_read_from_its_index_in_order(
    db: Database,
    author: User,
    make_post: MakePost,
    index: str,
    match: dict[str, object],
    sort_field: str,
) -> None:
    """
    An index that doesn't end with everything the query sorts by forces
    MongoDB to load every match and sort it in memory. Two of these once did:
    the author feed read 13,300 index entries to return 21 posts.
    """
    await make_post(author, "One", tags=["python"], published=True)
    match = {key: author.id if value is None else value for key, value in match.items()}

    explained = await db.command(
        "explain",
        {
            "find": "posts",
            "filter": match,
            "sort": {sort_field: -1, "_id": -1},
            "hint": index,
        },
        verbosity="queryPlanner",
    )

    stages = plan_stages(explained["queryPlanner"]["winningPlan"])
    assert "IXSCAN" in stages
    assert "SORT" not in stages, stages


# --- how the routes are described ----------------------------------------------


async def test_the_api_docs_show_which_routes_need_a_login(app: FastAPI) -> None:
    """A padlock on a public route would tell people to log in to read the blog."""
    paths = app.openapi()["paths"]

    def security(method: str, path: str) -> object:
        return paths[f"{API}{path}"][method].get("security")

    assert security("get", "/posts") is None
    assert security("get", "/tags") is None
    # With or without: the empty entry is how OpenAPI says "optional".
    assert security("get", "/posts/{slug}") == [{"OAuth2PasswordBearer": []}, {}]
    assert security("post", "/posts") == [{"OAuth2PasswordBearer": []}]
    assert security("patch", "/posts/{post_id}") == [{"OAuth2PasswordBearer": []}]
