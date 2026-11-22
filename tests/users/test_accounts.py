"""What administrators do with accounts: list them, deactivate them, bring them back."""

import anyio
import pytest
from httpx import AsyncClient, Response

from app.auth.repository import RefreshTokenRepository
from app.core.database import Database
from app.posts.models import Post
from app.rbac.permissions import READER_PERMISSIONS, Perm
from app.users import repository as users_repository
from app.users.models import User
from app.users.repository import UserRepository
from tests.conftest import HeadersFor, MakeRole, MakeUser
from tests.helpers import API, PASSWORD, login, plan_stages, refresh_token_of, refresh_with

pytestmark = pytest.mark.anyio

Headers = dict[str, str]
MISSING = "5f0000000000000000000000"
USERS = f"{API}/users"


def _status(user: User) -> str:
    return f"{USERS}/{user.id}/status"


async def _is_active(db: Database, user: User) -> bool:
    stored = await db.users.find_one({"_id": user.id})
    assert stored is not None
    return bool(stored["is_active"])


# --- GET /users -----------------------------------------------------------------


async def test_list_accounts_newest_first_with_their_roles(
    client: AsyncClient, admin: User, admin_headers: Headers, make_user: MakeUser
) -> None:
    await make_user("sam", "reader")
    await make_user("gone", "reader", is_active=False)

    response = await client.get(USERS, headers=admin_headers)

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["username"] for item in items] == ["gone", "sam", "root"]
    assert [[role["name"] for role in item["roles"]] for item in items] == [
        ["reader"],
        ["reader"],
        ["admin"],
    ]
    assert [item["is_active"] for item in items] == [False, True, True]
    assert items[0].keys() == {
        "id",
        "email",
        "username",
        "display_name",
        "avatar_url",
        "is_active",
        "created_at",
        "roles",
    }
    # Emails, for one reader's eyes.
    assert response.headers["cache-control"] == "private, no-store"


async def test_accounts_page_through_every_account_once(
    client: AsyncClient, admin_headers: Headers, make_user: MakeUser
) -> None:
    for number in range(5):
        await make_user(f"user{number}", "reader")
    seen: list[str] = []
    cursor: str | None = None

    for _ in range(4):
        params = {"limit": "2"} | ({"cursor": cursor} if cursor else {})
        page = (await client.get(USERS, params=params, headers=admin_headers)).json()
        seen += [item["username"] for item in page["items"]]
        if page["next_cursor"] is None:
            break
        cursor = str(page["next_cursor"])

    assert seen == ["user4", "user3", "user2", "user1", "user0", "root"]


@pytest.mark.parametrize(("wanted", "found"), [("true", ["sam", "root"]), ("false", ["gone"])])
async def test_accounts_can_be_filtered_by_status(
    client: AsyncClient, admin_headers: Headers, make_user: MakeUser, wanted: str, found: list[str]
) -> None:
    await make_user("sam", "reader")
    await make_user("gone", "reader", is_active=False)

    response = await client.get(USERS, params={"is_active": wanted}, headers=admin_headers)

    assert [item["username"] for item in response.json()["items"]] == found


async def test_listing_accounts_needs_the_permission(
    client: AsyncClient, user_headers: Headers
) -> None:
    as_a_reader = await client.get(USERS, headers=user_headers)
    as_a_visitor = await client.get(USERS)

    assert (as_a_reader.status_code, as_a_visitor.status_code) == (403, 401)


@pytest.mark.parametrize("params", [{"active": "true"}, {"is_active": "maybe"}, {"limit": "0"}])
async def test_list_accounts_with_invalid_input(
    client: AsyncClient, admin_headers: Headers, params: dict[str, str]
) -> None:
    """A misspelt filter is an error, not a quietly unfiltered list."""
    response = await client.get(USERS, params=params, headers=admin_headers)

    assert response.status_code == 422


@pytest.mark.parametrize("is_active", [None, True, False])
async def test_the_list_of_accounts_is_read_off_an_index(
    db: Database, make_user: MakeUser, is_active: bool | None
) -> None:
    for number in range(8):
        await make_user(f"user{number}", "reader", is_active=number % 2 == 0)
    # Four of each, so that a page of three exists whichever is asked for.

    explained = await db.command(
        "explain",
        {
            "find": "users",
            "filter": users_repository.users_query(is_active),
            "sort": {"created_at": -1, "_id": -1},
            "limit": 3,
        },
        verbosity="executionStats",
    )

    stats = explained["executionStats"]
    stages = plan_stages(explained["queryPlanner"]["winningPlan"])
    assert "IXSCAN" in stages
    assert "SORT" not in stages, stages
    assert stats["totalDocsExamined"] == stats["nReturned"] == 3


# --- PATCH /users/{id}/status ---------------------------------------------------


