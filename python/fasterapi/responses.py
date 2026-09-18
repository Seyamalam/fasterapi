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
