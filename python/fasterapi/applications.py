"""fasterapi.applications — FastAPI-compatible app on Starlette's ASGI host.

``FastAPI`` subclasses Starlette so middleware, lifespan, exception handlers,
and TestClient use the familiar ASGI stack. ``build()`` compiles dependency
and native router metadata; ``serve()`` defaults to uvicorn, with an optional
experimental Hyper host.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Optional

from starlette.applications import Starlette
from starlette.datastructures import State as State  # noqa: F401
from starlette.middleware import Middleware as Middleware  # noqa: F401
from starlette.middleware.base import BaseHTTPMiddleware as BaseHTTPMiddleware  # noqa: F401
from starlette.middleware.exceptions import ExceptionMiddleware as ExceptionMiddleware  # noqa: F401
from starlette.middleware.errors import ServerErrorMiddleware as ServerErrorMiddleware  # noqa: F401
from starlette.requests import Request
from starlette.responses import HTMLResponse as HTMLResponse  # noqa: F401
from starlette.routing import BaseRoute as BaseRoute  # noqa: F401
from starlette.responses import JSONResponse, Response
from starlette.routing import Route as StarletteRoute

from . import _rust_bridge as _bridge
from .dag import CompiledDAG, compile_app_dag
from .exception_handlers import http_exception_handler as http_exception_handler  # noqa: F401
from .exception_handlers import request_validation_exception_handler as request_validation_exception_handler  # noqa: F401
from .exception_handlers import websocket_request_validation_exception_handler as websocket_request_validation_exception_handler  # noqa: F401
from .exceptions import HTTPException, RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from .exceptions import WebSocketRequestValidationError as WebSocketRequestValidationError  # noqa: F401
from .middleware.asyncexitstack import AsyncExitStackMiddleware as AsyncExitStackMiddleware  # noqa: F401
from .openapi.docs import get_redoc_html as get_redoc_html  # noqa: F401
from .openapi.docs import get_swagger_ui_html as get_swagger_ui_html  # noqa: F401
from .openapi.docs import get_swagger_ui_oauth2_redirect_html as get_swagger_ui_oauth2_redirect_html  # noqa: F401
from .openapi.utils import get_openapi as get_openapi  # noqa: F401
from .params import Depends
from .routing import APIRouter
from .types import DecoratedCallable as DecoratedCallable  # noqa: F401 (surface parity)
from .utils import generate_unique_id as generate_unique_id  # noqa: F401


def _on_event_lifespan(router: Any) -> Callable[..., Any]:
    """Lifespan running ``router.on_startup``/``on_shutdown`` handler lists.

    Mirrors FastAPI's vendored ``_DefaultLifespan``: only installed when the
    user passed no ``lifespan=`` (a custom lifespan bypasses on_event there
    too). Sync handlers are called, async handlers awaited.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app: Any):
        for handler in getattr(router, "on_startup", []):
            res = handler()
            if inspect.isawaitable(res):
                await res
        try:
            yield None
        finally:
            for handler in getattr(router, "on_shutdown", []):
                res = handler()
                if inspect.isawaitable(res):
                    await res

    return lifespan


