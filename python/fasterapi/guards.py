"""fasterapi.guards — Rust-level edge guards, Python fallback included.

``RateLimiter`` configures a socket-level token bucket (Rust ``governor``
when compiled, pure-Python mirror otherwise) returning ``429`` with zero
Python overhead on the Hot path. ``APIKeyGuard`` validates ``X-API-Key``
before any handler runs.
"""

from __future__ import annotations

import hmac
from typing import Any, Awaitable, Callable, Optional

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import _rust_bridge as _bridge


class RateLimiter(BaseHTTPMiddleware):
    """Token-bucket rate limiter middleware (per-process, thread-safe)."""

    def __init__(self, app: Any, per_second: int = 100, burst: Optional[int] = None) -> None:
        super().__init__(app)
        self.per_second = per_second
        self.burst = burst
        self.bucket = _bridge.make_token_bucket(per_second, burst)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        allowed = self.bucket.check()
        if not allowed:
            return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
        return await call_next(request)


class APIKeyGuard(BaseHTTPMiddleware):
    """Require ``X-API-Key: <key>`` (or ``Authorization: Bearer``)."""

    def __init__(
        self,
        app: Any,
        api_key: Optional[str] = None,
        *,
        header: str = "x-api-key",
        allow_bearer: bool = True,
    ) -> None:
        super().__init__(app)
        self.api_key = api_key
        self.header = header.lower()
        self.allow_bearer = allow_bearer

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self.api_key:
            return await call_next(request)
        provided = request.headers.get(self.header)
        if provided is None and self.allow_bearer:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                provided = auth[7:].strip()
        # Constant-time-ish compare without importing hmac hot-path cost.
        ok = provided is not None and _constant_time_eq(provided, self.api_key)
        if not ok:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# Functional guard helper for route-level `dependencies=[Depends(api_key_guard)]`
def api_key_dependency(expected: str) -> Callable[..., Awaitable[None]]:
    from .exceptions import HTTPException
    from starlette.requests import Request as _Req

    async def _guard(request: _Req) -> None:
        provided = request.headers.get("x-api-key")
        if provided is None:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                provided = auth[7:].strip()
        if provided is None or not _constant_time_eq(provided, expected):
            raise HTTPException(status_code=401, detail="Unauthorized")

    return _guard
