"""
A request id: one short label on everything that belongs to one request.

Every log line written while a request is handled carries it, and the
response returns it in ``X-Request-ID``. Someone reporting "I got an error at
ten past three" can then give the id, and the lines for exactly that request
can be found among everyone else's.
"""

import re
import uuid
from contextvars import ContextVar

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADER = "X-Request-ID"

# Set for the duration of a request; read by the log filter (see core.logs).
# A context variable, not a global: each request running at the same time
# sees its own value.
request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

# A proxy in front may already have given the request an id; keeping it lets
# the two sets of logs be matched. But the header is the client's to write, so
# only something that looks like an id is accepted. Anything else, such as
# text made to look like another log line, is replaced.
_ACCEPTABLE = re.compile(r"[A-Za-z0-9._-]{8,64}")


class RequestIdMiddleware:
    """Give every HTTP request an id, for its logs and its response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        received = Headers(scope=scope).get(HEADER, "")
        this_request = received if _ACCEPTABLE.fullmatch(received) else uuid.uuid4().hex
        token = request_id.set(this_request)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[HEADER] = this_request
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id.reset(token)