async def test_deactivate_an_account(
    client: AsyncClient, db: Database, user: User, user_headers: Headers, admin_headers: Headers
) -> None:
    session = await login(client)

    response = await client.patch(_status(user), json={"is_active": False}, headers=admin_headers)

    assert response.status_code == 200, response.text
    assert (response.json()["username"], response.json()["is_active"]) == ("jane", False)
    assert [role["name"] for role in response.json()["roles"]] == ["reader"]
    assert await _is_active(db, user) is False
    # From the very next request: the token, logging in, and refreshing.
    assert (await client.get(f"{API}/users/me", headers=user_headers)).status_code == 401
    assert (await login(client)).status_code == 401
    assert (await refresh_with(client, refresh_token_of(session))).status_code == 401


async def test_a_reactivated_account_starts_afresh(
    client: AsyncClient, db: Database, user: User, admin_headers: Headers
) -> None:
    """
    The attack: a session opened before the ban, kept, and tried again once
    the account is back. Deactivating ends sessions for good.
    """
    session = await login(client)
    await client.patch(_status(user), json={"is_active": False}, headers=admin_headers)

    response = await client.patch(_status(user), json={"is_active": True}, headers=admin_headers)

    assert (response.status_code, response.json()["is_active"]) == (200, True)
    assert (await login(client, password=PASSWORD)).status_code == 200
    assert (await refresh_with(client, refresh_token_of(session))).status_code == 401
    assert await db.refresh_tokens.count_documents({"revoked_at": None}) == 1  # the new login


async def test_reactivating_ends_any_session_the_deactivation_missed(
    client: AsyncClient, db: Database, user: User, admin_headers: Headers
) -> None:
    """
    A login can slip in while an account is being switched off. Its token is
    useless while the account is off; bringing the account back must not
    bring that session back with it.
    """
    session = await login(client)
    await db.users.update_one({"_id": user.id}, {"$set": {"is_active": False}})

    response = await client.patch(_status(user), json={"is_active": True}, headers=admin_headers)

    assert response.status_code == 200
    assert (await refresh_with(client, refresh_token_of(session))).status_code == 401


async def test_a_refusal_says_nothing_about_the_account(
    client: AsyncClient,
    admin: User,
    make_role: MakeRole,
    make_user: MakeUser,
    headers_for: HeadersFor,
) -> None:
    """The attack: try ids, and read from the refusal which of them are administrators."""
    await make_role("moderator", Perm.USERS_DEACTIVATE)
    moderator = headers_for(await make_user("mod", "moderator"))

    response = await client.patch(_status(admin), json={"is_active": False}, headers=moderator)

    assert response.status_code == 403
    assert response.json() == {"detail": "You don't have permission to do this"}


async def test_a_response_that_carries_an_email_is_never_cached(
    client: AsyncClient, user: User, admin_headers: Headers
) -> None:
    status_change = await client.patch(
        _status(user), json={"is_active": True}, headers=admin_headers
    )
    roles = await client.put(
        f"{USERS}/{user.id}/roles", json={"role_ids": []}, headers=admin_headers
    )

    assert status_change.headers["cache-control"] == "private, no-store"
    assert roles.headers["cache-control"] == "private, no-store"


async def test_roles_are_listed_in_the_same_order_everywhere(
    client: AsyncClient, db: Database, user: User, admin_headers: Headers, make_role: MakeRole
) -> None:
    earlier_by_name = await make_role("author", Perm.POSTS_CREATE)
    await db.users.update_one({"_id": user.id}, {"$push": {"role_ids": earlier_by_name.id}})

    listed = await client.get(USERS, headers=admin_headers)
    changed = await client.patch(_status(user), json={"is_active": True}, headers=admin_headers)

    (row,) = [item for item in listed.json()["items"] if item["username"] == "jane"]
    assert [role["name"] for role in row["roles"]] == ["author", "reader"]
    assert [role["name"] for role in changed.json()["roles"]] == ["author", "reader"]


async def test_setting_the_status_an_account_already_has_changes_nothing(
    client: AsyncClient, db: Database, user: User, admin_headers: Headers
) -> None:
    session = await login(client)
    before = await db.users.find_one({"_id": user.id})

    response = await client.patch(_status(user), json={"is_active": True}, headers=admin_headers)

    assert (response.status_code, response.json()["is_active"]) == (200, True)
    assert await db.users.find_one({"_id": user.id}) == before
    assert (await refresh_with(client, refresh_token_of(session))).status_code == 200


async def test_changing_a_status_needs_the_permission(
    client: AsyncClient, db: Database, user_headers: Headers, make_user: MakeUser
) -> None:
    # Another reader: someone the caller holds every permission of, so that
    # the missing "deactivate" permission is the only thing in the way.
    peer = await make_user("sam", "reader")

    as_a_reader = await client.patch(_status(peer), json={"is_active": False}, headers=user_headers)
    as_a_visitor = await client.patch(_status(peer), json={"is_active": False})

    assert (as_a_reader.status_code, as_a_visitor.status_code) == (403, 401)
    assert await _is_active(db, peer) is True


async def test_change_the_status_of_an_account_that_does_not_exist(
    client: AsyncClient, admin_headers: Headers
) -> None:
    response = await client.patch(
        f"{USERS}/{MISSING}/status", json={"is_active": False}, headers=admin_headers
    )

    assert (response.status_code, response.json()) == (404, {"detail": "User not found"})


