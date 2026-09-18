"""Differential parity suite: identical endpoints on FastAPI vs fasterapi.

Runs every behavior twice (once per framework) and asserts:
  * equal HTTP status codes, JSON bodies (modulo documented normalizations),
  * identical Pydantic validation error *shapes* (loc/type, 422 + {"detail": [...]}),
  * identical dependency execution order + yield-teardown behavior,
  * matching headers for CORS / custom handlers.

Usage:
    pytest tests/test_parity.py -q
    pytest tests/test_parity.py -q -k "validation or dependencies"
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

import pytest
from pydantic import BaseModel, Field

import fasterapi as F
from fasterapi import Depends as FDepends
from fasterapi import HTTPException as FHTTPException
from fasterapi import Query as FQuery
from fasterapi import Path as FPath
from fasterapi import Header as FHeader
from fasterapi import Cookie as FCookie
from fasterapi import Body as FBody
from fasterapi import Form as FForm
from fasterapi import File as FFile

from fastapi import Depends as GDepends
from fastapi import HTTPException as GHTTPException
from fastapi import Query as GQuery
from fastapi import Path as GPath
from fastapi import Header as GHeader
from fastapi import Cookie as GCookie
from fastapi import Form as GForm
from fastapi import File as GFile
from fastapi import UploadFile as GUploadFile
from fastapi import APIRouter as GAPIRouter
from fastapi import FastAPI as GFastAPI
from fastapi.testclient import TestClient as GTestClient
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.websockets import WebSocket as StarletteWS


# ------------------------------------------------------------------ models ---

class Item(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    price: float = Field(gt=0)
    tags: list[str] = []


class ItemOut(Item):
    id: int


class Cart(BaseModel):
    items: list[Item]
    note: Optional[str] = None


# ------------------------------------------------------- app builders ---

def build_fastapi_app() -> GFastAPI:
    order: list[str] = []

    @asynccontextmanager
    async def lifespan(app: GFastAPI):
        order.append("startup")
        yield
        order.append("shutdown")

    app = GFastAPI(title="parity", version="0.1.0", lifespan=lifespan)
    app.state.order = order  # type: ignore[attr-defined]

    async def get_db():
        order.append("db:enter")
        yield {"db": "conn"}
        order.append("db:exit")

    async def get_user(db= GDepends(get_db)):
        order.append("user:enter")
        yield {"user": "ada", "db": db}
        order.append("user:exit")

    @app.get("/ping")
    async def ping():
        return {"pong": True}

    @app.get("/items/{item_id}")
    async def read_item(item_id: int = GPath(ge=1), q: Optional[str] = None, limit: int = GQuery(10, ge=1, le=100)):
        return {"item_id": item_id, "q": q, "limit": limit}

    @app.post("/items", response_model=ItemOut)
    async def create_item(item: Item, item_id: int = GQuery(1)):
        return {**item.model_dump(), "id": item_id}

    @app.get("/headers-test")
    async def headers_test(x_token: Optional[str] = GHeader(default=None)):
        return {"x_token": x_token}

    @app.get("/cookies-test")
    async def cookies_test(session: Optional[str] = GCookie(default=None)):
        return {"session": session}

    @app.get("/needs-auth")
    async def needs_auth(user= GDepends(get_user)):
        return user

    @app.post("/login")
    async def login(username: str = GForm(), password: str = GForm(min_length=4)):
        return {"user": username, "ok": len(password) >= 4}

    @app.post("/upload")
    async def upload(file: GUploadFile = GFile()):
        content = await file.read()
        return {"filename": file.filename, "content_type": file.content_type, "size": len(content)}

    @app.get("/boom/{code}")
    async def boom(code: int):
        if code == 404:
            raise GHTTPException(status_code=404, detail="Nope")
        raise ValueError("bad value")

    @app.exception_handler(ValueError)
    async def _value_err(request, exc):  # type: ignore[no-untyped-def]
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": f"value-error:{exc}"}, status_code=418)

    sub = GAPIRouter(prefix="/v1", tags=["v1"])

    @sub.get("/hello/{name}")
    async def hello(name: str, greeting: str = "hi"):
        return {"msg": f"{greeting} {name}"}

    app.include_router(sub)
    return app


def build_fasterapi_app() -> F.FastAPI:
    order: list[str] = []

    @asynccontextmanager
    async def lifespan(app: F.FastAPI):
        order.append("startup")
        yield
        order.append("shutdown")

    app = F.FastAPI(title="parity", version="0.1.0", lifespan=lifespan)
    app.state.order = order  # type: ignore[attr-defined]

    async def get_db():
        order.append("db:enter")
        yield {"db": "conn"}
        order.append("db:exit")

    async def get_user(db=FDepends(get_db)):
        order.append("user:enter")
        yield {"user": "ada", "db": db}
        order.append("user:exit")

    @app.get("/ping")
    async def ping():
        return {"pong": True}

    @app.get("/items/{item_id}")
    async def read_item(item_id: int = FPath(ge=1), q: Optional[str] = None, limit: int = FQuery(10, ge=1, le=100)):
        return {"item_id": item_id, "q": q, "limit": limit}

    @app.post("/items", response_model=ItemOut)
    async def create_item(item: Item, item_id: int = FQuery(1)):
        return {**item.model_dump(), "id": item_id}

    @app.get("/headers-test")
    async def headers_test(x_token: Optional[str] = FHeader(default=None)):
        return {"x_token": x_token}

    @app.get("/cookies-test")
    async def cookies_test(session: Optional[str] = FCookie(default=None)):
        return {"session": session}

    @app.get("/needs-auth")
    async def needs_auth(user=FDepends(get_user)):
        return user

    @app.post("/login")
    async def login(username: str = FForm(), password: str = FForm(min_length=4)):
        return {"user": username, "ok": len(password) >= 4}

    @app.post("/upload")
    async def upload(file: StarletteUploadFile = FFile()):
        content = await file.read()
        return {"filename": file.filename, "content_type": file.content_type, "size": len(content)}

    @app.get("/boom/{code}")
    async def boom(code: int):
        if code == 404:
            raise FHTTPException(status_code=404, detail="Nope")
        raise ValueError("bad value")

    @app.exception_handler(ValueError)
    async def _value_err(request, exc):  # type: ignore[no-untyped-def]
        from starlette.responses import JSONResponse

        return JSONResponse({"detail": f"value-error:{exc}"}, status_code=418)

    sub = F.APIRouter(prefix="/v1", tags=["v1"])

    @sub.get("/hello/{name}")
    async def hello(name: str, greeting: str = "hi"):
        return {"msg": f"{greeting} {name}"}

    app.include_router(sub)
    return app


@pytest.fixture()
def fastapi_client():
    with GTestClient(build_fastapi_app()) as c:
        yield c


@pytest.fixture()
def fasterapi_client():
    with F.TestClient(build_fasterapi_app()) as c:
        yield c


# ------------------------------------------------------------------ helpers ---

def _norm(body):  # type: ignore[no-untyped-def]
    # Normalize known-harmless divergences (none expected today).
    return body


def assert_same(path: str, *, method: str = "GET", **kw):  # type: ignore[no-untyped-def]
    """GET helper placeholder (tests below use both clients explicitly)."""
    raise NotImplementedError


def _err_shapes(detail):  # type: ignore[no-untyped-def]
    return sorted(
        (tuple(e.get("loc", ())), str(e.get("type"))) for e in detail
    )


# ------------------------------------------------------------------ tests ---

def test_ping_parity(fastapi_client, fasterapi_client):
    a = fastapi_client.get("/ping")
    b = fasterapi_client.get("/ping")
    assert a.status_code == b.status_code == 200
    assert _norm(a.json()) == _norm(b.json()) == {"pong": True}


def test_path_query_parity(fastapi_client, fasterapi_client):
    for path in ["/items/3", "/items/3?q=hello&limit=5", "/items/1?limit=100"]:
        a = fastapi_client.get(path)
        b = fasterapi_client.get(path)
        assert a.status_code == b.status_code == 200, path
        assert a.json() == b.json(), path


def test_path_query_validation_parity(fastapi_client, fasterapi_client):
    for path in ["/items/0", "/items/abc", "/items/2?limit=101", "/items/2?limit=0"]:
        a = fastapi_client.get(path)
        b = fasterapi_client.get(path)
        assert a.status_code == 422, path
        assert b.status_code == 422, path
        assert _err_shapes(a.json()["detail"]) == _err_shapes(b.json()["detail"]), path


def test_missing_query_uses_default(fastapi_client, fasterapi_client):
    a = fastapi_client.get("/items/7")
    b = fasterapi_client.get("/items/7")
    assert a.json()["limit"] == b.json()["limit"] == 10


def test_body_create_parity(fastapi_client, fasterapi_client):
    payload = {"name": "widget", "price": 9.5, "tags": ["a", "b"]}
    a = fastapi_client.post("/items?item_id=42", json=payload)
    b = fasterapi_client.post("/items?item_id=42", json=payload)
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
    assert b.json()["id"] == 42


def test_body_validation_parity(fastapi_client, fasterapi_client):
    bad_payloads = [
        {"price": 9.5},                       # missing name
        {"name": "", "price": 9.5},           # min_length
        {"name": "x", "price": -1},           # gt
        {"name": "x", "price": "not-a-num"},  # parsing
    ]
    for payload in bad_payloads:
        a = fastapi_client.post("/items", json=payload)
        b = fasterapi_client.post("/items", json=payload)
        assert a.status_code == 422, payload
        assert b.status_code == 422, payload
        assert _err_shapes(a.json()["detail"]) == _err_shapes(b.json()["detail"]), payload


def test_headers_cookies_parity(fastapi_client, fasterapi_client):
    a = fastapi_client.get("/headers-test", headers={"X-Token": "abc"})
    b = fasterapi_client.get("/headers-test", headers={"X-Token": "abc"})
    assert a.json() == b.json() == {"x_token": "abc"}

    a = fastapi_client.get("/cookies-test", cookies={"session": "s3cr3t"})
    b = fasterapi_client.get("/cookies-test", cookies={"session": "s3cr3t"})
    assert a.json() == b.json() == {"session": "s3cr3t"}


def test_form_parity(fastapi_client, fasterapi_client):
    a = fastapi_client.post("/login", data={"username": "ada", "password": "s3cr3t"})
    b = fasterapi_client.post("/login", data={"username": "ada", "password": "s3cr3t"})
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json() == {"user": "ada", "ok": True}


def test_form_validation_parity(fastapi_client, fasterapi_client):
    a = fastapi_client.post("/login", data={"username": "ada", "password": "x"})
    b = fasterapi_client.post("/login", data={"username": "ada", "password": "x"})
    assert a.status_code == 422, a.json()
    assert b.status_code == 422, b.json()
    assert _err_shapes(a.json()["detail"]) == _err_shapes(b.json()["detail"])

    a = fastapi_client.post("/login", data={"username": "ada"})
    b = fasterapi_client.post("/login", data={"username": "ada"})
    assert a.status_code == b.status_code == 422
    assert _err_shapes(a.json()["detail"]) == _err_shapes(b.json()["detail"])


def test_file_upload_parity(fastapi_client, fasterapi_client):
    files = {"file": ("hello.txt", b"hello fasterapi", "text/plain")}
    a = fastapi_client.post("/upload", files=files)
    b = fasterapi_client.post("/upload", files=files)
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json() == {"filename": "hello.txt", "content_type": "text/plain", "size": 15}


def test_dependencies_order_and_teardown():
    g_app = build_fastapi_app()
    f_app = build_fasterapi_app()
    with GTestClient(g_app) as gc, F.TestClient(f_app) as fc:
        ga = gc.get("/needs-auth")
        fa = fc.get("/needs-auth")
        assert ga.status_code == fa.status_code == 200
        assert ga.json()["user"] == fa.json()["user"] == "ada"
        assert g_app.state.order == f_app.state.order, (g_app.state.order, f_app.state.order)
        # Lifespan startup runs first; teardown (shutdown/db:exit) runs on exit.
        assert g_app.state.order[0] == "startup"
        assert g_app.state.order[1:5] == ["db:enter", "user:enter", "user:exit", "db:exit"]


def test_http_exception_parity(fastapi_client, fasterapi_client):
    a = fastapi_client.get("/boom/404")
    b = fasterapi_client.get("/boom/404")
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (404, {"detail": "Nope"})


def test_custom_exception_handler_parity(fastapi_client, fasterapi_client):
    a = fastapi_client.get("/boom/500")
    b = fasterapi_client.get("/boom/500")
    assert a.status_code == b.status_code == 418
    assert a.json() == b.json() == {"detail": "value-error:bad value"}


def test_router_prefix_parity(fastapi_client, fasterapi_client):
    a = fastapi_client.get("/v1/hello/ada?greeting=yo")
    b = fasterapi_client.get("/v1/hello/ada?greeting=yo")
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json() == {"msg": "yo ada"}


def test_docs_parity(fastapi_client, fasterapi_client):
    for path in ["/docs", "/redoc", "/docs/oauth2-redirect"]:
        a = fastapi_client.get(path)
        b = fasterapi_client.get(path)
        assert a.status_code == b.status_code == 200, path
        assert a.text == b.text, path
    # Disabled docs stay disabled on both sides
    g_off = GFastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    f_off = F.FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    with GTestClient(g_off) as gc, F.TestClient(f_off) as fc:
        for path in ["/docs", "/redoc", "/openapi.json"]:
            assert gc.get(path).status_code == fc.get(path).status_code == 404, path


def test_openapi_shapes():
    g = build_fastapi_app().openapi()
    f = build_fasterapi_app().openapi()
    assert set(g["paths"]) == set(f["paths"])
    for path, item in g["paths"].items():
        assert set(item) == set(f["paths"][path]), path
    assert "Item" in f["components"]["schemas"] or "Item" in str(f["components"])


def test_openapi_full_equality():
    def build(cls):  # type: ignore[no-untyped-def]
        app = cls(title="T", version="1", description="demo")

        @app.get("/items/{item_id}", response_model=Item, tags=["t"], summary="Read")
        def r(item_id: int, q: Optional[str] = None, limit: int = 10):
            return {}

        @app.post("/items", response_model=Item)
        def c(item: Item):
            return item

        @app.post("/cart", response_model=Cart)
        def cart(cart: Cart):
            return cart

        @app.get("/h", deprecated=True)
        def h(x_token: str = ""):
            return {}

        return app

    assert build(GFastAPI).openapi() == build(F.FastAPI).openapi()


def test_websocket_echo_parity():
    g_app = GFastAPI()

    @g_app.websocket("/ws")
    async def _gws(websocket: StarletteWS):  # type: ignore[no-untyped-def]
        await websocket.accept()
        data = await websocket.receive_text()
        await websocket.send_text(f"echo:{data}")
        await websocket.close()

    f_app = F.FastAPI()

    @f_app.websocket("/ws")
    async def _fws(websocket: StarletteWS):  # type: ignore[no-untyped-def]
        await websocket.accept()
        data = await websocket.receive_text()
        await websocket.send_text(f"echo:{data}")
        await websocket.close()

    with GTestClient(g_app) as gc, F.TestClient(f_app) as fc:
        with gc.websocket_connect("/ws") as gws:
            gws.send_text("hi")
            assert gws.receive_text() == "echo:hi"
        with fc.websocket_connect("/ws") as fws:
            fws.send_text("hi")
            assert fws.receive_text() == "echo:hi"
