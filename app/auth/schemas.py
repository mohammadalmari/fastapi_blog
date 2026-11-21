"""What the auth endpoints accept and return."""

from typing import Literal

from pydantic import BaseModel, Field

from app.core.settings.security import PASSWORD_MAX_LENGTH
from app.users.schemas import Email, Username


class RegisterRequest(BaseModel):
    email: Email
    username: Username
    # Only the upper bound here; the rest of the password policy depends on
    # the settings and is checked in the service.
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class TokenResponse(BaseModel):
    """The shape OAuth2 clients expect, which the /docs "Authorize" button is."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - the OAuth2 token type, not a secret
    expires_in: int = Field(description="Seconds until the access token expires.")
