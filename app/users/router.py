from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Response, UploadFile, status

from app.core.caching import never_cache
from app.core.errors import error_responses
from app.core.pagination import Page
from app.core.settings.security import AVATAR_MAX_BODY_SIZE, AVATAR_MAX_BYTES
from app.core.types import PyObjectId

# Import rule between features: auth and rbac may import anything from users.
# Users imports from them only here in the router, and only their
# `dependencies` modules plus modules that import nothing from users back
# (`rbac.permissions`, `rbac.policy`). One direction is what prevents a
# circular import.
from app.rbac.dependencies import PrincipalDep, RoleServiceDep, require_permission
from app.rbac.permissions import Perm
from app.rbac.policy import Principal
from app.users.avatars import AVATAR_KEY_PATTERN, MEDIA_TYPE
from app.users.dependencies import UserServiceDep
from app.users.models import User
from app.users.schemas import (
    AVATAR_URL_PREFIX,
    AssignRolesRequest,
    ProfileUpdate,
    StatusUpdate,
    UserAdmin,
    UserListParams,
    UserMe,
    UserPublic,
)

router = APIRouter(prefix="/users", tags=["users"])
# Avatars are files, not API resources: served from outside the versioned API.
media_router = APIRouter(prefix=AVATAR_URL_PREFIX, tags=["users"])

UserId = Annotated[PyObjectId, Path(description="The user's id.")]

_ADMIN_ERRORS = error_responses(
    status.HTTP_401_UNAUTHORIZED,
    status.HTTP_403_FORBIDDEN,
    status.HTTP_404_NOT_FOUND,
    status.HTTP_409_CONFLICT,
)

# A name is made once and never reused, so the picture behind it never
# changes: caches may keep it for as long as they like.
_AVATAR_CACHING = "public, max-age=31536000, immutable"


# `response_model` does the filtering: the internal User goes in with all its
# fields, and only UserMe's come out, so `password_hash` is left behind. The
# same happens to each role, cut down to its id and name.
def _me(user: User, principal: Principal) -> dict[str, Any]:
    return {
        **user.model_dump(),
        "roles": principal.roles,
        "permissions": sorted(principal.permissions),
    }


@router.get("/me", response_model=UserMe, responses=error_responses(status.HTTP_401_UNAUTHORIZED))
async def read_me(principal: PrincipalDep) -> dict[str, Any]:
    """The account the access token belongs to, with its roles and permissions."""
    return _me(principal.user, principal)


@router.patch(
    "/me",
    response_model=UserMe,
    responses=error_responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_409_CONFLICT),
)
async def update_me(
    data: ProfileUpdate, principal: PrincipalDep, service: UserServiceDep
) -> dict[str, Any]:
    """
    Change your username, display name or bio. A field left out or sent as
    null stays as it is; an empty display name or bio clears it.

    Changing your username changes the address of your profile, and frees the
    old name for anyone to take.
    """
    return _me(await service.update_profile(principal.user, data), principal)


@router.put(
    "/me/avatar",
    response_model=UserMe,
    responses={
        **error_responses(status.HTTP_401_UNAUTHORIZED),
        # These two can also come from the framework before the route runs,
        # and then as plain text, not in the shape the app's own errors have.
        status.HTTP_400_BAD_REQUEST: {"description": "More than the one file was sent."},
        status.HTTP_413_CONTENT_TOO_LARGE: {"description": "The file is larger than 2 MB."},
    },
    # The app's default body limit is far below a photo. This route alone
    # accepts more, and nothing but the one file.
    max_body_size=AVATAR_MAX_BODY_SIZE,
    form_limits={"max_files": 1, "max_fields": 0},
)
async def upload_avatar(
    file: UploadFile, principal: PrincipalDep, service: UserServiceDep
) -> dict[str, Any]:
    """
    Set your avatar: a PNG, JPEG or WebP of at most 2 MB, sent as the form
    field `file`. It is cropped to a square and stored as a 256-pixel WebP;
    nothing else of the uploaded file is kept.
    """
    # One byte more than allowed is enough to know it is too big, without
    # holding the rest in memory.
    upload = await file.read(AVATAR_MAX_BYTES + 1)
    return _me(await service.set_avatar(principal.user, upload), principal)


