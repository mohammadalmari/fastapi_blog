from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, status

# Import rule between features: auth and rbac may import anything from users.
# Users imports from them only here in the router, and only their
# `dependencies` modules plus modules that import nothing from users back
# (`rbac.permissions`, `rbac.policy`). One direction is what prevents a
# circular import.
from app.core.errors import error_responses
from app.core.types import PyObjectId
from app.rbac.dependencies import PrincipalDep, RoleServiceDep, require_permission
from app.rbac.permissions import Perm
from app.rbac.policy import Principal
from app.users.schemas import AssignRolesRequest, UserAdmin, UserMe

router = APIRouter(prefix="/users", tags=["users"])

UserId = Annotated[PyObjectId, Path(description="The user's id.")]


# `response_model` does the filtering: the internal User goes in with all its
# fields, and only UserMe's come out, so `password_hash` is left behind. The
# same happens to each role, cut down to its id and name.
@router.get("/me", response_model=UserMe, responses=error_responses(status.HTTP_401_UNAUTHORIZED))
async def read_me(principal: PrincipalDep) -> dict[str, Any]:
    """The account the access token belongs to, with its roles and permissions."""
    return {
        **principal.user.model_dump(),
        "roles": principal.roles,
        "permissions": sorted(principal.permissions),
    }


@router.put(
    "/{user_id}/roles",
    response_model=UserAdmin,
    responses=error_responses(
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
        status.HTTP_409_CONFLICT,
    ),
)
async def assign_roles(
    user_id: UserId,
    data: AssignRolesRequest,
    principal: Annotated[Principal, Depends(require_permission(Perm.USERS_ASSIGN_ROLES))],
    service: RoleServiceDep,
) -> dict[str, Any]:
    """
    Replace a user's roles with the given set.

    You may only give or take away roles whose permissions you hold yourself,
    and the last active admin can't be demoted.
    """
    user, roles = await service.assign_roles(principal, user_id, data.role_ids)
    return {**user.model_dump(), "roles": roles}
