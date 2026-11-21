from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, Response, status
from fastapi.security import OAuth2PasswordRequestForm

from app.auth.dependencies import AuthServiceDep, CurrentUserDep, verify_origin
from app.auth.schemas import RegisterRequest, TokenResponse
from app.auth.service import IssuedTokens
from app.core.errors import error_responses
from app.core.settings import Settings, SettingsDep
from app.core.settings.security import (
    REFRESH_COOKIE_NAME,
    REFRESH_COOKIE_PATH,
    REFRESH_COOKIE_SAMESITE,
)
from app.users.models import User
from app.users.schemas import UserAccount

router = APIRouter(prefix="/auth", tags=["auth"])

# An OAuth2 "password" form, not JSON: fields `username` and `password`. That
# is the format the /docs "Authorize" button sends.
LoginForm = Annotated[OAuth2PasswordRequestForm, Depends()]
# The alias ties the cookie's name to the constant it is set with.
RefreshCookie = Annotated[str | None, Cookie(alias=REFRESH_COOKIE_NAME)]
# Browsers set this header themselves; hide it from the /docs form.
UserAgent = Annotated[str | None, Header(include_in_schema=False)]


def _respond_with_tokens(
    response: Response, tokens: IssuedTokens, settings: Settings
) -> TokenResponse:
    """Access token in the JSON body, refresh token in the cookie."""
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        tokens.refresh_token,
        max_age=settings.refresh_token_ttl_days * 24 * 60 * 60,
        path=REFRESH_COOKIE_PATH,  # sent to the auth routes only
        httponly=True,  # page JavaScript can't read it, so XSS can't steal it
        secure=settings.https_only,  # HTTPS only, except in debug
        samesite=REFRESH_COOKIE_SAMESITE,  # not sent on cross-site requests
    )
    # Tokens must never be stored by a browser or proxy cache (RFC 6749, 5.1).
    response.headers["Cache-Control"] = "no-store"
    return TokenResponse(access_token=tokens.access_token, expires_in=tokens.expires_in)


def _clear_cookie(response: Response, settings: Settings) -> None:
    # A browser only deletes a cookie when these attributes match the ones it
    # was set with.
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.https_only,
        samesite=REFRESH_COOKIE_SAMESITE,
    )


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=UserAccount,
    responses=error_responses(status.HTTP_409_CONFLICT),
)
async def register(data: RegisterRequest, service: AuthServiceDep) -> User:
    """Create an account. Log in afterwards to get tokens."""
    return await service.register(data)


@router.post("/login", responses=error_responses(status.HTTP_401_UNAUTHORIZED))
async def login(
    form: LoginForm,
    response: Response,
    service: AuthServiceDep,
    settings: SettingsDep,
    user_agent: UserAgent = None,
) -> TokenResponse:
    """Log in with an email or username. Sets the refresh-token cookie."""
    tokens = await service.login(form.username, form.password, user_agent)
    return _respond_with_tokens(response, tokens, settings)


@router.post(
    "/refresh",
    dependencies=[Depends(verify_origin)],
    responses=error_responses(status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
)
async def refresh(
    response: Response,
    service: AuthServiceDep,
    settings: SettingsDep,
    refresh_token: RefreshCookie = None,
    user_agent: UserAgent = None,
) -> TokenResponse:
    """Get a new access token, and a new refresh token in the cookie."""
    tokens = await service.refresh(refresh_token, user_agent)
    return _respond_with_tokens(response, tokens, settings)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(verify_origin)],
    responses=error_responses(status.HTTP_403_FORBIDDEN),
)
async def logout(
    response: Response,
    service: AuthServiceDep,
    settings: SettingsDep,
    refresh_token: RefreshCookie = None,
) -> None:
    """End this session: revoke its refresh tokens and clear the cookie."""
    await service.logout(refresh_token)
    _clear_cookie(response, settings)


@router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(status.HTTP_401_UNAUTHORIZED),
)
async def logout_all(
    user: CurrentUserDep,
    response: Response,
    service: AuthServiceDep,
    settings: SettingsDep,
) -> None:
    """End every session of the current user, on every device."""
    await service.logout_all(user.id)
    _clear_cookie(response, settings)
