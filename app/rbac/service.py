"""Managing roles, and deciding which roles a user has."""

from collections.abc import Awaitable, Iterable, Sequence
from typing import Protocol

from bson import ObjectId

from app.core.database import Database, Session, in_transaction
from app.core.errors import ConflictError, FieldError, ForbiddenError, NotFoundError
from app.rbac.models import Role
from app.rbac.permissions import ADMIN_ROLE
from app.rbac.policy import FORBIDDEN, Principal, ensure_can_grant
from app.rbac.repository import DuplicateRoleError, RoleRepository
from app.rbac.schemas import RoleCreate, RoleUpdate
from app.users.models import User
from app.users.repository import UserRepository

_NAME_TAKEN = "A role with this name already exists"
_NOT_FOUND = "Role not found"


class StatusChangeStep(Protocol):
    """Something to do when an account is switched off or on, inside the same transaction."""

    def __call__(self, user_id: ObjectId, *, session: Session) -> Awaitable[None]: ...


class RoleService:
    def __init__(
        self,
        db: Database,
        roles: RoleRepository,
        users: UserRepository,
        on_status_change: Sequence[StatusChangeStep] = (),
    ) -> None:
        self._db = db
        self._roles = roles
        self._users = users
        # Ending the account's sessions is the auth feature's business. It is
        # registered in `get_role_service`; this service knows only that
        # something may have to happen.
        self._on_status_change = on_status_change

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
        deactivated) must call this first, inside its transaction.

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

    async def set_active(
        self, principal: Principal, user_id: ObjectId, *, active: bool
    ) -> tuple[User, list[Role]]:
        """
        Deactivate an account, or bring it back. Returns the user and their roles.

        A deactivated account can't log in or use a token, from the very next
        request. Its sessions are ended for good, so they don't come back to
        life if the account does.
        """
        if user_id == principal.user.id:
            # Not even an admin with others beside them: an account that
            # locks itself out can't undo it.
            raise ConflictError("You can't change your own account's status")
        admin = await self._admin_role()

        async def work(session: Session) -> tuple[User, list[Role]]:
            user = await self._users.get(user_id, session=session)
            if user is None:
                raise NotFoundError("User not found")
            roles = await self._roles.get_many(user.role_ids, session=session)
            # Switching someone off, or back on, is power over everything
            # they may do. Without this, the permission to deactivate would
            # be the permission to remove every administrator but one.
            try:
                ensure_can_grant(
                    principal, {permission for role in roles for permission in role.permissions}
                )
            except ForbiddenError:
                # That error lists the permissions the caller lacks, which
                # here would be a description of the account they asked
                # about. A plain refusal says nothing about it.
                raise ForbiddenError(FORBIDDEN) from None
            if user.is_active == active:
                return user, roles
            if not active and admin.id in user.role_ids:
                await self.ensure_another_active_admin(user.id, session=session)
            # In both directions. Switching off ends the sessions there are.
            # A login can still slip one in at that very moment; it is
            # useless while the account is off, and switching the account
            # back on ends it too, so nothing from before comes back.
            for step in self._on_status_change:
                await step(user.id, session=session)
            updated = await self._users.set_active(user.id, active, session=session)
            if updated is None:
                raise NotFoundError("User not found")
            return updated, roles

        return await in_transaction(self._db, work)

    async def roles_of(self, users: Iterable[User]) -> dict[ObjectId, list[Role]]:
        """Each user's roles, by user id: one query for a whole page of users."""
        users = list(users)
        # `found` comes back by name, and each user's roles keep that order:
        # the same as everywhere else roles are shown.
        found = await self._roles.get_many({role_id for user in users for role_id in user.role_ids})
        return {user.id: [role for role in found if role.id in user.role_ids] for user in users}

    async def _admin_role(self) -> Role:
        admin = await self._roles.get_by_name(ADMIN_ROLE)
        if admin is None:
            # Startup creates it and the API can't delete it, so this means
            # the database was changed by hand. Better a loud error than a
            # safety rule that quietly stopped applying.
            raise RuntimeError("The admin role is missing; restart the app to recreate it")
        return admin
