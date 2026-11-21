"""
Everything protecting the app.

Passwords, tokens, transport security, cookies, CORS, and response headers.
Baseline hardening applies everywhere; anything that requires HTTPS or would
get in the way of local development is gated behind ``not DEBUG``.

This module only holds the *values*. They are enforced by
``app.core.security`` (middleware, password hashing and validation).
"""

from typing import Literal

from pydantic import Field, ValidationInfo, field_validator

from .environment import API_PREFIX, CommaSeparated, EnvironmentSettings, is_debug

# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

PASSWORD_MIN_LENGTH_DEBUG = 8
PASSWORD_MIN_LENGTH = 12  # Stronger than the common default of 8
# Argon2 has no length limit of its own; cap it so a huge "password" can't be
# used to burn CPU and memory on hashing.
PASSWORD_MAX_LENGTH = 128
# How many passwords may be hashed at the same moment. Each Argon2 hash takes
# about 64 MiB and several CPU threads, so this caps what a burst of logins
# can cost: 4 x 64 MiB. Further requests wait their turn.
PASSWORD_HASH_CONCURRENCY = 4

# ---------------------------------------------------------------------------
# Request size limits
# ---------------------------------------------------------------------------

# Default cap on a whole request body; FastAPI answers 413 beyond it. Routes
# that accept uploads raise it for themselves with their own `max_body_size`.
MAX_BODY_SIZE = 1 * 1024 * 1024  # 1 MiB

# ---------------------------------------------------------------------------
# Headers (safe in every environment)
# ---------------------------------------------------------------------------

X_FRAME_OPTIONS = "DENY"
REFERRER_POLICY = "same-origin"
CROSS_ORIGIN_OPENER_POLICY = "same-origin"

# The API only returns JSON and files, so nothing may load or run from it.
CONTENT_SECURITY_POLICY = "default-src 'none'; frame-ancestors 'none'"

# The interactive docs (DEBUG only) need the Swagger UI / ReDoc assets.
DOCS_CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com",
        "font-src 'self' data: https://fonts.gstatic.com",
        "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly",
        "worker-src 'self' blob:",
        "frame-ancestors 'none'",
    ]
)

# ---------------------------------------------------------------------------
# HTTPS-only hardening (production)
# ---------------------------------------------------------------------------

HSTS_SECONDS = 31536000  # 1 year
STRICT_TRANSPORT_SECURITY = f"max-age={HSTS_SECONDS}; includeSubDomains; preload"

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------

CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
CORS_ALLOW_HEADERS = ["Authorization", "Content-Type"]

# ---------------------------------------------------------------------------
# Refresh-token cookie
# ---------------------------------------------------------------------------

REFRESH_COOKIE_NAME = "refresh_token"
REFRESH_COOKIE_PATH = f"{API_PREFIX}/auth"  # Only sent to the auth endpoints
REFRESH_COOKIE_SAMESITE: Literal["strict"] = "strict"


class SecuritySettings(EnvironmentSettings):
    """Security values that come from the environment or depend on DEBUG."""

    # In DEBUG any origin may call the API; in production only this explicit
    # allow-list is honoured, e.g.
    #   FASTAPI_CORS_ALLOWED_ORIGINS=https://app.example.com,https://admin.example.com
    cors_allowed_origins: CommaSeparated = Field(default_factory=list)

    # Cookies / Authorization headers on cross-origin requests are opt-in.
    # A browser frontend on another origin needs this for the refresh cookie.
    cors_allow_credentials: bool = False

    # Short-lived access token; the refresh token (rotated on use) outlives it.
    access_token_ttl_minutes: int = Field(default=15, gt=0)
    refresh_token_ttl_days: int = Field(default=14, gt=0)

    @field_validator("cors_allowed_origins")
    @classmethod
    def _check_origins(cls, value: list[str], info: ValidationInfo) -> list[str]:
        origins = [origin.rstrip("/") for origin in value]
        for origin in origins:
            if origin == "*":
                if is_debug(info):
                    continue
                raise ValueError("FASTAPI_CORS_ALLOWED_ORIGINS must not contain '*' outside DEBUG")
            if not origin.startswith(("http://", "https://")):
                raise ValueError(f"CORS origin {origin!r} must include the scheme, e.g. https://")
        return origins

    @property
    def cors_allow_all_origins(self) -> bool:
        return self.debug

    @property
    def https_only(self) -> bool:
        """Redirect HTTP to HTTPS, send HSTS, and mark cookies Secure."""
        return not self.debug

    @property
    def docs_enabled(self) -> bool:
        """Interactive docs and the OpenAPI schema are a development tool."""
        return self.debug

    @property
    def password_min_length(self) -> int:
        return PASSWORD_MIN_LENGTH_DEBUG if self.debug else PASSWORD_MIN_LENGTH

    @property
    def password_strict(self) -> bool:
        """Outside DEBUG, also reject numeric-only and look-alike passwords."""
        return not self.debug
