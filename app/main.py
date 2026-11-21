from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from asyncer import asyncify
from fastapi import APIRouter, FastAPI

from app.auth.router import router as auth_router
from app.comments.router import comments_router, post_comments_router
from app.core import health
from app.core.database import create_client, ensure_indexes
from app.core.errors import install_error_handlers
from app.core.logs import configure_logging
from app.core.security import dummy_hash, install_security
from app.core.security.middleware import OPENAPI_URL
from app.core.settings import Settings, get_settings
from app.core.settings.environment import API_PREFIX
from app.core.settings.security import MAX_BODY_SIZE
from app.likes.router import router as likes_router
from app.posts.router import my_posts_router, posts_router, tags_router
from app.rbac.router import permissions_router, roles_router
from app.rbac.sync import sync_rbac
from app.users.router import router as users_router


def create_app(settings: Settings | None = None) -> FastAPI:
    """
    Build the app.

    A factory, because the middleware is fixed when the app is created: tests
    pass their own settings to get, say, a production-mode app.
    """
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # Everything before `yield` runs once at startup, everything after it
        # once at shutdown.
        client = create_client(settings)
        try:
            db = client[settings.database_name]
            # Creating the client doesn't connect, so ask for something: if
            # MongoDB is unreachable the app fails to start instead of
            # failing on the first request.
            await db.command("ping")
            await ensure_indexes(db)
            # The permission catalog and the two system roles (see rbac.sync).
            await sync_rbac(db)
            app.state.db = db
            # Computed now so that the first login for an unknown account isn't
            # slower than later ones.
            await asyncify(dummy_hash)()
            yield
        finally:
            await client.close()

    app = FastAPI(
        title="FastAPI Blog",
        debug=settings.debug,
        lifespan=lifespan,
        # Without the schema there are no interactive docs either, so this one
        # switch turns /openapi.json, /docs and /redoc off outside DEBUG.
        openapi_url=OPENAPI_URL if settings.docs_enabled else None,
        max_body_size=MAX_BODY_SIZE,
    )
    # Where `SettingsDep` finds them: routes get the settings this app was
    # built with, which in tests are not the ones from the environment.
    app.state.settings = settings

    install_security(app, settings)
    install_error_handlers(app)
    app.include_router(health.router)

    # Each feature router sets its own prefix and tags; this adds the version.
    api = APIRouter(prefix=API_PREFIX)
    api.include_router(auth_router)
    api.include_router(users_router)
    api.include_router(permissions_router)
    api.include_router(roles_router)
    api.include_router(posts_router)
    api.include_router(my_posts_router)
    api.include_router(tags_router)
    api.include_router(post_comments_router)
    api.include_router(comments_router)
    api.include_router(likes_router)
    app.include_router(api)

    return app


app = create_app()
