"""Sign-up, login and the refresh-token lifecycle."""

import logging
from dataclasses import dataclass
from datetime import timedelta

from bson import ObjectId

from app.auth.repository import RefreshTokenRepository
from app.auth.schemas import RegisterRequest
from app.core.database import utcnow
from app.core.errors import ConflictError, FieldError, UnauthorizedError
from app.core.logs import SECURITY_LOGGER
from app.core.security import (
    create_access_token,
    dummy_hash,
    hash_password_async,
    hash_refresh_token,
    new_refresh_token,
    validate_password,
    verify_password_async,
)
from app.core.settings import Settings
from app.core.settings.security import PASSWORD_MAX_LENGTH
from app.users.models import User
from app.users.repository import DuplicateUserError, UserRepository

security_log = logging.getLogger(SECURITY_LOGGER)

USER_AGENT_MAX_LENGTH = 256


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    expires_in: int  # seconds, for the access token


class AuthService:
    def __init__(
        self,
        users: UserRepository,
        refresh_tokens: RefreshTokenRepository,
        settings: Settings,
    ) -> None:
        self._users = users
        self._refresh_tokens = refresh_tokens
        self._settings = settings

    async def register(self, data: RegisterRequest) -> User:
        problems = validate_password(
            data.password, self._settings, user_inputs=[data.email, data.username]
        )
        if problems:
            raise FieldError("password", problems)

        password_hash = await hash_password_async(data.password)
        try:
            return await self._users.create(
                email=data.email, username=data.username, password_hash=password_hash
            )
        except DuplicateUserError as error:
            # This does tell a stranger that an email has an account. Closing
            # that needs email verification (answer the same either way, and
            # mail the owner), which is planned for after v1.
            raise ConflictError(
                "Email already registered" if error.field == "email" else "Username already taken"
            ) from error

    async def login(self, identifier: str, password: str, user_agent: str | None) -> IssuedTokens:
        user = await self._users.get_by_login(identifier)

        # Always verify something, even for an unknown account (see dummy_hash).
        stored_hash = user.password_hash if user else dummy_hash()
        valid, upgraded_hash = False, None
        # An over-long password can't be right (sign-up refuses them), so it
        # isn't worth hashing. Skipping is safe: it depends only on the input,
        # not on whether the account exists.
        if len(password) <= PASSWORD_MAX_LENGTH:
            valid, upgraded_hash = await verify_password_async(password, stored_hash)

        if user is None or not valid or not user.is_active:
            security_log.warning("Failed login for %s", f"user {user.id}" if user else "no account")
            # One message for every cause, so the response doesn't say whether
            # the account exists or is deactivated.
            raise UnauthorizedError("Incorrect email, username or password")

        if upgraded_hash:
            await self._users.set_password_hash(user.id, upgraded_hash)

        # A login starts a new session: a new token family.
        return await self._issue(user.id, ObjectId(), user_agent)

    async def refresh(self, refresh_token: str | None, user_agent: str | None) -> IssuedTokens:
        """Trade a refresh token for a new access token and a new refresh token."""
        invalid = UnauthorizedError("Invalid refresh token")
        if not refresh_token:
            raise invalid
        stored = await self._refresh_tokens.get(hash_refresh_token(refresh_token))
        if stored is None or stored.expires_at <= utcnow():
            raise invalid

        if stored.revoked_at is not None:
            if stored.replaced_by is not None:
                # This token was already traded for a newer one, and whoever
                # did that holds the newer one. So two parties have tokens of
                # this session and we can't tell which is the thief: end it.
                await self._report_reuse(stored.family_id, stored.user_id)
            # Otherwise it was ended by a logout, which is no cause for alarm.
            raise invalid

        user = await self._users.get(stored.user_id)
        if user is None or not user.is_active:
            await self._refresh_tokens.revoke_all_for_user(stored.user_id)
            raise invalid

        # Rotation: each refresh token works once. The order matters. The
        # replacement is stored first and the old token retired second, so
        # that at every moment the session has a stored, live token. Anything
        # that ends the session meanwhile (a logout, a concurrent request
        # detecting reuse) therefore reaches the replacement too. Retiring
        # first would leave a gap in which the session looks already ended,
        # and the replacement would then be created alive.
        tokens = await self._issue(user.id, stored.family_id, user_agent)
        retired = await self._refresh_tokens.rotate(
            stored.id, replaced_by=hash_refresh_token(tokens.refresh_token)
        )
        if not retired:
            # It was active a moment ago and no longer is: another request
            # used or revoked it in between. The replacement just stored is in
            # the same family, so this ends it as well.
            await self._report_reuse(stored.family_id, stored.user_id)
            raise invalid
        return tokens

    async def logout(self, refresh_token: str | None) -> None:
        """
        End the session this refresh token belongs to. Fine to call twice.

        The whole family is revoked, not just the token presented: if a thief
        has already rotated it, the token in the user's browser is stale, and
        revoking only that one would leave the thief logged in.
        """
        if not refresh_token:
            return
        stored = await self._refresh_tokens.get(hash_refresh_token(refresh_token))
        if stored is not None:
            await self._refresh_tokens.revoke_family(stored.family_id)

    async def logout_all(self, user_id: ObjectId) -> None:
        """
        End every session of the user, on every device.

        Access tokens already issued keep working until they expire, which is
        at most `access_token_ttl_minutes` later.
        """
        await self._refresh_tokens.revoke_all_for_user(user_id)

    async def _report_reuse(self, family_id: ObjectId, user_id: ObjectId) -> None:
        await self._refresh_tokens.revoke_family(family_id)
        security_log.warning("Refresh token reuse detected for user %s", user_id)

    async def _issue(
        self, user_id: ObjectId, family_id: ObjectId, user_agent: str | None
    ) -> IssuedTokens:
        refresh_token = new_refresh_token()
        # Each refresh grants a full new lifetime, so a session lasts as long
        # as it keeps being used; there is no absolute limit.
        ttl = timedelta(days=self._settings.refresh_token_ttl_days)
        await self._refresh_tokens.create(
            token_hash=hash_refresh_token(refresh_token),
            user_id=user_id,
            family_id=family_id,
            expires_at=utcnow() + ttl,
            user_agent=user_agent[:USER_AGENT_MAX_LENGTH] if user_agent else None,
        )
        return IssuedTokens(
            access_token=create_access_token(user_id, self._settings),
            refresh_token=refresh_token,
            expires_in=self._settings.access_token_ttl_minutes * 60,
        )
