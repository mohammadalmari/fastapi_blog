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

``SettingsDep`` hands out the settings the running app was built with
(``create_app(settings)`` stores them on ``app.state``), which in tests are not
the ones from the environment. ``get_settings()`` always reads the environment.
"""

from functools import lru_cache
from typing import Annotated, cast

from fastapi import Depends, Request

from .security import SecuritySettings
from .services import ServicesSettings


class Settings(SecuritySettings, ServicesSettings):
    """Every setting in one object."""


@lru_cache
def get_settings() -> Settings:
    """Load and validate the settings once per process."""
    return Settings()


async def get_app_settings(request: Request) -> Settings:
    """The settings of the app handling this request."""
    return cast(Settings, request.app.state.settings)


SettingsDep = Annotated[Settings, Depends(get_app_settings)]

__all__ = ["Settings", "SettingsDep", "get_app_settings", "get_settings"]
