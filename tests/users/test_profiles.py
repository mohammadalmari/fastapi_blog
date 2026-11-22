"""Editing your own profile, and the public profile page."""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from app.core.database import Database
from app.users import repository as users_repository
from app.users.models import User
from tests.conftest import MakeUser
from tests.helpers import API, PASSWORD, login

pytestmark = pytest.mark.anyio

Headers = dict[str, str]
ME = f"{API}/users/me"


# --- PATCH /users/me ------------------------------------------------------------


async def test_edit_your_profile(
    client: AsyncClient,
    db: Database,
    user: User,
    user_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    later = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)
    monkeypatch.setattr(users_repository, "utcnow", lambda: later)

    response = await client.patch(
        ME,
        json={"display_name": "  Jane Doe ", "bio": "Writes.\nSometimes."},
        headers=user_headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["display_name"], body["bio"]) == ("Jane Doe", "Writes.\nSometimes.")
    # The same shape as GET /users/me, roles and permissions included.
    assert [role["name"] for role in body["roles"]] == ["reader"]
    stored = await db.users.find_one({"_id": user.id})
    assert stored is not None
    assert (stored["display_name"], stored["bio"]) == ("Jane Doe", "Writes.\nSometimes.")
    assert stored["updated_at"] == later


async def test_an_empty_string_clears_a_field_and_null_leaves_it(
    client: AsyncClient, db: Database, user: User, user_headers: Headers
) -> None:
    await client.patch(ME, json={"display_name": "Jane", "bio": "Hello"}, headers=user_headers)

    response = await client.patch(ME, json={"display_name": "", "bio": None}, headers=user_headers)

    assert response.status_code == 200
    assert (response.json()["display_name"], response.json()["bio"]) == (None, "Hello")
    stored = await db.users.find_one({"_id": user.id})
    assert stored is not None
    assert (stored["display_name"], stored["bio"]) == (None, "Hello")


async def test_an_edit_that_changes_nothing_writes_nothing(
    client: AsyncClient,
    db: Database,
    user: User,
    user_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await client.patch(ME, json={"display_name": "Jane"}, headers=user_headers)
    before = await db.users.find_one({"_id": user.id})
    later = datetime.now(UTC) + timedelta(hours=1)
    monkeypatch.setattr(users_repository, "utcnow", lambda: later)

    response = await client.patch(
        ME, json={"display_name": "Jane", "username": "JANE"}, headers=user_headers
    )

    assert response.status_code == 200
    assert await db.users.find_one({"_id": user.id}) == before


async def test_change_your_username(client: AsyncClient, user: User, user_headers: Headers) -> None:
    response = await client.patch(ME, json={"username": "Jane_Doe"}, headers=user_headers)

    assert response.status_code == 200
    assert response.json()["username"] == "jane_doe"
    # The token still works: it names the account, not the username.
    assert (await client.get(ME, headers=user_headers)).json()["username"] == "jane_doe"
    assert (await login(client, identifier="jane_doe", password=PASSWORD)).status_code == 200
    assert (await client.get(f"{API}/users/jane_doe")).status_code == 200
    assert (await client.get(f"{API}/users/jane")).status_code == 404


async def test_a_username_someone_else_has_is_refused(
    client: AsyncClient, db: Database, user: User, user_headers: Headers, make_user: MakeUser
) -> None:
    await make_user("sam", "reader")

    response = await client.patch(
        ME, json={"username": "Sam", "display_name": "Sam"}, headers=user_headers
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Username already taken"}
    stored = await db.users.find_one({"_id": user.id})
    assert stored is not None
    # Nothing of the request was applied, the display name included.
    assert (stored["username"], stored["display_name"]) == ("jane", None)


@pytest.mark.parametrize(
    "field",
    ["email", "password", "password_hash", "is_active", "role_ids", "roles", "avatar_key", "id"],
)
async def test_what_is_not_yours_to_edit_is_refused_not_ignored(
    client: AsyncClient, db: Database, user: User, user_headers: Headers, field: str
) -> None:
    """The attack: add a field to the request and hope the server copies it over."""
    before = await db.users.find_one({"_id": user.id})

    response = await client.patch(
        ME, json={"display_name": "Jane", field: "anything"}, headers=user_headers
    )

    assert response.status_code == 422
    assert await db.users.find_one({"_id": user.id}) == before


async def test_edit_with_invalid_input(client: AsyncClient, user_headers: Headers) -> None:
    response = await client.patch(ME, json={"display_name": "x" * 51}, headers=user_headers)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "display_name"]


async def test_editing_needs_a_login(client: AsyncClient) -> None:
    response = await client.patch(ME, json={"display_name": "Jane"})

    assert response.status_code == 401


# --- GET /users/{username} ------------------------------------------------------


async def test_a_profile_is_public_and_shows_nothing_private(
    client: AsyncClient, user: User, user_headers: Headers
) -> None:
    await client.patch(ME, json={"display_name": "Jane", "bio": "Hello"}, headers=user_headers)

    response = await client.get(f"{API}/users/JANE")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "id": str(user.id),
        "username": "jane",
        "display_name": "Jane",
        "bio": "Hello",
        "avatar_url": None,
        "created_at": body["created_at"],
    }


@pytest.mark.parametrize(
    ("name", "answer"), [("nobody", 404), ("jane@example.com", 404), ("x" * 65, 422)]
)
async def test_a_profile_that_is_not_there(
    client: AsyncClient, user: User, name: str, answer: int
) -> None:
    """An email finds nobody: a public page that matched emails would confirm accounts."""
    response = await client.get(f"{API}/users/{name}")

    assert response.status_code == answer


async def test_a_deactivated_account_has_no_profile(
    client: AsyncClient, make_user: MakeUser
) -> None:
    await make_user("gone", "reader", is_active=False)

    response = await client.get(f"{API}/users/gone")
    never_existed = await client.get(f"{API}/users/nobody")

    assert response.status_code == 404
    assert response.json() == never_existed.json()
