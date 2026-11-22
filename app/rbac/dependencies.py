"""Who is asking: the FastAPI side of authorization. The rules are in ``policy``."""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends

from app.auth.dependencies import CurrentUserDep, OptionalUserDep
from app.auth.repository import RefreshTokenRepository
from app.core.database import DbDep
from app.core.errors import ForbiddenError
from app.rbac.permissions import Perm
from app.rbac.policy import FORBIDDEN, Principal
from app.rbac.providers import RoleRepositoryDep, get_role_repository
from app.rbac.repository import RoleRepository
from app.rbac.service import RoleService
from app.users.dependencies import UserRepositoryDep
from app.users.models import User

__all__ = [
    "LOGIN_OPTIONAL",
    "OptionalPrincipalDep",
    "PrincipalDep",
    "RoleRepositoryDep",
    "RoleServiceDep",
    "get_optional_principal",
    "get_principal",
    "get_role_repository",
    "get_role_service",
    "require_permission",
]


async def get_role_service(
    db: DbDep, roles: RoleRepositoryDep, users: UserRepositoryDep
) -> RoleService:
    # Here is where "switching an account off or on ends its sessions" is wired up.
    return RoleService(
        db, roles, users, on_status_change=[RefreshTokenRepository(db).revoke_all_for_user]
    )


RoleServiceDep = Annotated[RoleService, Depends(get_role_service)]


async def get_principal(user: CurrentUserDep, roles: RoleRepositoryDep) -> Principal:
    """
    Loaded on every request, like the user: changing a role, or which roles
    someone has, takes effect on their very next request. A role id that no
    longer matches a role is simply skipped.
    """
    return await _principal_of(user, roles)


PrincipalDep = Annotated[Principal, Depends(get_principal)]


async def get_optional_principal(
    user: OptionalUserDep, roles: RoleRepositoryDep
) -> Principal | None:
    """The principal, or None for an anonymous visitor. For public routes."""
    return await _principal_of(user, roles) if user else None


OptionalPrincipalDep = Annotated[Principal | None, Depends(get_optional_principal)]

# Pass as `openapi_extra=` on a route that takes `OptionalPrincipalDep`. In the
# API docs it marks the route as usable with or without a login: FastAPI adds
# this empty entry to the login the route already lists, and an empty entry
# means "no credentials" is one of the accepted ways in. Without it the docs
# show a padlock on a public route.
LOGIN_OPTIONAL: dict[str, Any] = {"security": [{}]}


async def _principal_of(user: User, roles: RoleRepository) -> Principal:
    held = await roles.get_many(user.role_ids)
    permissions = frozenset(permission for role in held for permission in role.permissions)
    return Principal(user=user, roles=held, permissions=permissions)


def require_permission(
    permission: Perm,
) -> Callable[[Principal], Coroutine[Any, Any, Principal]]:
    """
    A dependency that lets the request through only with this permission, for
    actions on things without an owner. When the route doesn't need to know
    who is asking::

        @router.get("", dependencies=[Depends(require_permission(Perm.ROLES_READ))])

    and when it does, as a parameter, which also hands over the principal::

        principal: Annotated[Principal, Depends(require_permission(Perm.ROLES_CREATE))]
    """

    async def dependency(principal: PrincipalDep) -> Principal:
        if not principal.has(permission):
            raise ForbiddenError(FORBIDDEN)
        return principal

    return dependency
