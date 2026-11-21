"""Managing roles, and deciding which roles a user has."""

from collections.abc import Sequence

from bson import ObjectId

from app.core.database import Database, Session, in_transaction
from app.core.errors import ConflictError, FieldError, NotFoundError
from app.rbac.models import Role
from app.rbac.permissions import ADMIN_ROLE
from app.rbac.policy import Principal, ensure_can_grant
from app.rbac.repository import DuplicateRoleError, RoleRepository
from app.rbac.schemas import RoleCreate, RoleUpdate
from app.users.models import User
from app.users.repository import UserRepository

_NAME_TAKEN = "A role with this name already exists"
_NOT_FOUND = "Role not found"


class RoleService:
    def __init__(self, db: Database, roles: RoleRepository, users: UserRepository) -> None:
        self._db = db
        self._roles = roles
        self._users = users

    async def list_roles(self) -> list[Role]:
        return await self._roles.list_all()

    async def get_role(self, role_id: ObjectId) -> Role:
        role = await self._roles.get(role_id)
        if role is None:
            raise NotFoundError(_NOT_FOUND)
        return role

    async def create_role(self, principal: Principal, data: RoleCreate) -> Role:
        ensure_can_grant(principal, data.permissions)
        try:
            return await self._roles.create(
                name=data.name, description=data.description, permissions=data.permissions
            )
        except DuplicateRoleError as error:
            raise ConflictError(_NAME_TAKEN) from error

    async def update_role(self, principal: Principal, role_id: ObjectId, data: RoleUpdate) -> Role:
        role = await self.get_role(role_id)
        # Only what the client sent and that differs: a field left out, sent
        # as null, or sent with its current value changes nothing.
        sent = data.model_dump(exclude_none=True, exclude={"is_default"})
        changes = {field: value for field, value in sent.items() if value != getattr(role, field)}

        if role.is_system and "name" in changes:
            raise ConflictError("System roles can't be renamed")
        if role.name == ADMIN_ROLE and "permissions" in changes:
            raise ConflictError("The admin role always has every permission")
        if "permissions" in changes:
            # Only what is being added counts: taking a permission away, or
            # re-sending one the role already has, gives nobody anything.
            ensure_can_grant(principal, set(changes["permissions"]) - set(role.permissions))
        if data.is_default is False and role.is_default:
            # Clearing the flag would leave new accounts with no role at all.
            raise ConflictError("Make another role the default instead")
        if data.is_default and role.name == ADMIN_ROLE:
            # Sign-up is open to anyone, and the default role is what they get.
            raise ConflictError("The admin role can't be the default")

        if changes:
            try:
                updated = await self._roles.update(role_id, changes)
            except DuplicateRoleError as error:
                raise ConflictError(_NAME_TAKEN) from error
            if updated is None:  # deleted since we loaded it
                raise NotFoundError(_NOT_FOUND)
            role = updated
        if data.is_default and not role.is_default:
            role = await self._make_default(role_id)
        return role

    async def _make_default(self, role_id: ObjectId) -> Role:
        async def work(session: Session) -> Role:
            role = await self._roles.move_default_to(role_id, session=session)
            if role is None:
                raise NotFoundError(_NOT_FOUND)
            return role

        # A transaction, because "exactly one default" spans two documents:
        # the role losing the flag and the one gaining it. Two requests moving
        # it at the same time write to the same documents, so MongoDB makes
        # them take turns instead of letting each undo half of the other.
        return await in_transaction(self._db, work)

    async def delete_role(self, role_id: ObjectId) -> None:
        async def work(session: Session) -> None:
            # The conditions are part of the delete itself, so they can't have
            # become untrue between checking and deleting.
            if not await self._roles.delete_ordinary(role_id, session=session):
                role = await self._roles.get(role_id, session=session)
                if role is None:
                    raise NotFoundError(_NOT_FOUND)
                if role.is_system:
                    raise ConflictError("System roles can't be deleted")
                raise ConflictError("Make another role the default before deleting this one")
            await self._users.remove_role(role_id, session=session)

        # Both steps or neither. One gap remains and is accepted: a role
        # assigned to someone at the very moment it is deleted can leave its
        # id on that user. Such an id matches no role and grants nothing.
        await in_transaction(self._db, work)

    async def ensure_another_active_admin(self, user_id: ObjectId, *, session: Session) -> None:
        """
        Refuse, unless some active admin other than ``user_id`` exists.

        Somebody must always be able to administer the app. Anything that
        stops a user being an active admin (losing the role here; being
        deactivated, later) must call this first, inside its transaction.

        "Is there another admin?" followed by "remove this one" is two steps,
        and two requests removing two different admins could each pass the
        check before either wrote. Locking the admin role first makes such
        requests take turns, each seeing what the previous one did.
        """
        admin = await self._admin_role()
        await self._roles.lock(admin.id, session=session)
        others = await self._users.count_active_with_role(
            admin.id, excluding=user_id, session=session
        )
        if others == 0:
            raise ConflictError("This would leave no active admin")

    async def assign_roles(
        self, principal: Principal, user_id: ObjectId, role_ids: Sequence[ObjectId]
    ) -> tuple[User, list[Role]]:
        """Replace the user's roles. Returns the user and their new roles."""
        wanted = list(dict.fromkeys(role_ids))  # no duplicates, order kept
        admin = await self._admin_role()

        async def work(session: Session) -> tuple[User, list[Role]]:
            user = await self._users.get(user_id, session=session)
            if user is None:
                raise NotFoundError("User not found")
            roles = await self._roles.get_many(wanted, session=session)
            found = {role.id for role in roles}
            unknown = [role_id for role_id in wanted if role_id not in found]
            if unknown:
                raise FieldError("role_ids", [f"Unknown role: {role_id}" for role_id in unknown])

            # Giving a role and taking one away are both exercises of the
            # power that role carries, so both need the caller to hold it.
            removed = await self._roles.get_many(set(user.role_ids) - found, session=session)
            added = [role for role in roles if role.id not in user.role_ids]
            for role in (*added, *removed):
                ensure_can_grant(principal, role.permissions)

            if user.is_active and admin.id in user.role_ids and admin.id not in found:
                await self.ensure_another_active_admin(user.id, session=session)

            # Stored in the order `roles` came back in (by name), so the same
            # set is always stored the same way.
            updated = await self._users.set_roles(
                user.id, [role.id for role in roles], session=session
            )
            return updated or user, roles

        return await in_transaction(self._db, work)

    async def _admin_role(self) -> Role:
        admin = await self._roles.get_by_name(ADMIN_ROLE)
        if admin is None:
            # Startup creates it and the API can't delete it, so this means
            # the database was changed by hand. Better a loud error than a
            # safety rule that quietly stopped applying.
            raise RuntimeError("The admin role is missing; restart the app to recreate it")
        return admin
