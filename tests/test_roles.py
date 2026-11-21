"""
The roles and permissions routes, and role assignment. Each route follows the
same checklist: success, invalid input, not logged in, not allowed, not
found, conflict, and what changed in the database.
"""

import anyio
import pytest
from bson import ObjectId
from httpx import AsyncClient, Response
from pymongo.errors import DuplicateKeyError

from app.core.database import Database
from app.rbac.models import Role
from app.rbac.permissions import READER_PERMISSIONS, Perm
from app.users.models import User
from tests.conftest import HeadersFor, MakeRole, MakeUser
from tests.helpers import API

pytestmark = pytest.mark.anyio

Headers = dict[str, str]
# A well-formed id that matches nothing. Fixed, not random: it appears in test
# names, and parallel workers must all collect the same names.
MISSING = "5f0000000000000000000000"
FORBIDDEN = {"detail": "You don't have permission to do this"}


async def _create_role(client: AsyncClient, headers: Headers, **fields: object) -> Response:
    body = {"name": "author", "description": "Writes posts", "permissions": [Perm.POSTS_CREATE]}
    return await client.post(f"{API}/roles", json={**body, **fields}, headers=headers)


# --- every route refuses strangers and unprivileged users ---------------------

ROUTES = [
    ("GET", "/permissions"),
    ("GET", "/roles"),
    ("POST", "/roles"),
    ("GET", f"/roles/{MISSING}"),
    ("PATCH", f"/roles/{MISSING}"),
    ("DELETE", f"/roles/{MISSING}"),
    ("PUT", f"/users/{MISSING}/roles"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_route_requires_a_login(client: AsyncClient, method: str, path: str) -> None:
    response = await client.request(method, f"{API}{path}", json={})

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_route_refuses_a_user_without_the_permission(
    client: AsyncClient, user_headers: Headers, method: str, path: str
) -> None:
    """A reader holds none of the roles:* or users:* permissions."""
    response = await client.request(method, f"{API}{path}", json={}, headers=user_headers)

    assert response.status_code == 403
    assert response.json() == FORBIDDEN


async def test_one_permission_opens_one_door_only(
    client: AsyncClient, make_role: MakeRole, make_user: MakeUser, headers_for: HeadersFor
) -> None:
    await make_role("auditor", Perm.ROLES_READ)
    auditor = await make_user("auditor_1", "auditor")

    can_read = await client.get(f"{API}/roles", headers=headers_for(auditor))
    cannot_write = await _create_role(client, headers_for(auditor))

    assert can_read.status_code == 200
    assert cannot_write.status_code == 403


# --- GET /permissions, GET /roles, GET /roles/{id} ---------------------------


async def test_list_permissions(client: AsyncClient, admin_headers: Headers) -> None:
    response = await client.get(f"{API}/permissions", headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body] == sorted(Perm)
    assert body[0].keys() == {"id", "resource", "description"}


async def test_list_roles(client: AsyncClient, admin_headers: Headers) -> None:
    response = await client.get(f"{API}/roles", headers=admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert [role["name"] for role in body] == ["admin", "reader"]
    assert body[0].keys() == {
        "id",
        "name",
        "description",
        "permissions",
        "is_system",
        "is_default",
        "created_at",
        "updated_at",
    }


async def test_get_a_role(
    client: AsyncClient, admin_headers: Headers, roles: dict[str, Role]
) -> None:
    response = await client.get(f"{API}/roles/{roles['reader'].id}", headers=admin_headers)

    assert response.status_code == 200
    assert response.json()["name"] == "reader"
    assert response.json()["is_default"] is True


@pytest.mark.parametrize(("role_id", "status"), [(MISSING, 404), ("not-an-id", 422)])
async def test_get_a_role_that_does_not_exist(
    client: AsyncClient, admin_headers: Headers, role_id: str, status: int
) -> None:
    response = await client.get(f"{API}/roles/{role_id}", headers=admin_headers)

    assert response.status_code == status


# --- POST /roles -------------------------------------------------------------


async def test_create_a_role(client: AsyncClient, db: Database, admin_headers: Headers) -> None:
    response = await _create_role(
        client,
        admin_headers,
        name=" Author ",
        permissions=[Perm.POSTS_UPDATE_OWN, Perm.POSTS_CREATE, Perm.POSTS_CREATE],
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "author"
    assert body["permissions"] == [Perm.POSTS_CREATE, Perm.POSTS_UPDATE_OWN]
    assert (body["is_system"], body["is_default"]) == (False, False)

    stored = await db.roles.find_one({"name": "author"})
    assert stored is not None
    assert str(stored["_id"]) == body["id"]
    assert stored["permissions"] == [Perm.POSTS_CREATE, Perm.POSTS_UPDATE_OWN]


async def test_create_a_role_with_invalid_input(
    client: AsyncClient, db: Database, admin_headers: Headers
) -> None:
    """That bad input is a 422 and creates nothing; test_role_schemas.py lists the rules."""
    response = await _create_role(client, admin_headers, permissions=["posts:fly"])

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "permissions", 0]
    assert await db.roles.count_documents({}) == 2


async def test_create_a_role_with_a_taken_name(
    client: AsyncClient, db: Database, admin_headers: Headers
) -> None:
    response = await _create_role(client, admin_headers, name="Reader")

    assert response.status_code == 409
    assert response.json() == {"detail": "A role with this name already exists"}
    assert await db.roles.count_documents({}) == 2


# --- PATCH /roles/{id} -------------------------------------------------------


async def test_update_a_role(client: AsyncClient, db: Database, admin_headers: Headers) -> None:
    role_id = (await _create_role(client, admin_headers)).json()["id"]

    response = await client.patch(
        f"{API}/roles/{role_id}",
        json={"name": "writer", "permissions": [Perm.POSTS_DELETE_OWN]},
        headers=admin_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "writer"
    assert body["permissions"] == [Perm.POSTS_DELETE_OWN]
    assert body["description"] == "Writes posts"  # untouched: it wasn't sent
    stored = await db.roles.find_one({"_id": ObjectId(role_id)})
    assert stored is not None
    assert (stored["name"], stored["permissions"]) == ("writer", [Perm.POSTS_DELETE_OWN])
    assert stored["updated_at"] >= stored["created_at"]


async def test_update_a_role_that_does_not_exist(
    client: AsyncClient, admin_headers: Headers
) -> None:
    response = await client.patch(
        f"{API}/roles/{MISSING}", json={"description": "x"}, headers=admin_headers
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Role not found"}


async def test_update_a_role_with_invalid_input(
    client: AsyncClient, admin_headers: Headers, roles: dict[str, Role]
) -> None:
    response = await client.patch(
        f"{API}/roles/{roles['reader'].id}",
        json={"permissions": ["posts:fly"]},
        headers=admin_headers,
    )

    assert response.status_code == 422


async def test_rename_a_role_to_a_taken_name(client: AsyncClient, admin_headers: Headers) -> None:
    role_id = (await _create_role(client, admin_headers)).json()["id"]

    response = await client.patch(
        f"{API}/roles/{role_id}", json={"name": "reader"}, headers=admin_headers
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "A role with this name already exists"}


@pytest.mark.parametrize("name", ["admin", "reader"])
async def test_a_system_role_cannot_be_renamed(
    client: AsyncClient, db: Database, admin_headers: Headers, roles: dict[str, Role], name: str
) -> None:
    response = await client.patch(
        f"{API}/roles/{roles[name].id}", json={"name": "renamed"}, headers=admin_headers
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "System roles can't be renamed"}
    assert await db.roles.count_documents({"name": name}) == 1


async def test_admin_cannot_lose_permissions(
    client: AsyncClient, db: Database, admin_headers: Headers, roles: dict[str, Role]
) -> None:
    response = await client.patch(
        f"{API}/roles/{roles['admin'].id}", json={"permissions": []}, headers=admin_headers
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "The admin role always has every permission"}
    stored = await db.roles.find_one({"name": "admin"})
    assert stored is not None
    assert stored["permissions"] == sorted(Perm)


async def test_a_system_role_can_still_be_described_and_reader_tuned(
    client: AsyncClient, admin_headers: Headers, roles: dict[str, Role]
) -> None:
    admin = await client.patch(
        f"{API}/roles/{roles['admin'].id}", json={"description": "Runs it"}, headers=admin_headers
    )
    reader = await client.patch(
        f"{API}/roles/{roles['reader'].id}",
        json={"permissions": [Perm.LIKES_CREATE]},
        headers=admin_headers,
    )

    assert (admin.status_code, admin.json()["description"]) == (200, "Runs it")
    assert (reader.status_code, reader.json()["permissions"]) == (200, [Perm.LIKES_CREATE])


async def test_making_a_role_the_default_moves_the_flag(
    client: AsyncClient, db: Database, admin_headers: Headers
) -> None:
    role_id = (await _create_role(client, admin_headers)).json()["id"]

    response = await client.patch(
        f"{API}/roles/{role_id}", json={"is_default": True}, headers=admin_headers
    )

    assert response.status_code == 200
    assert response.json()["is_default"] is True
    # Exactly one default at any time.
    assert [role["name"] async for role in db.roles.find({"is_default": True})] == ["author"]


async def test_the_default_flag_cannot_simply_be_removed(
    client: AsyncClient, db: Database, admin_headers: Headers, roles: dict[str, Role]
) -> None:
    response = await client.patch(
        f"{API}/roles/{roles['reader'].id}", json={"is_default": False}, headers=admin_headers
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Make another role the default instead"}
    assert await db.roles.count_documents({"is_default": True}) == 1


async def test_changing_a_role_changes_what_its_holders_can_do_at_once(
    client: AsyncClient,
    db: Database,
    admin_headers: Headers,
    user_headers: Headers,
    roles: dict[str, Role],
) -> None:
    """No waiting for a token to expire: permissions are read on every request."""
    assert (await client.get(f"{API}/roles", headers=user_headers)).status_code == 403

    await client.patch(
        f"{API}/roles/{roles['reader'].id}",
        json={"permissions": [Perm.ROLES_READ]},
        headers=admin_headers,
    )
    assert (await client.get(f"{API}/roles", headers=user_headers)).status_code == 200

    await client.patch(
        f"{API}/roles/{roles['reader'].id}", json={"permissions": []}, headers=admin_headers
    )
    assert (await client.get(f"{API}/roles", headers=user_headers)).status_code == 403


# --- DELETE /roles/{id} ------------------------------------------------------


async def test_delete_a_role_removes_it_from_its_holders(
    client: AsyncClient, db: Database, admin_headers: Headers, make_user: MakeUser
) -> None:
    role_id = (await _create_role(client, admin_headers)).json()["id"]
    writer = await make_user("writer", "reader")
    await db.users.update_one({"_id": writer.id}, {"$push": {"role_ids": ObjectId(role_id)}})

    response = await client.delete(f"{API}/roles/{role_id}", headers=admin_headers)

    assert response.status_code == 204
    assert await db.roles.find_one({"_id": ObjectId(role_id)}) is None
    stored = await db.users.find_one({"_id": writer.id})
    assert stored is not None
    assert stored["role_ids"] == writer.role_ids  # the other role is kept


async def test_delete_a_role_that_does_not_exist(
    client: AsyncClient, admin_headers: Headers
) -> None:
    response = await client.delete(f"{API}/roles/{MISSING}", headers=admin_headers)

    assert response.status_code == 404


@pytest.mark.parametrize("name", ["admin", "reader"])
async def test_a_system_role_cannot_be_deleted(
    client: AsyncClient, db: Database, admin_headers: Headers, roles: dict[str, Role], name: str
) -> None:
    response = await client.delete(f"{API}/roles/{roles[name].id}", headers=admin_headers)

    assert response.status_code == 409
    assert response.json() == {"detail": "System roles can't be deleted"}
    assert await db.roles.count_documents({"name": name}) == 1


async def test_the_default_role_cannot_be_deleted(
    client: AsyncClient, db: Database, admin_headers: Headers
) -> None:
    role_id = (await _create_role(client, admin_headers)).json()["id"]
    await client.patch(f"{API}/roles/{role_id}", json={"is_default": True}, headers=admin_headers)

    response = await client.delete(f"{API}/roles/{role_id}", headers=admin_headers)

    assert response.status_code == 409
    assert response.json() == {"detail": "Make another role the default before deleting this one"}
    assert await db.roles.count_documents({"name": "author"}) == 1


# --- PUT /users/{id}/roles ---------------------------------------------------


async def _assign(client: AsyncClient, headers: Headers, user: User, *roles: Role) -> Response:
    return await client.put(
        f"{API}/users/{user.id}/roles",
        json={"role_ids": [str(role.id) for role in roles]},
        headers=headers,
    )


async def test_assign_roles(
    client: AsyncClient, db: Database, admin_headers: Headers, user: User, roles: dict[str, Role]
) -> None:
    response = await _assign(client, admin_headers, user, roles["admin"], roles["reader"])

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(user.id)
    assert sorted(role["name"] for role in body["roles"]) == ["admin", "reader"]
    assert "password_hash" not in body
    stored = await db.users.find_one({"_id": user.id})
    assert stored is not None
    assert set(stored["role_ids"]) == {roles["admin"].id, roles["reader"].id}


async def test_assign_roles_replaces_the_previous_set(
    client: AsyncClient, db: Database, admin_headers: Headers, user: User
) -> None:
    response = await _assign(client, admin_headers, user)

    assert response.status_code == 200
    assert response.json()["roles"] == []
    stored = await db.users.find_one({"_id": user.id})
    assert stored is not None
    assert stored["role_ids"] == []


async def test_assign_roles_to_a_user_who_does_not_exist(
    client: AsyncClient, admin_headers: Headers
) -> None:
    response = await client.put(
        f"{API}/users/{MISSING}/roles", json={"role_ids": []}, headers=admin_headers
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "User not found"}


async def test_assign_a_role_that_does_not_exist(
    client: AsyncClient, db: Database, admin_headers: Headers, user: User, roles: dict[str, Role]
) -> None:
    response = await client.put(
        f"{API}/users/{user.id}/roles",
        json={"role_ids": [str(roles["admin"].id), MISSING]},
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == [
        {"type": "value_error", "loc": ["body", "role_ids"], "msg": f"Unknown role: {MISSING}"}
    ]
    # Nothing was applied, the valid id included.
    stored = await db.users.find_one({"_id": user.id})
    assert stored is not None
    assert stored["role_ids"] == user.role_ids


async def test_assign_roles_with_invalid_input(
    client: AsyncClient, admin_headers: Headers, user: User
) -> None:
    response = await client.put(
        f"{API}/users/{user.id}/roles", json={"role_ids": "reader"}, headers=admin_headers
    )

    assert response.status_code == 422


async def test_assign_roles_ignores_duplicates(
    client: AsyncClient, db: Database, admin_headers: Headers, user: User, roles: dict[str, Role]
) -> None:
    reader = str(roles["reader"].id)

    response = await client.put(
        f"{API}/users/{user.id}/roles", json={"role_ids": [reader, reader]}, headers=admin_headers
    )

    assert response.status_code == 200
    stored = await db.users.find_one({"_id": user.id})
    assert stored is not None
    assert stored["role_ids"] == [roles["reader"].id]


async def test_a_user_cannot_make_themself_admin(
    client: AsyncClient, db: Database, user: User, user_headers: Headers, roles: dict[str, Role]
) -> None:
    """The obvious attack on any role system."""
    response = await _assign(client, user_headers, user, roles["admin"])

    assert response.status_code == 403
    stored = await db.users.find_one({"_id": user.id})
    assert stored is not None
    assert stored["role_ids"] == [roles["reader"].id]


async def test_the_last_active_admin_cannot_lose_the_admin_role(
    client: AsyncClient,
    db: Database,
    admin: User,
    admin_headers: Headers,
    make_user: MakeUser,
    roles: dict[str, Role],
) -> None:
    # Another admin exists but is deactivated, so doesn't count.
    await make_user("retired", "admin", is_active=False)

    response = await _assign(client, admin_headers, admin, roles["reader"])

    assert response.status_code == 409
    assert response.json() == {"detail": "This would leave no active admin"}
    stored = await db.users.find_one({"_id": admin.id})
    assert stored is not None
    assert stored["role_ids"] == [roles["admin"].id]


async def test_an_admin_can_step_down_when_another_remains(
    client: AsyncClient,
    admin: User,
    admin_headers: Headers,
    make_user: MakeUser,
    roles: dict[str, Role],
) -> None:
    await make_user("second", "admin")

    response = await _assign(client, admin_headers, admin, roles["reader"])

    assert response.status_code == 200
    # And having stepped down, the door is closed behind them.
    assert (await client.get(f"{API}/roles", headers=admin_headers)).status_code == 403


async def test_two_admins_removing_each_other_at_once_leave_one(
    client: AsyncClient,
    db: Database,
    make_user: MakeUser,
    headers_for: HeadersFor,
    roles: dict[str, Role],
) -> None:
    """Each request alone is fine; together they would leave nobody in charge."""
    for round_number in range(8):
        first = await make_user(f"first_{round_number}", "admin")
        second = await make_user(f"second_{round_number}", "admin")
        responses: list[Response] = []

        async def demote(actor: User, target: User, out: list[Response] = responses) -> None:
            out.append(await _assign(client, headers_for(actor), target, roles["reader"]))

        async with anyio.create_task_group() as group:
            group.start_soon(demote, first, second)
            group.start_soon(demote, second, first)

        # Exactly one request wins; the other is told why it was refused.
        assert sorted(response.status_code for response in responses) == [200, 409]
        still_admin = await db.users.count_documents(
            {"_id": {"$in": [first.id, second.id]}, "role_ids": roles["admin"].id}
        )
        assert still_admin == 1
        # The rule is about every admin there is, so a survivor of this round
        # would excuse both requests of the next one.
        await db.users.delete_many({})


# --- the default role ---------------------------------------------------------


async def test_admin_cannot_be_the_default_role(
    client: AsyncClient, db: Database, admin_headers: Headers, roles: dict[str, Role]
) -> None:
    """Or every stranger who signs up would be an administrator."""
    response = await client.patch(
        f"{API}/roles/{roles['admin'].id}", json={"is_default": True}, headers=admin_headers
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "The admin role can't be the default"}
    assert [role["name"] async for role in db.roles.find({"is_default": True})] == ["reader"]


async def test_two_roles_made_the_default_at_once_leave_exactly_one(
    client: AsyncClient, db: Database, admin_headers: Headers, make_role: MakeRole
) -> None:
    for round_number in range(8):
        first = await make_role(f"first_{round_number}")
        second = await make_role(f"second_{round_number}")

        async def make_default(role: Role) -> None:
            await client.patch(
                f"{API}/roles/{role.id}", json={"is_default": True}, headers=admin_headers
            )

        async with anyio.create_task_group() as group:
            group.start_soon(make_default, first)
            group.start_soon(make_default, second)

        defaults = [role["name"] async for role in db.roles.find({"is_default": True})]
        assert len(defaults) == 1, defaults


async def test_the_database_itself_refuses_a_second_default(db: Database) -> None:
    """The last line of defence, whatever the code above it does."""
    with pytest.raises(DuplicateKeyError):
        await db.roles.insert_one({"name": "second_default", "is_default": True})


# --- you can't grant what you don't hold --------------------------------------


@pytest.fixture
async def role_editor(make_role: MakeRole, make_user: MakeUser) -> User:
    """Holds `roles:create` and `roles:update`, plus one ordinary permission."""
    await make_role("role_editor", Perm.ROLES_CREATE, Perm.ROLES_UPDATE, Perm.POSTS_CREATE)
    return await make_user("eddie", "role_editor")


async def test_a_role_editor_cannot_put_more_into_a_role_than_they_hold(
    client: AsyncClient, db: Database, role_editor: User, headers_for: HeadersFor
) -> None:
    """The attack: hold `roles:update` only, and write yourself every permission."""
    own_role = await db.roles.find_one({"name": "role_editor"})
    assert own_role is not None

    response = await client.patch(
        f"{API}/roles/{own_role['_id']}",
        json={"permissions": sorted(Perm)},
        headers=headers_for(role_editor),
    )

    assert response.status_code == 403
    assert "users:assign_roles" in response.json()["detail"]
    unchanged = await db.roles.find_one({"name": "role_editor"})
    assert unchanged is not None
    assert unchanged["permissions"] == own_role["permissions"]


async def test_a_role_editor_cannot_create_a_role_with_more_than_they_hold(
    client: AsyncClient, db: Database, role_editor: User, headers_for: HeadersFor
) -> None:
    response = await _create_role(
        client, headers_for(role_editor), permissions=[Perm.POSTS_CREATE, Perm.USERS_DEACTIVATE]
    )

    assert response.status_code == 403
    assert await db.roles.count_documents({"name": "author"}) == 0


async def test_a_role_editor_can_grant_what_they_hold_and_remove_anything(
    client: AsyncClient, role_editor: User, headers_for: HeadersFor, roles: dict[str, Role]
) -> None:
    headers = headers_for(role_editor)

    created = await _create_role(client, headers, permissions=[Perm.POSTS_CREATE])
    # Reader's permissions are not ones the editor holds, yet taking them away
    # hands nobody anything.
    emptied = await client.patch(
        f"{API}/roles/{roles['reader'].id}", json={"permissions": []}, headers=headers
    )
    # Nor is re-sending permissions a role already has granting anything.
    described = await client.patch(
        f"{API}/roles/{roles['admin'].id}",
        json={"description": "Runs it", "permissions": sorted(Perm)},
        headers=headers,
    )

    assert (created.status_code, emptied.status_code, described.status_code) == (201, 200, 200)


@pytest.fixture
async def assigner(make_role: MakeRole, make_user: MakeUser) -> User:
    """Holds `users:assign_roles` and reader's permissions, nothing else."""
    await make_role("assigner", Perm.USERS_ASSIGN_ROLES, *READER_PERMISSIONS)
    return await make_user("ash", "assigner")


async def test_an_assigner_cannot_hand_out_a_role_stronger_than_themself(
    client: AsyncClient,
    db: Database,
    assigner: User,
    headers_for: HeadersFor,
    roles: dict[str, Role],
) -> None:
    """The attack: hold `users:assign_roles` only, and assign yourself admin."""
    response = await _assign(client, headers_for(assigner), assigner, roles["admin"])

    assert response.status_code == 403
    stored = await db.users.find_one({"_id": assigner.id})
    assert stored is not None
    assert roles["admin"].id not in stored["role_ids"]


async def test_an_assigner_cannot_take_a_stronger_role_away_either(
    client: AsyncClient,
    db: Database,
    assigner: User,
    admin: User,
    make_user: MakeUser,
    headers_for: HeadersFor,
    roles: dict[str, Role],
) -> None:
    await make_user("second", "admin")  # so the last-admin rule isn't what stops it

    response = await _assign(client, headers_for(assigner), admin, roles["reader"])

    assert response.status_code == 403
    stored = await db.users.find_one({"_id": admin.id})
    assert stored is not None
    assert stored["role_ids"] == [roles["admin"].id]


async def test_an_assigner_can_hand_out_roles_within_their_own_permissions(
    client: AsyncClient,
    assigner: User,
    user: User,
    make_user: MakeUser,
    headers_for: HeadersFor,
    roles: dict[str, Role],
) -> None:
    newcomer = await make_user("newcomer")

    given = await _assign(client, headers_for(assigner), newcomer, roles["reader"])
    taken = await _assign(client, headers_for(assigner), user)

    assert (given.status_code, taken.status_code) == (200, 200)