@router.delete(
    "/me/avatar",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(status.HTTP_401_UNAUTHORIZED),
)
async def delete_avatar(principal: PrincipalDep, service: UserServiceDep) -> None:
    """Remove your avatar. Does nothing if you have none."""
    await service.remove_avatar(principal.user)


@router.get(
    "",
    response_model=Page[UserAdmin],
    dependencies=[Depends(require_permission(Perm.USERS_READ_ANY))],
    responses=error_responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
)
async def list_users(
    params: Annotated[UserListParams, Query()],
    service: UserServiceDep,
    role_service: RoleServiceDep,
    response: Response,
) -> dict[str, Any]:
    """Every account with its roles, newest first. For administrators."""
    users, next_cursor = await service.list_accounts(
        is_active=params.is_active, after=params.after(), limit=params.limit
    )
    roles = await role_service.roles_of(users)
    never_cache(response)  # emails, for one reader's eyes
    return {
        "items": [{**user.model_dump(), "roles": roles[user.id]} for user in users],
        "next_cursor": next_cursor,
    }


@router.get(
    "/{username}",
    response_model=UserPublic,
    responses=error_responses(status.HTTP_404_NOT_FOUND),
)
async def read_profile(
    username: Annotated[str, Path(max_length=64, description="The user's username.")],
    service: UserServiceDep,
) -> User:
    """Someone's public profile. A deactivated account has none."""
    return await service.get_public(username)


@router.patch("/{user_id}/status", response_model=UserAdmin, responses=_ADMIN_ERRORS)
async def set_status(
    user_id: UserId,
    data: StatusUpdate,
    principal: Annotated[Principal, Depends(require_permission(Perm.USERS_DEACTIVATE))],
    service: RoleServiceDep,
    response: Response,
) -> dict[str, Any]:
    """
    Deactivate an account, or bring it back.

    A deactivated account can't log in, and its sessions are ended. You need
    to hold every permission the account holds, you can't do this to
    yourself, and the last active admin can't be deactivated.
    """
    user, roles = await service.set_active(principal, user_id, active=data.is_active)
    never_cache(response)  # the answer carries an email
    return {**user.model_dump(), "roles": roles}


@router.put("/{user_id}/roles", response_model=UserAdmin, responses=_ADMIN_ERRORS)
async def assign_roles(
    user_id: UserId,
    data: AssignRolesRequest,
    principal: Annotated[Principal, Depends(require_permission(Perm.USERS_ASSIGN_ROLES))],
    service: RoleServiceDep,
    response: Response,
) -> dict[str, Any]:
    """
    Replace a user's roles with the given set.

    You may only give or take away roles whose permissions you hold yourself,
    and the last active admin can't be demoted.
    """
    user, roles = await service.assign_roles(principal, user_id, data.role_ids)
    never_cache(response)  # the answer carries an email
    return {**user.model_dump(), "roles": roles}


@media_router.get(
    "/{avatar_key}",
    response_class=Response,
    responses={
        status.HTTP_200_OK: {"content": {MEDIA_TYPE: {}}, "description": "The picture."},
        **error_responses(status.HTTP_404_NOT_FOUND),
    },
)
async def read_avatar(
    # Exactly the names this app makes. Anything else, "../" included, never
    # reaches the storage.
    avatar_key: Annotated[str, Path(pattern=AVATAR_KEY_PATTERN)],
    service: UserServiceDep,
) -> Response:
    """An avatar, as addressed by `avatar_url`."""
    picture = await service.read_avatar(avatar_key)
    return Response(picture, media_type=MEDIA_TYPE, headers={"Cache-Control": _AVATAR_CACHING})