@pytest.mark.parametrize("body", [{}, {"is_active": "maybe"}, {"is_active": False, "role_ids": []}])
async def test_change_a_status_with_invalid_input(
    client: AsyncClient, user: User, admin_headers: Headers, body: dict[str, object]
) -> None:
    response = await client.patch(_status(user), json=body, headers=admin_headers)

    assert response.status_code == 422


async def test_nobody_changes_their_own_status(
    client: AsyncClient, db: Database, admin: User, admin_headers: Headers, make_user: MakeUser
) -> None:
    """Even with another admin beside them: locking yourself out can't be undone by you."""
    await make_user("second", "admin")

    response = await client.patch(_status(admin), json={"is_active": False}, headers=admin_headers)

    assert response.status_code == 409
    assert await _is_active(db, admin) is True


async def test_a_moderator_cannot_switch_off_someone_who_may_do_more(
    client: AsyncClient,
    db: Database,
    admin: User,
    user: User,
    make_role: MakeRole,
    make_user: MakeUser,
    headers_for: HeadersFor,
) -> None:
    """
    The attack: hold "deactivate users" and nothing else, and remove every
    administrator but the last. You may only switch off, or back on, an
    account whose every permission you hold yourself.
    """
    await make_user("second", "admin")
    # Everything a reader may do, plus deactivating: enough for readers.
    await make_role("moderator", Perm.USERS_DEACTIVATE, *READER_PERMISSIONS)
    moderator = headers_for(await make_user("mod", "moderator"))
    banned_admin = await make_user("banned", "admin", is_active=False)

    off = await client.patch(_status(admin), json={"is_active": False}, headers=moderator)
    back_on = await client.patch(_status(banned_admin), json={"is_active": True}, headers=moderator)
    a_reader = await client.patch(_status(user), json={"is_active": False}, headers=moderator)

    assert (off.status_code, back_on.status_code) == (403, 403)
    assert await _is_active(db, admin) is True
    assert await _is_active(db, banned_admin) is False
    assert a_reader.status_code == 200
    assert await _is_active(db, user) is False


async def test_the_last_active_admin_cannot_be_deactivated(
    client: AsyncClient,
    db: Database,
    admin: User,
    make_role: MakeRole,
    make_user: MakeUser,
    headers_for: HeadersFor,
) -> None:
    """Someone who holds every permission, without being an admin, still can't."""
    await make_role("deputy", *Perm)
    deputy = headers_for(await make_user("deputy", "deputy"))
    await make_user("former", "admin", is_active=False)

    response = await client.patch(_status(admin), json={"is_active": False}, headers=deputy)

    assert response.status_code == 409
    assert response.json() == {"detail": "This would leave no active admin"}
    assert await _is_active(db, admin) is True


async def test_two_admins_deactivating_each_other_at_once_leave_one(
    client: AsyncClient, db: Database, admin: User, make_user: MakeUser, headers_for: HeadersFor
) -> None:
    """Each request alone is allowed; together they would leave nobody in charge."""
    second = await make_user("second", "admin")
    responses: list[Response] = []

    async def deactivate(target: User, by: User) -> None:
        responses.append(
            await client.patch(_status(target), json={"is_active": False}, headers=headers_for(by))
        )

    async with anyio.create_task_group() as group:
        group.start_soon(deactivate, second, admin)
        group.start_soon(deactivate, admin, second)

    # One went through. The other found itself the last admin standing (409),
    # or found its own caller already switched off (401).
    statuses = sorted(response.status_code for response in responses)
    assert statuses in ([200, 401], [200, 409])
    assert await db.users.count_documents({"is_active": True}) == 1


def _failing(*args: object, **kwargs: object) -> None:
    raise RuntimeError("the database is having a moment")


async def test_a_deactivation_whose_sessions_cannot_be_ended_is_refused(
    client: AsyncClient,
    db: Database,
    user: User,
    admin_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(RefreshTokenRepository, "revoke_all_for_user", _failing)

    with pytest.raises(RuntimeError):
        await client.patch(_status(user), json={"is_active": False}, headers=admin_headers)

    assert await _is_active(db, user) is True


async def test_a_deactivation_that_cannot_be_stored_ends_no_session(
    client: AsyncClient,
    db: Database,
    user: User,
    admin_headers: Headers,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sessions are ended first; if the last step fails, that is undone."""
    session = await login(client)
    monkeypatch.setattr(UserRepository, "set_active", _failing)

    with pytest.raises(RuntimeError):
        await client.patch(_status(user), json={"is_active": False}, headers=admin_headers)

    assert await _is_active(db, user) is True
    assert (await refresh_with(client, refresh_token_of(session))).status_code == 200


async def test_what_a_deactivated_author_wrote_stays_under_their_name(
    client: AsyncClient, author: User, admin_headers: Headers, post: Post
) -> None:
    await client.patch(_status(author), json={"is_active": False}, headers=admin_headers)

    feed = await client.get(f"{API}/posts")

    (item,) = feed.json()["items"]
    assert item["author"]["username"] == "ali"
