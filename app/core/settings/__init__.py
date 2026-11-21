"""
Settings for the app, assembled from focused modules.

There is a single ``Settings`` class used everywhere; environment variables,
chiefly ``FASTAPI_DEBUG``, select dev/prod behaviour inside each module rather
than swapping settings classes per environment.

- ``environment``: env loading, DEBUG, SECRET_KEY, ALLOWED_HOSTS
- ``security``: passwords, tokens, HTTPS/HSTS, cookies, CORS, headers
- ``services``: database, logging

Modules only ever import from ``environment``, never from each other.

Usage::

    from app.core.settings import get_settings

    settings = get_settings()

or, in a route or another dependency, declare a ``SettingsDep`` parameter::

    @router.get("/info")
    async def info(settings: SettingsDep) -> ...: ...

Tests swap the settings with ``app.dependency_overrides[get_settings]``.
"""

from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from .security import SecuritySettings
from .services import ServicesSettings


class Settings(SecuritySettings, ServicesSettings):
    """Every setting in one object."""


@lru_cache
def get_settings() -> Settings:
    """Load and validate the settings once per process."""
    return Settings()


SettingsDep = Annotated[Settings, Depends(get_settings)]

__all__ = ["Settings", "SettingsDep", "get_settings"]
