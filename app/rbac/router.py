from typing import Annotated

from fastapi import APIRouter, Depends, Path, status

from app.core.errors import error_responses
from app.core.types import PyObjectId
from app.rbac.dependencies import RoleServiceDep, require_permission
from app.rbac.models import Role
from app.rbac.permissions import DESCRIPTIONS, Perm
from app.rbac.policy import Principal
from app.rbac.schemas import PermissionPublic, RoleCreate, RolePublic, RoleUpdate

# Every route here needs a login and a permission, so each can answer these.
_AUTH_ERRORS = error_responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)

permissions_router = APIRouter(prefix="/permissions", tags=["roles"], responses=_AUTH_ERRORS)
roles_router = APIRouter(prefix="/roles", tags=["roles"], responses=_AUTH_ERRORS)

_CAN_READ = [Depends(require_permission(Perm.ROLES_READ))]
RoleId = Annotated[PyObjectId, Path(description="The role's id.")]
# As a parameter rather than in `dependencies=`: these routes need to know
# who is asking, to apply "you can't grant what you don't hold".
RoleCreator = Annotated[Principal, Depends(require_permission(Perm.ROLES_CREATE))]
RoleEditor = Annotated[Principal, Depends(require_permission(Perm.ROLES_UPDATE))]


@permissions_router.get("", dependencies=_CAN_READ)
async def list_permissions() -> list[PermissionPublic]:
    """Every permission that exists. They are fixed in the code; roles are built from them."""
    # Straight from the catalog: startup keeps the database copy identical.
    return [
        PermissionPublic(id=perm, resource=perm.resource, description=DESCRIPTIONS[perm])
        for perm in sorted(Perm)
    ]


@roles_router.get("", response_model=list[RolePublic], dependencies=_CAN_READ)
async def list_roles(service: RoleServiceDep) -> list[Role]:
    """All roles, by name."""
    return await service.list_roles()


@roles_router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=RolePublic,
    responses=error_responses(status.HTTP_409_CONFLICT),
)
async def create_role(data: RoleCreate, principal: RoleCreator, service: RoleServiceDep) -> Role:
    """Create a role. It may only contain permissions you hold yourself."""
    return await service.create_role(principal, data)


@roles_router.get(
    "/{role_id}",
    response_model=RolePublic,
    dependencies=_CAN_READ,
    responses=error_responses(status.HTTP_404_NOT_FOUND),
)
async def read_role(role_id: RoleId, service: RoleServiceDep) -> Role:
    """One role."""
    return await service.get_role(role_id)


@roles_router.patch(
    "/{role_id}",
    response_model=RolePublic,
    responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
)
async def update_role(
    role_id: RoleId, data: RoleUpdate, principal: RoleEditor, service: RoleServiceDep
) -> Role:
    """
    Change the fields sent; a field left out or sent as null stays as it is.

    You may only add permissions you hold yourself. System roles keep their
    name and `admin` keeps every permission. `is_default: true` moves the
    default from the role that had it; `admin` can't be the default.
    """
    return await service.update_role(principal, role_id, data)


@roles_router.delete(
    "/{role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission(Perm.ROLES_DELETE))],
    responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
)
async def delete_role(role_id: RoleId, service: RoleServiceDep) -> None:
    """Delete a role and take it away from everyone who had it."""
    await service.delete_role(role_id)
