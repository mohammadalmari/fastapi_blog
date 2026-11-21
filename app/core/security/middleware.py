"""
Request-level protection: host checking, HTTPS, CORS, and response headers.

The values live in ``app.core.settings.security``; this module enforces them.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.httpsredirect import HTTPSRedirectMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.settings import Settings
from app.core.settings import security as values

DOCS_URL = "/docs"
REDOC_URL = "/redoc"
OPENAPI_URL = "/openapi.json"
_DOCS_PATHS = frozenset({DOCS_URL, f"{DOCS_URL}/oauth2-redirect", REDOC_URL})


class SecurityHeadersMiddleware:
    """Add the security headers to every HTTP response."""

    def __init__(self, app: ASGIApp, *, hsts: bool, docs_enabled: bool) -> None:
        self.app = app
        self.docs_paths = _DOCS_PATHS if docs_enabled else frozenset()
        self.headers = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": values.X_FRAME_OPTIONS,
            "Referrer-Policy": values.REFERRER_POLICY,
            "Cross-Origin-Opener-Policy": values.CROSS_ORIGIN_OPENER_POLICY,
        }
        if hsts:
            self.headers["Strict-Transport-Security"] = values.STRICT_TRANSPORT_SECURITY

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_docs = scope["path"] in self.docs_paths
        csp = values.DOCS_CONTENT_SECURITY_POLICY if is_docs else values.CONTENT_SECURITY_POLICY

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    headers[name] = value
                headers["Content-Security-Policy"] = csp
            await send(message)

        await self.app(scope, receive, send_with_headers)


def install_security(app: FastAPI, settings: Settings) -> None:
    """
    Wire the protective middleware onto the app.

    Starlette runs the middleware added *last* first, so this list reads from
    the innermost layer to the outermost one.
    """
    # CORS sits closest to the app so that its headers are on every response
    # the app produces, including error responses.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.cors_allow_all_origins else settings.cors_allowed_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=values.CORS_ALLOW_METHODS,
        allow_headers=values.CORS_ALLOW_HEADERS,
    )

    if settings.https_only:
        # Behind a TLS-terminating proxy the server must trust the proxy's
        # X-Forwarded-Proto header, or every request redirects forever:
        #   fastapi run --forwarded-allow-ips=<proxy ip>
        app.add_middleware(HTTPSRedirectMiddleware)

    # Reject requests whose Host header isn't ours (400).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    # Outermost, so the headers also land on redirects and rejected requests.
    app.add_middleware(
        SecurityHeadersMiddleware,
        hsts=settings.https_only,
        docs_enabled=settings.docs_enabled,
    )
