from fastapi import FastAPI

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

    app = FastAPI(
        title="FastAPI Blog",
        debug=settings.debug,
        # Without the schema there are no interactive docs either, so this one
        # switch turns /openapi.json, /docs and /redoc off outside DEBUG.
        openapi_url=OPENAPI_URL if settings.docs_enabled else None,
        max_body_size=MAX_BODY_SIZE,
    )
    install_security(app, settings)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
