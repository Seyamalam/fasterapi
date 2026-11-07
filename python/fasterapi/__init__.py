"""fasterapi — FastAPI-compatible ASGI framework with a native Rust core.

Drop-in migration is one import change::

    from fastapi import FastAPI, Depends, HTTPException, Query, Path, Body, APIRouter
    #  ──▶
    from fasterapi import FastAPI, Depends, HTTPException, Query, Path, Body, APIRouter

Parity: decorators, params, Pydantic v2 validation, ``Depends``/``Security``
DI (sub-deps, cache, ``yield`` teardown), ``BackgroundTasks``, security
schemes, ``HTTPException``/``RequestValidationError`` handlers, lifespan,
middleware, ``TestClient`` and OpenAPI.

Speed: ``app.build()`` AOT-compiles ``Depends()`` graphs into a Rust DAG +
radix router (``matchit``); scalars validate in Rust; Pydantic bodies take
the single-pass ``validate_json`` path and responses the ``dump_json`` path.
"""

from __future__ import annotations

from . import _rust_bridge as _rust_bridge  # noqa: F401
from . import concurrency as concurrency  # noqa: F401 (submodule surface parity)
from . import dependencies as dependencies  # noqa: F401
from . import exception_handlers as exception_handlers  # noqa: F401
from . import logger as logger  # noqa: F401
from . import middleware as middleware  # noqa: F401
from . import openapi as openapi  # noqa: F401
from . import param_functions as param_functions  # noqa: F401
from . import requests as requests  # noqa: F401
from . import status as status  # noqa: F401
from . import types as types  # noqa: F401
from . import utils as utils  # noqa: F401
from . import websockets as websockets  # noqa: F401
from ._version import __version__
from .applications import FastAPI
from .background import BackgroundTask, BackgroundTasks
from .dag import CompiledDAG, compile_app_dag, compile_endpoint_dag
from .datastructures import UploadFile
from .encoders import jsonable_encoder
from .exceptions import HTTPException, RequestValidationError, ResponseValidationError, WebSocketException
from .guards import APIKeyGuard, RateLimiter, api_key_dependency
from .params import Body, Cookie, Depends, File, Form, Header, Path, Query, Security
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
    "Security",
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
    "UploadFile",
    "BackgroundTasks",
    "BackgroundTask",
    "jsonable_encoder",
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
