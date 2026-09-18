"""fasterapi.applications — drop-in ``fastapi.FastAPI``.

``FastAPI`` subclasses Starlette (same ASGI engine FastAPI uses) so
middleware, lifespan, exception handlers and the TestClient behave 1:1,
while ``build()`` AOT-compiles ``Depends()`` graphs + the Rust radix router
and ``serve()`` launches the host (uvicorn today, Hyper host when staged).
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route as StarletteRoute

from . import _rust_bridge as _bridge
from .dag import CompiledDAG, compile_app_dag
from .exceptions import HTTPException, RequestValidationError
from .params import Depends
from .routing import APIRouter


class FastAPI(Starlette):
    """1:1 ``fastapi.FastAPI`` replacement."""

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
        **extra: Any,
    ) -> None:
        super().__init__(lifespan=lifespan, middleware=middleware)  # type: ignore[call-arg]
        self.title = title
        self.version = version
        self.description = description
        self.docs_url = docs_url
        self.redoc_url = redoc_url
        self.openapi_url = openapi_url
        self.swagger_ui_oauth2_redirect_url = swagger_ui_oauth2_redirect_url
        self.swagger_ui_parameters = swagger_ui_parameters
        self.default_response_class = default_response_class
        self.redirect_slashes = redirect_slashes
        self._router = APIRouter(dependencies=dependencies or [])
        self._user_exception_handlers: dict[Any, Callable[..., Any]] = dict(exception_handlers or {})
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

    # -- middleware / handlers -------------------------------------------------
    def add_middleware(self, middleware_class: Any, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
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

        # Starlette ≥0.37 / 1.x keeps handlers on the app (not the router).
        try:
            super().add_exception_handler(exc_class, _h)  # type: ignore[arg-type]
        except Exception:
            pass
        try:
            self.exception_handlers[exc_class] = _h  # type: ignore[attr-defined]
        except Exception:
            pass

    async def _default_http_exception(self, request: Request, exc: HTTPException) -> Response:
        handler = self._user_exception_handlers.get(HTTPException) or self._user_exception_handlers.get(Exception)
        if handler is not None:
            res = handler(request, exc)
            if inspect.isawaitable(res):
                res = await res
            return res if isinstance(res, Response) else JSONResponse(res)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers or {})

    # -- openapi ---------------------------------------------------------------
    def openapi(self) -> dict[str, Any]:
        from .openapi.utils import get_openapi

        return get_openapi(
            title=self.title,
            version=self.version,
            description=self.description,
            routes=self._router.routes,
        )

    # -- Phase-2: AOT build ------------------------------------------------------
    def build(self) -> CompiledDAG:
        """Traverse ``Depends()`` graphs once, compile Rust radix router + DAG.

        Safe to call multiple times; returns the compiled plan. Warms the
        SIMD JSON path so the first request never pays init cost.
        """
        endpoints = [r.endpoint for r in self._router.routes]
        dag = compile_app_dag(endpoints)
        router = _bridge.make_router()
        for r in self._router.routes:
            # matchit uses `{param}` syntax already; starlette `{param}` matches.
            pattern = r.path
            try:
                if hasattr(router, "insert"):
                    router.insert(pattern, f"{sorted(r.methods)[0]} {pattern}")
            except Exception:
                pass
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
            serve_asgi(self, host, port, workers)
            return
        import uvicorn

        uvicorn.run(self, host=host, port=port, **kw)
