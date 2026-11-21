"""Who is asking: the FastAPI side of authorization. The rules are in ``policy``."""

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends

from app.auth.dependencies import CurrentUserDep
from app.core.database import DbDep
from app.core.errors import ForbiddenError
from app.rbac.permissions import Perm
from app.rbac.policy import FORBIDDEN, Principal
from app.rbac.providers import RoleRepositoryDep, get_role_repository
from app.rbac.service import RoleService
from app.users.dependencies import UserRepositoryDep

__all__ = [
    "PrincipalDep",
    "RoleRepositoryDep",
    "RoleServiceDep",
    "get_principal",
    "get_role_repository",
    "get_role_service",
    "require_permission",
]


async def get_role_service(
    db: DbDep, roles: RoleRepositoryDep, users: UserRepositoryDep
) -> RoleService:
    return RoleService(db, roles, users)


RoleServiceDep = Annotated[RoleService, Depends(get_role_service)]


async def get_principal(user: CurrentUserDep, roles: RoleRepositoryDep) -> Principal:
    """
    Loaded on every request, like the user: changing a role, or which roles
    someone has, takes effect on their very next request. A role id that no
    longer matches a role is simply skipped.
    """
    held = await roles.get_many(user.role_ids)
    permissions = frozenset(permission for role in held for permission in role.permissions)
    return Principal(user=user, roles=held, permissions=permissions)


PrincipalDep = Annotated[Principal, Depends(get_principal)]


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
