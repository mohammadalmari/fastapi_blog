"""What a user sees about their own roles."""

import pytest
from httpx import AsyncClient

from app.core.database import Database
from app.rbac.permissions import READER_PERMISSIONS, Perm
from tests.conftest import MakeRole, MakeUser
from tests.helpers import API, PASSWORD, bearer, login, register

pytestmark = pytest.mark.anyio


async def test_me_lists_roles_and_effective_permissions(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    response = await client.get(f"{API}/users/me", headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert [role["name"] for role in body["roles"]] == ["admin"]
    assert body["roles"][0].keys() == {"id", "name"}
    assert body["permissions"] == sorted(Perm)


async def test_permissions_are_the_union_of_all_roles(
    client: AsyncClient, db: Database, user_headers: dict[str, str], make_role: MakeRole
) -> None:
    extra = await make_role("author", Perm.POSTS_CREATE, Perm.COMMENTS_CREATE)
    await db.users.update_one({}, {"$push": {"role_ids": extra.id}})

    response = await client.get(f"{API}/users/me", headers=user_headers)

    body = response.json()
    assert sorted(role["name"] for role in body["roles"]) == ["author", "reader"]
    assert body["permissions"] == sorted({*READER_PERMISSIONS, Perm.POSTS_CREATE})


async def test_a_new_account_can_do_what_the_default_role_allows(client: AsyncClient) -> None:
    """What is stored at sign-up is checked in test_auth.py; this is what it means."""
    await register(client)
    access_token = (await login(client, password=PASSWORD)).json()["access_token"]

    me = await client.get(f"{API}/users/me", headers=bearer(access_token))

    assert [role["name"] for role in me.json()["roles"]] == ["reader"]
    assert me.json()["permissions"] == sorted(READER_PERMISSIONS)


async def test_a_new_account_gets_whichever_role_is_the_default_now(
    client: AsyncClient, db: Database, make_role: MakeRole
) -> None:
    member = await make_role("member", is_default=True)

    await register(client)

    stored = await db.users.find_one({})
    assert stored is not None
    assert stored["role_ids"] == [member.id]


async def test_a_role_that_was_deleted_under_a_user_is_ignored(
    client: AsyncClient, db: Database, user_headers: dict[str, str]
) -> None:
    """A dangling role id must not break the account."""
    await db.roles.delete_many({"name": "reader"})

    response = await client.get(f"{API}/users/me", headers=user_headers)

    assert response.status_code == 200
    assert (response.json()["roles"], response.json()["permissions"]) == ([], [])


async def test_the_user_factory_refuses_a_role_that_does_not_exist(make_user: MakeUser) -> None:
    """
    A test of the test tools: a misspelt role name must fail loudly. If it
    made a user with no roles instead, a "403" test would pass for the wrong
    reason.
    """
    with pytest.raises(AssertionError, match="no role named 'amdin'"):
        await make_user("typo", "amdin")
