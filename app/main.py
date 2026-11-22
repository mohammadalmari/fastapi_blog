from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from asyncer import asyncify
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from app.auth.router import password_router
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
from app.core.storage import LocalStorage
from app.core.ttl import Remembered
from app.likes.router import router as likes_router
from app.posts.router import my_posts_router, posts_router, tags_router
from app.rbac.router import permissions_router, roles_router
from app.rbac.sync import sync_rbac
from app.users.router import media_router
from app.users.router import router as users_router

API_VERSION = "1.0.0"

API_DESCRIPTION = """
Log in with `POST /api/v1/auth/login` (or the **Authorize** button) and send
the access token as `Authorization: Bearer <token>`.

- Lists are paged by cursor: pass a page's `next_cursor` as `cursor`.
- Errors are `{"detail": "..."}`; invalid input is a 422 with one entry per problem.
- A `429` means "too often": wait the number of seconds in `Retry-After`.
- Responses carry `X-Request-ID`; quote it when reporting a problem.
"""

# The order here is the order of the sections in /docs.
API_TAGS = [
    {"name": "auth", "description": "Signing up, logging in, and sessions."},
    {
        "name": "users",
        "description": "Your account and profile, avatars, and administering accounts.",
    },
    {"name": "roles", "description": "Roles, and the catalog of permissions they are built from."},
    {"name": "posts", "description": "Posts, drafts, publishing, the feed and tags."},
    {"name": "comments", "description": "Comments on posts, and one level of replies."},
    {"name": "likes", "description": "Likes on posts and comments."},
    {"name": "health", "description": "Whether the app and its database are up."},
]


def _operation_id(route: APIRoute) -> str:
    """
    A route's name in the schema: its section and its function, "posts-list_tags".

    FastAPI's default adds the whole path and the method
    ("list_tags_api_v1_tags_get"), and client generators name their functions
    after it. This is the form its docs suggest for generated clients.
    """
    return f"{route.tags[0]}-{route.name}"


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
            # The same idea for uploads: a folder that can't be written to
            # stops the app here, not the first person to upload a picture.
            await app.state.storage.check()
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
        summary="A multi-user blog API with roles and permissions an administrator manages.",
        description=API_DESCRIPTION,
        version=API_VERSION,
        openapi_tags=API_TAGS,
        debug=settings.debug,
        lifespan=lifespan,
        # Without the schema there are no interactive docs either, so this one
        # switch turns /openapi.json, /docs and /redoc off outside DEBUG.
        openapi_url=OPENAPI_URL if settings.docs_enabled else None,
        max_body_size=MAX_BODY_SIZE,
        generate_unique_id_function=_operation_id,
    )
    # Where `SettingsDep` finds them: routes get the settings this app was
    # built with, which in tests are not the ones from the environment.
    app.state.settings = settings
    # Where `StorageDep` finds it. Swap the class here to keep uploads
    # somewhere other than this machine's disk.
    app.state.storage = LocalStorage(settings.media_dir)
    app.state.tag_cache = Remembered(settings.tags_cache_seconds)

    install_security(app, settings)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(media_router)

    # Each feature router sets its own prefix and tags; this adds the version.
    api = APIRouter(prefix=API_PREFIX)
    api.include_router(auth_router)
    api.include_router(password_router)
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
