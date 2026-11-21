from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core import health
from app.core.database import create_client, ensure_indexes
from app.core.logs import configure_logging
from app.core.security import install_security
from app.core.security.middleware import OPENAPI_URL
from app.core.settings import Settings, get_settings
from app.core.settings.security import MAX_BODY_SIZE


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
            app.state.db = db
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
    install_security(app, settings)
    app.include_router(health.router)

    return app


app = create_app()
