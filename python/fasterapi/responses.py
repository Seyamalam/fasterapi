"""fasterapi.responses — response classes (1:1 ``fastapi.responses``)."""

from __future__ import annotations

import importlib
from typing import Any

from starlette.responses import (  # noqa: F401
    FileResponse as _StarletteFileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from .sse import EventSourceResponse as EventSourceResponse  # noqa: F401

try:
    _ujson = importlib.import_module("ujson")
except ModuleNotFoundError:  # pragma: no cover
    _ujson = None  # type: ignore[assignment]

try:
    _orjson = importlib.import_module("orjson")
except ModuleNotFoundError:  # pragma: no cover
    _orjson = None  # type: ignore[assignment]


class UJSONResponse(JSONResponse):
    """JSON response using ujson (deprecated upstream; same assert behavior)."""

    def render(self, content: Any) -> bytes:
        assert _ujson is not None, "ujson must be installed to use UJSONResponse"
        return _ujson.dumps(content, ensure_ascii=False).encode("utf-8")


class ORJSONResponse(JSONResponse):
    """JSON response using orjson (deprecated upstream; same assert behavior)."""

    def render(self, content: Any) -> bytes:
        assert _orjson is not None, "orjson must be installed to use ORJSONResponse"
        return _orjson.dumps(content, option=_orjson.OPT_NON_STR_KEYS | _orjson.OPT_SERIALIZE_NUMPY)


class FileResponse(_StarletteFileResponse):
    """Static-file response with a zero-copy serve plan attached.

    Behaves exactly like Starlette's ``FileResponse``; when the native ext
    is compiled it also records the ``x-fasterapi-serve`` static plan so the
    staged host can ``sendfile`` media bypassing Python.
    """

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        # Plan headers are hyper-host-only (env-gated) so uvicorn/TestClient
        # byte parity stays exact everywhere else.
        import os as _os

        if _os.environ.get("FASTERAPI_HYPER") != "1":
            return
        try:
            from . import _rust_bridge as _bridge

            if _bridge.HAS_RUST:
                try:
                    from ._fasterapi_rs import serve_static_config  # type: ignore
                except ImportError:
                    from _fasterapi_rs import serve_static_config  # type: ignore

                cfg = serve_static_config(str(self.path))
                self.headers["x-fasterapi-serve"] = "sendfile"
                self.headers["x-fasterapi-static-plan"] = cfg
        except Exception:
            pass

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        import os as _os

        # Zero-copy lane: under the Hyper host (and only there — the env var
        # is set solely by it), keep Starlette's exact headers/status (etag,
        # 304/206 negotiation) but swap the body bytes for a sendfile marker
        # the Rust layer turns into a disk stream. Range requests keep the
        # Python path so partial-content semantics stay Starlette's.
        if scope.get("type") == "http" and _os.environ.get("FASTERAPI_HYPER") == "1":
            try:
                raw = [(k.decode("latin-1").lower(), v) for k, v in scope.get("headers", [])]
            except Exception:
                raw = []
            if not any(k == "range" for k, _ in raw):
                async def _send(message: Any) -> None:
                    mtype = message.get("type")
                    if mtype == "http.response.start":
                        headers = dict(message.get("headers", []))
                        headers[b"x-fasterapi-sendfile"] = str(self.path).encode("latin-1")
                        message = dict(message, headers=list(headers.items()))
                    elif mtype == "http.response.body":
                        message = dict(message, body=b"")
                    await send(message)

                await super().__call__(scope, receive, _send)
                return
        await super().__call__(scope, receive, send)


__all__ = [
    "Response",
    "JSONResponse",
    "PlainTextResponse",
    "HTMLResponse",
    "RedirectResponse",
    "StreamingResponse",
    "EventSourceResponse",
    "ORJSONResponse",
    "UJSONResponse",
    "FileResponse",
]
