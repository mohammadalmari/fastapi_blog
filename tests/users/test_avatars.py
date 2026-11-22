"""The avatar routes over HTTP. What a picture may be is in test_avatar_rules.py."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response

from app.core.database import Database
from app.core.settings import Settings
from app.core.settings.security import AVATAR_MAX_BYTES
from app.core.storage import LocalStorage
from app.posts.models import Post
from app.users.models import User
from tests.helpers import API
from tests.users.pictures import opened, picture

pytestmark = pytest.mark.anyio

Headers = dict[str, str]
AVATAR = f"{API}/users/me/avatar"


def _file(
    data: bytes, name: str = "me.png", media_type: str = "image/png"
) -> dict[str, tuple[str, bytes, str]]:
    return {"file": (name, data, media_type)}


async def _upload(client: AsyncClient, headers: Headers, data: bytes | None = None) -> Response:
    return await client.put(AVATAR, files=_file(data or picture()), headers=headers)


def _stored(settings: Settings) -> list[str]:
    """The names of the avatar files on disk."""
    folder = Path(settings.media_dir) / "avatars"
    return sorted(path.name for path in folder.iterdir()) if folder.exists() else []


@pytest.fixture(autouse=True)
def _empty_media_folder(settings: Settings) -> None:
    # The database is emptied between tests; the disk has to be, too.
    for name in _stored(settings):
        (Path(settings.media_dir) / "avatars" / name).unlink()


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
async def test_route_requires_a_login(client: AsyncClient, method: str) -> None:
    response = await client.request(method, AVATAR, files=_file(picture()))

    assert response.status_code == 401


async def test_upload_an_avatar(
    client: AsyncClient, db: Database, settings: Settings, user: User, user_headers: Headers
) -> None:
    response = await _upload(client, user_headers)

    assert response.status_code == 200, response.text
    stored = await db.users.find_one({"_id": user.id})
    assert stored is not None
    assert _stored(settings) == [stored["avatar_key"]]
    assert response.json()["avatar_url"] == f"/media/avatars/{stored['avatar_key']}"
    assert "avatar_key" not in response.json()
    # Nothing of the user in the name.
    assert len(stored["avatar_key"]) == len("0" * 32 + ".webp")
    assert str(user.id) not in stored["avatar_key"]


async def test_the_avatar_is_served_to_anyone_and_cached_for_good(
    client: AsyncClient, user_headers: Headers
) -> None:
    url = (await _upload(client, user_headers)).json()["avatar_url"]

    response = await client.get(url)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    # Even if it were not a picture, a browser may not guess it is a page.
    assert response.headers["x-content-type-options"] == "nosniff"
    avatar = opened(response.content)
    assert (avatar.format, avatar.size) == ("WEBP", (256, 256))


async def test_a_new_avatar_replaces_the_old_file(
    client: AsyncClient, settings: Settings, user_headers: Headers
) -> None:
    first = (await _upload(client, user_headers)).json()["avatar_url"]

    second = (await _upload(client, user_headers, picture("JPEG"))).json()["avatar_url"]

    assert second != first
    assert _stored(settings) == [second.rsplit("/", 1)[1]]
    assert (await client.get(first)).status_code == 404
    assert (await client.get(second)).status_code == 200


async def test_remove_your_avatar(
    client: AsyncClient, db: Database, settings: Settings, user: User, user_headers: Headers
) -> None:
    url = (await _upload(client, user_headers)).json()["avatar_url"]

    response = await client.delete(AVATAR, headers=user_headers)
    again = await client.delete(AVATAR, headers=user_headers)

    assert (response.status_code, again.status_code) == (204, 204)
    stored = await db.users.find_one({"_id": user.id})
    assert stored is not None
    assert stored["avatar_key"] is None
    assert _stored(settings) == []
    assert (await client.get(url)).status_code == 404


async def test_an_avatar_shows_wherever_its_owner_does(
    client: AsyncClient, author: User, author_headers: Headers, post: Post
) -> None:
    url = (await _upload(client, author_headers)).json()["avatar_url"]

    me = await client.get(f"{API}/users/me", headers=author_headers)
    profile = await client.get(f"{API}/users/{author.username}")
    as_author = await client.get(f"{API}/posts/{post.slug}")

    assert me.json()["avatar_url"] == url
    assert profile.json()["avatar_url"] == url
    assert as_author.json()["author"]["avatar_url"] == url


async def test_an_upload_that_is_not_a_picture_is_refused_and_changes_nothing(
    client: AsyncClient, db: Database, settings: Settings, user: User, user_headers: Headers
) -> None:
    """The attack: a page of script sent as "me.png" with an image content type."""
    before = (await _upload(client, user_headers)).json()["avatar_url"]

    response = await client.put(
        AVATAR,
        files=_file(b"<html><script>alert(1)</script></html>", "me.png", "image/png"),
        headers=user_headers,
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "file"]
    stored = await db.users.find_one({"_id": user.id})
    assert stored is not None
    assert before.endswith(stored["avatar_key"])
    assert _stored(settings) == [stored["avatar_key"]]


async def test_a_file_over_the_limit_is_refused(
    client: AsyncClient, settings: Settings, user_headers: Headers
) -> None:
    # A real picture, padded past the limit: its size is the only thing wrong.
    too_big = picture() + b"\x00" * AVATAR_MAX_BYTES

    response = await _upload(client, user_headers, too_big)

    assert response.status_code == 413
    assert response.json() == {"detail": "The picture must be at most 2 MB"}
    assert _stored(settings) == []


async def test_a_body_far_over_the_limit_is_cut_off_by_the_route_itself(
    client: AsyncClient, user_headers: Headers
) -> None:
    """Not read into memory first: the route's own body limit stops it."""
    response = await _upload(client, user_headers, b"\x00" * (AVATAR_MAX_BYTES * 2))

    assert response.status_code == 413
    # The framework's answer, in plain text; the app's own 413 is JSON and
    # comes only after the whole file has been received.
    assert response.headers["content-type"].startswith("text/plain")


