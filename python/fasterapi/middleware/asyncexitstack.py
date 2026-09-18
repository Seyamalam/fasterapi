"""fasterapi.middleware.asyncexitstack — request-scoped teardown ordering.

Mirrors ``fastapi.middleware.asyncexitstack.AsyncExitStackMiddleware``: one
``AsyncExitStack`` per request, closed only after the downstream app (route
handler *and* response background tasks) fully completes. ``yield``
dependencies therefore tear down after background work — the exact FastAPI
ordering — instead of before the response is sent.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

#: Scope key carrying the request lifetime stack (same key as FastAPI).
SCOPE_KEY = "fastapi_middleware_astack"


class AsyncExitStackMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        async with AsyncExitStack() as stack:
            request.scope[SCOPE_KEY] = stack
            return await call_next(request)
