"""fasterapi — 1:1 FastAPI drop-in powered by a Rust (Tokio + Hyper + PyO3) core.

Drop-in migration is one import change::

    from fastapi import FastAPI, Depends, HTTPException, Query, Path, Body, APIRouter
    #  ──▶
    from fasterapi import FastAPI, Depends, HTTPException, Query, Path, Body, APIRouter

Phase 1 (this release): total interface parity on a Starlette host with
Pydantic v2 validation, ``Depends`` DI (sub-deps, cache, ``yield`` teardown),
``HTTPException``/``RequestValidationError`` handlers, lifespan, middleware,
``TestClient`` and OpenAPI.

Phase 2 (accelerating): ``app.build()`` AOT-compiles ``Depends()`` graphs
into a Rust DAG + radix router (``matchit``); JSON takes the SIMD fast-path
(``simd-json``); guards run at the socket level (``governor``). Set
``FASTERAPI_HYPER=1`` once the inverted-runtime host is promoted.
"""

from __future__ import annotations

from . import _rust_bridge as _rust_bridge  # noqa: F401
from ._version import __version__
from .applications import FastAPI
from .dag import CompiledDAG, compile_app_dag, compile_endpoint_dag
from .exceptions import HTTPException, RequestValidationError, ResponseValidationError, WebSocketException
from .guards import APIKeyGuard, RateLimiter, api_key_dependency
from .params import Body, Cookie, Depends, File, Form, Header, Path, Query
from .responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from .routing import APIRouter
from .testclient import TestClient

try:
    from starlette.middleware.cors import CORSMiddleware  # noqa: F401
    from starlette.middleware.gzip import GZipMiddleware  # noqa: F401
    from starlette.requests import Request  # noqa: F401
    from starlette.staticfiles import StaticFiles  # noqa: F401
    from starlette.websockets import WebSocket, WebSocketDisconnect  # noqa: F401
except Exception:  # pragma: no cover - starlette always present per pyproject
    pass

try:
    from pydantic import BaseModel  # noqa: F401
except Exception:  # pragma: no cover
    pass

HAS_RUST: bool = _rust_bridge.HAS_RUST

__all__ = [
    "__version__",
    "HAS_RUST",
    "FastAPI",
    "APIRouter",
    "Depends",
    "HTTPException",
    "WebSocketException",
    "RequestValidationError",
    "ResponseValidationError",
    "Query",
    "Path",
    "Body",
    "Header",
    "Cookie",
    "Form",
    "File",
    "TestClient",
    "Response",
    "JSONResponse",
    "PlainTextResponse",
    "HTMLResponse",
    "RedirectResponse",
    "StreamingResponse",
    "FileResponse",
    "RateLimiter",
    "APIKeyGuard",
    "api_key_dependency",
    "CompiledDAG",
    "compile_app_dag",
    "compile_endpoint_dag",
]