class FastAPI(Starlette):
    """FastAPI-compatible ASGI application."""

    def __init__(
        self,
        *,
        title: str = "fasterapi",
        version: str = "0.1.0",
        description: str = "",
        lifespan: Optional[Callable[..., Any]] = None,
        middleware: Optional[list[Any]] = None,
        exception_handlers: Optional[dict[Any, Callable[..., Any]]] = None,
        dependencies: Optional[list[Depends]] = None,
        default_response_class: Any = JSONResponse,
        redirect_slashes: bool = True,
        docs_url: Optional[str] = "/docs",
        redoc_url: Optional[str] = "/redoc",
        openapi_url: Optional[str] = "/openapi.json",
        swagger_ui_oauth2_redirect_url: Optional[str] = "/docs/oauth2-redirect",
        swagger_ui_parameters: Optional[dict[str, Any]] = None,
        servers: Optional[list[dict[str, Any]]] = None,
        webhooks: Optional[Any] = None,
        **extra: Any,
    ) -> None:
        super().__init__(lifespan=lifespan, middleware=middleware)  # type: ignore[call-arg]
        # Radix-tree fast path for route dispatch (matchit). Falls back to
        # Starlette's linear scan for anything it cannot prove identical.
        from ._fast_router import FastRouter

        self.router = FastRouter(lifespan=lifespan, redirect_slashes=redirect_slashes)
        # Request lifetime stack first (innermost user middleware), exactly
        # like FastAPI: `yield` teardowns run after background tasks.
        from .middleware.asyncexitstack import AsyncExitStackMiddleware

        self.add_middleware(AsyncExitStackMiddleware)
        self.title = title
        self.version = version
        self.description = description
        self.servers = servers
        self.webhooks: Optional[list[Any]] = None
        if webhooks is not None:
            # Accept an APIRouter (FastAPI style) or a plain route list.
            self.webhooks = list(getattr(webhooks, "routes", webhooks))
        self.docs_url = docs_url
        self.redoc_url = redoc_url
        self.openapi_url = openapi_url
        self.swagger_ui_oauth2_redirect_url = swagger_ui_oauth2_redirect_url
        self.swagger_ui_parameters = swagger_ui_parameters
        self.default_response_class = default_response_class
        self.redirect_slashes = redirect_slashes
        self._router = APIRouter(dependencies=dependencies or [])
        self.router.on_startup = self._router.on_startup  # type: ignore[attr-defined]
        self.router.on_shutdown = self._router.on_shutdown  # type: ignore[attr-defined]
        if lifespan is None:
            self.router.lifespan_context = _on_event_lifespan(self.router)  # type: ignore[attr-defined]
        self._user_exception_handlers: dict[Any, Callable[..., Any]] = dict(exception_handlers or {})
        self._install_handler(StarletteHTTPException, http_exception_handler)
        self._install_handler(RequestValidationError, request_validation_exception_handler)
        for exc_class, handler in self._user_exception_handlers.items():
            self._install_handler(exc_class, handler)
        self._built_dag: Optional[CompiledDAG] = None
        self._rust_router: Any = None
        self._extra = extra
        self._sync_starlette_routes()

    # -- internal wiring -----------------------------------------------------
    def _sync_starlette_routes(self) -> None:
        # Rebuild Starlette route table from the APIRouter (idempotent):
        # drop previously generated fasterapi routes, keep the rest.
        self.router.routes = [r for r in self.router.routes if not getattr(r, "_fasterapi_owned", False)]
        for sr in self._router._starlette_routes():
            sr._fasterapi_owned = True  # type: ignore[attr-defined]
            self.router.routes.append(sr)
        # Built-in docs/openapi routes
        if self.openapi_url:
            async def _openapi(_req: Request) -> Response:
                return JSONResponse(self.openapi())

            r = StarletteRoute(self.openapi_url, _openapi, methods=["GET"], name="openapi")
            r._fasterapi_owned = True  # type: ignore[attr-defined]
            self.router.routes.append(r)
        if self.docs_url:
            from .openapi.docs import get_swagger_ui_html

            docs_url, oauth2_url = self.docs_url, self.swagger_ui_oauth2_redirect_url
            params = self.swagger_ui_parameters

            async def _docs(_req: Request) -> Response:
                return get_swagger_ui_html(
                    openapi_url=self.openapi_url or "/openapi.json",
                    title=f"{self.title} - Swagger UI",
                    oauth2_redirect_url=oauth2_url,
                    swagger_ui_parameters=params,
                )

            r = StarletteRoute(docs_url, _docs, methods=["GET"], name="docs")
            r._fasterapi_owned = True  # type: ignore[attr-defined]
            self.router.routes.append(r)
        if self.redoc_url:
            from .openapi.docs import get_redoc_html

            redoc_url = self.redoc_url

            async def _redoc(_req: Request) -> Response:
                return get_redoc_html(openapi_url=self.openapi_url or "/openapi.json",
                                      title=f"{self.title} - ReDoc")

            r = StarletteRoute(redoc_url, _redoc, methods=["GET"], name="redoc")
            r._fasterapi_owned = True  # type: ignore[attr-defined]
            self.router.routes.append(r)
        if self.swagger_ui_oauth2_redirect_url:
            from .openapi.docs import get_swagger_ui_oauth2_redirect_html

            redirect_url = self.swagger_ui_oauth2_redirect_url

            async def _oauth2_redirect(_req: Request) -> Response:
                return get_swagger_ui_oauth2_redirect_html()

            r = StarletteRoute(redirect_url, _oauth2_redirect, methods=["GET"], name="oauth2-redirect")
            r._fasterapi_owned = True  # type: ignore[attr-defined]
            self.router.routes.append(r)
        compile_routes = getattr(self.router, "compile_fast_routes", None)
        if compile_routes is not None:
            compile_routes()

    # -- decorators (FastAPI signatures) --------------------------------------
    def add_api_route(self, path: str, endpoint: Callable[..., Any], **kw: Any) -> None:
        self._router.add_api_route(path, endpoint, **kw)
        self._sync_starlette_routes()

    def api_route(self, path: str, *, methods: list[str], **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.add_api_route(path, fn, methods=methods, **kw)
            return fn

        return deco

    def get(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self.api_route(path, methods=["GET"], **kw)

    def post(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        kw.setdefault("status_code", 200)
        return self.api_route(path, methods=["POST"], **kw)

    def put(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self.api_route(path, methods=["PUT"], **kw)

    def delete(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self.api_route(path, methods=["DELETE"], **kw)

    def patch(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self.api_route(path, methods=["PATCH"], **kw)

    def options(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self.api_route(path, methods=["OPTIONS"], **kw)

    def head(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self.api_route(path, methods=["HEAD"], **kw)

    def trace(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self.api_route(path, methods=["TRACE"], **kw)

    def route(self, path: str, *, methods: list[str], **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self.api_route(path, methods=methods, **kw)

    def websocket(self, path: str, *, name: Optional[str] = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._router.websocket(path, name=name)(fn)
            self._sync_starlette_routes()
            return fn

        return deco

    def include_router(self, router: APIRouter, **kw: Any) -> None:
        self._router.include_router(router, **kw)
        self._sync_starlette_routes()

    def on_event(self, event_type: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Deprecated ``@app.on_event("startup"|"shutdown")`` parity."""
        import warnings

        warnings.warn(
            "on_event is deprecated, use lifespan event handlers instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._router.on_event(event_type)

    # -- middleware / handlers -------------------------------------------------
    def add_middleware(self, middleware_class: Any, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        # RateLimiter: share one bucket app-wide so the Hyper host can check
        # it at socket level (see serve()). Hoisting note: under FASTERAPI_HYPER
        # the bucket admits/rejects before ANY Python middleware, so its
        # effective position in the stack is outermost.
        from .guards import RateLimiter

        if middleware_class is RateLimiter and "bucket" not in kwargs:
            if getattr(self, "_edge_bucket", None) is None:
                per_second = kwargs.get("per_second", args[0] if len(args) > 0 else 100)
                burst = kwargs.get("burst", args[1] if len(args) > 1 else None)
                self._edge_bucket = _bridge.make_token_bucket(per_second, burst)
            kwargs["bucket"] = self._edge_bucket
        super().add_middleware(middleware_class, *args, **kwargs)

    def exception_handler(self, exc_class: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._user_exception_handlers[exc_class] = fn
            self._install_handler(exc_class, fn)
            return fn

        return deco

    def add_exception_handler(self, exc_class: Any, handler: Callable[..., Any]) -> None:
        self._user_exception_handlers[exc_class] = handler
        self._install_handler(exc_class, handler)

    def _install_handler(self, exc_class: Any, fn: Callable[..., Any]) -> None:
        if inspect.iscoroutinefunction(fn):
            async def _h(request: Request, exc: Any) -> Response:
                res = await fn(request, exc)
                return res if isinstance(res, Response) else JSONResponse(res)

        else:
            async def _h(request: Request, exc: Any) -> Response:
                res = fn(request, exc)
                if inspect.isawaitable(res):
                    res = await res
                return res if isinstance(res, Response) else JSONResponse(res)

        # Starlette's exception middleware uses this mapping for route errors.
        super().add_exception_handler(exc_class, _h)  # type: ignore[arg-type]

    # -- openapi ---------------------------------------------------------------
    def openapi(self) -> dict[str, Any]:
        from .openapi.utils import get_openapi

        return get_openapi(
            title=self.title,
            version=self.version,
            description=self.description,
            routes=self._router.routes,
            servers=self.servers,
            webhooks=self.webhooks,
        )

    # -- Phase-2: AOT build ------------------------------------------------------
    def build(self) -> CompiledDAG:
        """Traverse ``Depends()`` graphs once, compile Rust radix router + DAG.

        Safe to call multiple times; returns the compiled plan. Warms the
        SIMD JSON path so the first request never pays init cost.
        """
        endpoints = [r.endpoint for r in self._router.routes]
        dag = compile_app_dag(endpoints)
        # The live dispatch tree lives on FastRouter; rebuild so build() also
        # double-checks every pattern inserted cleanly.
        compile_routes = getattr(self.router, "compile_fast_routes", None)
        if compile_routes is not None:
            compile_routes()
        router = getattr(self.router, "_fr_router", None) or _bridge.make_router()
        try:
            _bridge.fast_json_dumps({"warmup": True})
            _bridge.fast_json_loads('{"warmup":true}')
        except Exception:
            pass
        self._built_dag = dag
        self._rust_router = router
        return dag

    @property
    def built_dag(self) -> Optional[CompiledDAG]:
        return self._built_dag

    # -- serve -------------------------------------------------------------------
    def serve(self, host: str = "127.0.0.1", port: int = 8000, **kw: Any) -> None:
        """Run the app: Rust Hyper host when ``FASTERAPI_HYPER=1``, else uvicorn.

        The Hyper host owns sockets + HTTP parsing in Rust and drives this
        same app object in-process (lifespan included), so bytes stay
        identical to uvicorn. Spike limits are documented in ``src/lib.rs``.
        """
        import os

        self.build()
        if os.environ.get("FASTERAPI_HYPER") == "1" and _bridge.HAS_RUST:
            try:
                from ._fasterapi_rs import serve_asgi  # type: ignore
            except ImportError:
                from _fasterapi_rs import serve_asgi  # type: ignore

            workers = kw.pop("workers", 0) or None
            print(f"fasterapi: serving on http://{host}:{port} via Rust Hyper host", flush=True)
            # Rust owns SIGINT/SIGTERM during serve (graceful drain). Neutralize
            # Python's competing disposition so Ctrl-C can't surface as a stray
            # KeyboardInterrupt mid-shutdown; restored afterwards.
            import signal as _signal

            try:
                _prev_int = _signal.getsignal(_signal.SIGINT)
                _signal.signal(_signal.SIGINT, lambda *_a: None)
            except Exception:
                _prev_int = None
            try:
                serve_asgi(self, host, port, workers,
                           getattr(self, "_edge_bucket", None))
            finally:
                if _prev_int is not None:
                    try:
                        _signal.signal(_signal.SIGINT, _prev_int)
                    except Exception:
                        pass
            return
        import uvicorn

        uvicorn.run(self, host=host, port=port, **kw)
