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
from .limits import RateLimit

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
# Rate limits (see app.core.ratelimit)
# ---------------------------------------------------------------------------
# Each is "at most this many, in this many seconds, per caller". They are set
# well above what a person does and well below what a script can: nobody
# honest should ever meet one.

_MINUTE = 60
_HOUR = 60 * _MINUTE

# Logging in. Every attempt counts against the address it comes from, which
# stops one machine trying many accounts.
LOGIN_PER_ADDRESS = RateLimit("login-address", 30, 5 * _MINUTE)
# Failed attempts also count against the account, wherever they come from,
# which stops many machines trying one account. The price: someone who knows
# your email can keep you from logging in, fifteen minutes at a time, by
# failing on purpose. Ten slow guesses a quarter of an hour is the trade.
LOGIN_FAILURES_PER_ACCOUNT = RateLimit("login-account", 10, 15 * _MINUTE)
# Signing up, per address: accounts are what every other limit counts by.
REGISTER_PER_ADDRESS = RateLimit("register", 10, _HOUR)
# Changing a password asks for the current one, so it is a way to guess it
# for whoever holds a stolen token.
PASSWORD_CHANGE_PER_USER = RateLimit("password-change", 5, 15 * _MINUTE)
# Writing. Each of these is a transaction on a shared document, or a picture
# to decode.
COMMENTS_PER_USER = RateLimit("comments", 30, _MINUTE)
LIKES_PER_USER = RateLimit("likes", 120, _MINUTE)
AVATARS_PER_USER = RateLimit("avatars", 10, _HOUR)

# ---------------------------------------------------------------------------
# Avatars
# ---------------------------------------------------------------------------

AVATAR_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB, the uploaded file
# The avatar route's own body limit: the file, plus room for the multipart
# wrapping around it.
AVATAR_MAX_BODY_SIZE = AVATAR_MAX_BYTES + 64 * 1024
# Width times height. A small file can declare an enormous picture, so this
# is refused before anything is decoded. What it bounds is memory: decoding
# takes four bytes a pixel, and a few times that while the picture is
# converted and resized.
#
# A JPEG is decoded at a fraction of its size (see `make_avatar`), so it may
# be large: 4096 x 4096 covers a 12-megapixel photo and costs about 50 MiB.
AVATAR_MAX_PIXELS = 4096 * 4096
# PNG and WebP are decoded whole. At the limit above, a flat colour of under
# a kilobyte needed about 460 MiB. At this one, a quarter of the pixels,
# about 70 MiB.
AVATAR_MAX_PIXELS_FULL_DECODE = 2048 * 2048
AVATAR_SIZE = 256  # pixels, each side of the stored square
# How many uploads are decoded at the same moment; the rest wait their turn.
AVATAR_PROCESSING_CONCURRENCY = 2

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
# Response headers a page on another origin may read. A browser hides all but
# a few by default, and a frontend needs these two: how long to wait after a
# 429, and the id to quote when reporting a problem.
CORS_EXPOSE_HEADERS = ["Retry-After", "X-Request-ID"]

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
