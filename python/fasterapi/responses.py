"""fasterapi.responses — Starlette re-exports + zero-copy FileResponse hint."""

from __future__ import annotations

import os
from typing import Optional

from starlette.responses import (  # noqa: F401
    FileResponse as _StarletteFileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)


class FileResponse(_StarletteFileResponse):
    """Static-file response that prefers Tokio `sendfile` when available.

    On the pure-Python host this behaves exactly like Starlette's
    ``FileResponse``; when the Rust Hyper host is active the ``x-sendfile``
    plan (see ``_rust_bridge``) lets Tokio stream the file with zero Python
    overhead. ``ServeFiles`` / ``StaticFiles`` mounting stays compatible.
    """

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        try:
            from . import _rust_bridge as _bridge

            if _bridge.HAS_RUST:
                from _fasterapi_rs import serve_static_config  # type: ignore

                cfg = serve_static_config(str(self.path))
                self.headers["x-fasterapi-serve"] = "tokio-sendfile"
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
    "FileResponse",
]
