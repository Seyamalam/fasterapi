"""fasterapi._hyper_serve — Python side of the Hyper ASGI host spike.

Rust (`serve_asgi`) owns sockets + HTTP parsing and calls two functions here:

* ``run_lifespan(app, kind)`` — drives Starlette lifespan around serving,
* ``run_request(...)`` — drives ``app(scope, receive, send)`` for one
  request on a fresh per-call asyncio loop and returns
  ``(status, headers, body)``.

Same app object, same middleware, same state: bytes stay identical to uvicorn.
Spike limits (see ``src/lib.rs``): HTTP/1.1, buffered bodies, concatenated
streaming, no mid-response disconnect polling.
"""

from __future__ import annotations

import asyncio
import threading
import traceback
from typing import Any, Optional
from urllib.parse import unquote

_LIFESPAN_CM: Any = None
_tls = threading.local()


def _direct_ok(app: Any) -> bool:
    """Direct dispatch is valid only with zero user middleware.

    Skipping Starlette's ServerError/Exception wrappers + router loop is
    byte-identical only when no user middleware could observe or alter the
    exchange. The check is a tiny isinstance scan (no cache → always fresh
    if the app gains middleware later).
    """
    try:
        from .middleware.asyncexitstack import AsyncExitStackMiddleware

        middlewares = getattr(app, "user_middleware", None) or []
        return all(entry[0] is AsyncExitStackMiddleware for entry in middlewares)
    except Exception:
        return False


def _get_loop() -> asyncio.AbstractEventLoop:
    """One asyncio loop per worker thread, reused across requests.

    ``asyncio.run()`` per request costs a fresh loop each time (~tens of µs);
    Rust's blocking pool reuses threads, so a thread-local loop amortizes
    that to zero after the first request on each thread.
    """
    loop = getattr(_tls, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _tls.loop = loop
    return loop


def run_lifespan(app: Any, kind: str) -> None:
    """Run Starlette lifespan startup/shutdown around the Rust server."""

    async def _go() -> None:
        global _LIFESPAN_CM
        if kind == "startup":
            cm = app.router.lifespan_context(app)
            await cm.__aenter__()
            _LIFESPAN_CM = cm
        else:
            cm, _LIFESPAN_CM = _LIFESPAN_CM, None
            if cm is not None:
                await cm.__aexit__(None, None, None)

    asyncio.run(_go())


def run_request(
    app: Any,
    method: str,
    path: str,
    query_string: str,
    headers: list[tuple[str, bytes]],
    body: bytes,
) -> tuple[int, list[tuple[str, str]], bytes]:
    """Drive one ASGI request to completion; return ``(status, headers, body)``."""
    raw_headers = [(name.lower().encode("latin-1"), bytes(value)) for name, value in headers]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "scheme": "http",
        "path": unquote(path),
        "raw_path": path.encode("ascii", "replace"),
        "query_string": query_string.encode("ascii", "replace"),
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 0),
        "server": ("fasterapi-hyper", 0),
        "extensions": {},
    }
    pending: list[dict[str, Any]] = [{"type": "http.request", "body": bytes(body), "more_body": False}]
    sent: dict[str, Any] = {}

    async def receive() -> dict[str, Any]:
        if pending:
            return pending.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        mtype = message.get("type")
        if mtype == "http.response.start":
            sent["status"] = int(message.get("status", 500))
            sent["headers"] = [
                (k.decode("latin-1"), v.decode("latin-1")) for k, v in message.get("headers", [])
            ]
        elif mtype == "http.response.body":
            sent.setdefault("chunks", []).append(bytes(message.get("body", b"")))

    async def _go() -> None:
        if _direct_ok(app):
            # Fast lane: match via Starlette's own compiled matchers, then run
            # the matched route app directly — no ServerError/Exception
            # wrappers, no router loop. Anything unmatched (404/405/redirects)
            # falls through to the full stack for exact handling.
            from starlette.routing import Match

            router = app.router
            for route in router.routes:
                matches = getattr(route, "matches", None)
                if matches is None:
                    continue
                try:
                    match, child_scope = matches(scope)
                except Exception:
                    continue
                if match == Match.FULL:
                    scope.update(child_scope)
                    endpoint = getattr(route, "endpoint", None)
                    if endpoint is not None:
                        scope["endpoint"] = endpoint
                    await route.app(scope, receive, send)
                    return
        await app(scope, receive, send)

    try:
        _get_loop().run_until_complete(_go())
    except Exception:
        traceback.print_exc()
        raise
    return sent.get("status", 500), sent.get("headers", []), b"".join(sent.get("chunks", []))


def hyper_available() -> bool:
    try:
        from fasterapi._fasterapi_rs import serve_asgi  # type: ignore  # noqa: F401
    except ImportError:
        try:
            from _fasterapi_rs import serve_asgi  # type: ignore  # noqa: F401
        except ImportError:
            return False
    return True
