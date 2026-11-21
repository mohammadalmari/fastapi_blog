"""
Paths, environment loading, and the core deployment switches.

Every other settings module builds on ``EnvironmentSettings`` and imports its
helpers from here, so this module must stay import-free of its siblings.

Every setting that differs between machines is read from the process
environment, falling back to ``.env`` at the repo root. App-owned variables are
namespaced ``FASTAPI_*`` so they don't collide with anything else in the shell;
the database ones are ``DATABASE_*`` (``DATABASE_URL`` is the name tooling and
hosting providers already look for).

FastAPI and its CLI read a few ``FASTAPI_*`` variables of their own
(``FASTAPI_ENV``, ``FASTAPI_PUBLIC_URL``, ``FASTAPI_OTEL_AUTO_CONFIGURE``);
don't reuse those names for settings fields.

The *safe* value is always the default (DEBUG off, no hosts, no secret key), so
a missing variable degrades toward production behaviour, or stops the process,
rather than silently exposing a dev configuration.
"""

from pathlib import Path
from typing import Annotated

from pydantic import BeforeValidator, Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# The repo root: app/core/settings/environment.py -> three levels up.
ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent

# Only ever used when DEBUG is on. Long enough (>= 32 bytes) for HS256.
DEV_SECRET_KEY = "dev-only-insecure-key-never-use-in-production"  # noqa: S105

MIN_SECRET_KEY_LENGTH = 32

# Every feature router is mounted under this prefix (see app.main).
API_PREFIX = "/api/v1"

_DEV_HOSTS = ("localhost", "127.0.0.1")


def _split_commas(value: object) -> object:
    """Turn ``"a, b,c"`` from the environment into ``["a", "b", "c"]``."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


# A list read from a comma-separated env var, e.g. FASTAPI_ALLOWED_HOSTS=a.com,b.com
# (``NoDecode`` stops pydantic-settings from expecting JSON).
CommaSeparated = Annotated[list[str], NoDecode, BeforeValidator(_split_commas)]


def is_debug(info: ValidationInfo) -> bool:
    """Read the already-validated DEBUG flag from inside a field validator."""
    return bool(info.data.get("debug", False))


class EnvironmentSettings(BaseSettings):
    """DEBUG, SECRET_KEY and ALLOWED_HOSTS: the switches everything else keys off."""

    model_config = SettingsConfigDict(
        env_prefix="FASTAPI_",
        # Real environment variables win over .env, so containers and CI can
        # inject config without the file being in the way.
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        # `FASTAPI_SECRET_KEY=` in .env means "unset", not "empty string".
        env_ignore_empty=True,
        # Tolerate variables in .env that belong to other tools.
        extra="ignore",
        frozen=True,
    )

    # SECURITY WARNING: don't run with debug turned on in production!
    # true = development (interactive docs, relaxed password rules, permissive
    # CORS, insecure-key fallback). Unset/false = production hardening.
    debug: bool = False

    # SECURITY WARNING: keep the secret key used in production secret!
    # Signs the JWTs. Outside DEBUG a missing key must stop the process, not
    # fall back to a value an attacker can read in this repo.
    secret_key: SecretStr = Field(default=SecretStr(""), validate_default=True)

    # Host headers the app will answer to; anything else gets a 400.
    allowed_hosts: CommaSeparated = Field(default_factory=list, validate_default=True)

    @field_validator("secret_key")
    @classmethod
    def _require_secret_key(cls, value: SecretStr, info: ValidationInfo) -> SecretStr:
        key = value.get_secret_value()
        if is_debug(info):
            return value if key else SecretStr(DEV_SECRET_KEY)
        if not key:
            raise ValueError("FASTAPI_SECRET_KEY is required when FASTAPI_DEBUG is not true")
        if key == DEV_SECRET_KEY:
            raise ValueError("FASTAPI_SECRET_KEY is the public dev key; generate a real one")
        if len(key) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(
                f"FASTAPI_SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters"
            )
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def _require_allowed_hosts(cls, value: list[str], info: ValidationInfo) -> list[str]:
        if is_debug(info):
            return [*value, *(host for host in _DEV_HOSTS if host not in value)]
        if not value:
            raise ValueError("FASTAPI_ALLOWED_HOSTS is required when FASTAPI_DEBUG is not true")
        if "*" in value:
            raise ValueError("FASTAPI_ALLOWED_HOSTS must not contain '*' outside DEBUG")
        return value
