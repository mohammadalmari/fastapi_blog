import logging
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import OAuth2PasswordBearer

from app.auth.repository import RefreshTokenRepository
from app.auth.service import AuthService
from app.core.database import DbDep
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.logs import SECURITY_LOGGER
from app.core.security import decode_access_token
from app.core.settings import SettingsDep
from app.core.settings.environment import API_PREFIX
from app.rbac.providers import RoleRepositoryDep
from app.users.dependencies import UserRepositoryDep
from app.users.models import User

security_log = logging.getLogger(SECURITY_LOGGER)

# The rejected value is attacker-controlled; keep log lines bounded.
LOGGED_ORIGIN_MAX_LENGTH = 200


# Provider functions: they build the plain classes, which themselves know
# nothing about FastAPI. `async def` keeps them out of the thread pool.
async def get_refresh_token_repository(db: DbDep) -> RefreshTokenRepository:
    return RefreshTokenRepository(db)


async def get_auth_service(
    users: UserRepositoryDep,
    refresh_tokens: Annotated[RefreshTokenRepository, Depends(get_refresh_token_repository)],
    roles: RoleRepositoryDep,
    settings: SettingsDep,
) -> AuthService:
    return AuthService(users, refresh_tokens, roles, settings)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]

# Reads "Authorization: Bearer <token>" and answers 401 when it is missing.
# `tokenUrl` tells /docs where its "Authorize" button should send the login.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{API_PREFIX}/auth/login")


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    users: UserRepositoryDep,
    settings: SettingsDep,
) -> User:
    """
    The user the access token belongs to.

    The user is loaded from the database on every request, so deactivating an
    account takes effect at once rather than when the token expires.
    """
    user_id = decode_access_token(token, settings)
    user = await users.get(user_id) if user_id else None
    if user is None or not user.is_active:
        raise UnauthorizedError("Could not validate credentials")
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]

# The same scheme, except that a request without a token is let through. Both
# go by one name in the API docs on purpose: they are one way of logging in.
_optional_oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{API_PREFIX}/auth/login", auto_error=False
)


async def get_optional_user(
    token: Annotated[str | None, Depends(_optional_oauth2_scheme)],
    users: UserRepositoryDep,
    settings: SettingsDep,
) -> User | None:
    """
    For routes that work without a login but show more with one.

    No token means an anonymous visitor. A token that is present but bad is
    still a 401, not treated as "no token": a client whose token has expired
    needs to be told, so that it refreshes instead of quietly seeing less.
    """
    if token is None:
        return None
    return await get_current_user(token, users, settings)


OptionalUserDep = Annotated[User | None, Depends(get_optional_user)]


async def verify_origin(
    request: Request,
    settings: SettingsDep,
    # Browsers set this header themselves; hide it from the /docs form.
    origin: Annotated[str | None, Header(include_in_schema=False)] = None,
) -> None:
    """
    CSRF protection for the routes that authenticate with the cookie.

    A browser attaches cookies by itself, so another site could make a
    visitor's browser call these routes. It can't forge the ``Origin`` header
    though, which names the site the request came from. Requests without one
    (curl, mobile apps, server-to-server) aren't browser cross-site requests.
    """
    if origin is None or settings.debug:
        return
    own_origin = f"{request.url.scheme}://{request.url.netloc}"
    if origin.rstrip("/") not in {own_origin, *settings.cors_allowed_origins}:
        security_log.warning(
            "Rejected %s from origin %r", request.url.path, origin[:LOGGED_ORIGIN_MAX_LENGTH]
        )
        raise ForbiddenError("Origin not allowed")
