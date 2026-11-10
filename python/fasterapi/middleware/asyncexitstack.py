"""fasterapi.middleware.asyncexitstack — request-scoped teardown ordering.

Mirrors ``fastapi.middleware.asyncexitstack.AsyncExitStackMiddleware``: one
``AsyncExitStack`` per request, closed only after the downstream app (route
handler *and* response background tasks) fully completes. ``yield``
dependencies therefore tear down after background work — the exact FastAPI
ordering — instead of before the response is sent.

Pure-ASGI form: FastAPI's version wraps this three-line job in
``BaseHTTPMiddleware``, which allocates a ``Request``, opens a task group,
and plumbs two in-memory streams per request. Awaiting the downstream app
directly closes the stack at the identical point — after the route handler
and its background work return — with none of that machinery.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

#: Scope key carrying the request lifetime stack (same key as FastAPI).
SCOPE_KEY = "fastapi_middleware_astack"


class AsyncExitStackMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        async with AsyncExitStack() as stack:
            scope[SCOPE_KEY] = stack
            await self.app(scope, receive, send)