async def test_a_file_at_the_limit_is_accepted(client: AsyncClient, user_headers: Headers) -> None:
    """The route's body limit leaves room for the form around the file."""
    upload = picture()
    exactly = upload + b"\x00" * (AVATAR_MAX_BYTES - len(upload))

    response = await _upload(client, user_headers, exactly)

    assert response.status_code == 200, response.text


async def test_the_upload_must_be_the_form_field_called_file_and_nothing_else(
    client: AsyncClient, settings: Settings, user_headers: Headers
) -> None:
    misnamed = await client.put(
        AVATAR, files={"picture": ("me.png", picture(), "image/png")}, headers=user_headers
    )
    with_a_stray_field = await client.put(
        AVATAR, files=_file(picture()), data={"caption": "me"}, headers=user_headers
    )
    two_files = await client.put(
        AVATAR,
        files=[
            ("file", ("a.png", picture(), "image/png")),
            ("file", ("b.png", picture(), "image/png")),
        ],
        headers=user_headers,
    )

    assert misnamed.status_code == 422
    assert (with_a_stray_field.status_code, two_files.status_code) == (400, 400)
    assert _stored(settings) == []


@pytest.mark.parametrize(
    ("name", "answer"),
    [
        ("0" * 32 + ".webp", 404),  # a well-formed name nobody has
        ("nothing-like-a-key.webp", 422),
        ("0" * 32 + ".png", 422),
        ("0" * 32 + ".webp.html", 422),
        ("A" * 32 + ".webp", 422),
        # An encoded slash is decoded before routing: these are other paths.
        ("..%2F..%2Fpyproject.toml", 404),
        ("%2e%2e%2f" + "0" * 32 + ".webp", 404),
    ],
)
async def test_the_media_route_serves_avatars_and_nothing_else(
    client: AsyncClient, name: str, answer: int
) -> None:
    """The attack: ask the file route for a file that is not an avatar."""
    response = await client.get(f"/media/avatars/{name}")

    assert response.status_code == answer
    assert "content-type" not in response.headers or response.headers["content-type"].startswith(
        "application/json"
    )


async def test_a_file_in_the_avatar_folder_that_is_not_an_avatar_is_not_served(
    client: AsyncClient, settings: Settings
) -> None:
    """Only names the app itself makes are looked up, whatever else is on the disk."""
    folder = Path(settings.media_dir) / "avatars"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "notes.txt").write_text("not for the public")

    response = await client.get("/media/avatars/notes.txt")

    assert response.status_code == 422
    assert "not for the public" not in response.text


async def test_an_avatar_that_cannot_be_stored_leaves_the_account_as_it_was(
    client: AsyncClient,
    db: Database,
    user: User,
    user_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def full_disk(self: LocalStorage, key: str, data: bytes) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(LocalStorage, "save", full_disk)

    with pytest.raises(OSError, match="No space left"):
        await _upload(client, user_headers)

    stored = await db.users.find_one({"_id": user.id})
    assert stored is not None
    assert stored["avatar_key"] is None


async def test_the_api_docs_describe_the_upload_and_hide_the_storage_key(app: FastAPI) -> None:
    schema = app.openapi()
    upload = schema["paths"][f"{API}/users/me/avatar"]["put"]
    account = schema["components"]["schemas"]["UserMe"]["properties"]

    assert "multipart/form-data" in upload["requestBody"]["content"]
    assert "avatar_url" in account
    assert "avatar_key" not in account
