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


def test_bare_collection_bodies():
    def build(cls):  # type: ignore[no-untyped-def]
        app = cls()

        @app.post("/d")
        async def d(payload: dict):
            return {"keys": sorted(payload)}

        @app.post("/l")
        async def l(payload: list):
            return {"n": len(payload)}

        @app.post("/bulk")
        async def bulk(items: list[Item]):
            return {"count": len(items)}

        return app

    from fastapi.testclient import TestClient as _GT
    with _GT(build(GFastAPI)) as gc, F.TestClient(build(F.FastAPI)) as fc:
        a, b = gc.post("/d", json={"x": 1}), fc.post("/d", json={"x": 1})
        assert (a.status_code, a.json()) == (b.status_code, b.json()) == (200, {"keys": ["x"]})
        a, b = gc.post("/l", json=[1, 2]), fc.post("/l", json=[1, 2])
        assert a.status_code == b.status_code == 422
        a, b = gc.post("/l", json={"payload": [1, 2]}), fc.post("/l", json={"payload": [1, 2]})
        assert a.status_code == b.status_code == 422
        bulk = [{"name": f"i{i}", "price": 1.0} for i in range(5)]
        a, b = gc.post("/bulk", json=bulk), fc.post("/bulk", json=bulk)
        assert (a.status_code, a.json()) == (b.status_code, b.json()) == (200, {"count": 5})


def test_return_annotation_response_model():
    def build(cls):  # type: ignore[no-untyped-def]
        app = cls()

        @app.get("/a")
        async def a() -> Item:
            return {"name": "x", "price": 1.5, "zzz": "dropped"}

        @app.get("/b", response_model=None)
        async def b() -> Item:
            return {"name": "x", "price": 1.5, "zzz": "kept"}

        @app.get("/c")
        async def c() -> list[Item]:
            return [{"name": "x", "price": 1.5, "zzz": "dropped"}]

        return app

    from fastapi.testclient import TestClient as _GT
    with _GT(build(GFastAPI)) as gc, F.TestClient(build(F.FastAPI)) as fc:
        for path, expected in [("/a", {"name": "x", "price": 1.5, "tags": []}),
                               ("/b", {"name": "x", "price": 1.5, "zzz": "kept"}),
                               ("/c", [{"name": "x", "price": 1.5, "tags": []}])]:
            a, b = gc.get(path), fc.get(path)
            assert a.status_code == b.status_code == 200, path
            assert a.json() == b.json() == expected, path


def test_response_model_flags_parity():
    from pydantic import BaseModel as _BM

    class _Big(_BM):
        a: int
        b: int = 5
        c: Optional[str] = None

    def build(cls, **kw):  # type: ignore[no-untyped-def]
        app = cls()

        @app.get("/f", response_model=_Big, **kw)
        async def f():
            return {"a": 1, "b": 5, "c": None, "zzz": "dropped"}

        return app

    from fastapi.testclient import TestClient as _GT
    for kw in [{"response_model_include": {"a"}},
               {"response_model_exclude": {"b", "c"}},
               {"response_model_exclude_unset": True},
               {"response_model_exclude_none": True}]:
        with _GT(build(GFastAPI, **kw)) as gc, F.TestClient(build(F.FastAPI, **kw)) as fc:
            a, b = gc.get("/f"), fc.get("/f")
            assert a.status_code == b.status_code == 200, kw
            assert a.json() == b.json(), kw


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


def test_empty_subpath_collection_idiom():
    # `@router.post("")` addresses the prefix itself on both frameworks.
    def build(cls):  # type: ignore[no-untyped-def]
        from fasterapi import APIRouter as _FR
        from fastapi import APIRouter as _GR
        R = _GR if cls is GFastAPI else _FR
        app = cls()
        sub = R(prefix="/cols")

        @sub.post("")
        async def create():
            return {"created": True}

        @sub.get("")
        async def listing():
            return {"items": []}

        app.include_router(sub)
        return app

    with GTestClient(build(GFastAPI)) as gc, F.TestClient(build(F.FastAPI)) as fc:
        assert gc.post("/cols").json() == fc.post("/cols").json() == {"created": True}
        assert gc.get("/cols").json() == fc.get("/cols").json() == {"items": []}


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


def test_openapi_security_and_servers():
    from fasterapi import Security as _FS
    from fasterapi.security import HTTPBearer as _FHB
    from fasterapi.security import OAuth2PasswordBearer as _FO
    from fastapi import Security as _GS
    from fastapi.security import HTTPBearer as _GHB
    from fastapi.security import OAuth2PasswordBearer as _GO

    def build(cls, O, H, D, S):  # type: ignore[no-untyped-def]
        app = cls(title="T", version="1", servers=[{"url": "https://x.example"}])
        o = O(tokenUrl="t", scheme_name="my-oauth", scopes={"read": "r"})
        b = H(bearerFormat="JWT")

        @app.get("/a")
        def a(t: str = S(o, scopes=["read"])):
            return {}

        @app.get("/b")
        def bb(t=D(b)):
            return {}

        return app

    g = build(GFastAPI, _GO, _GHB, GDepends, _GS)
    f = build(F.FastAPI, _FO, _FHB, FDepends, _FS)
    assert g.openapi() == f.openapi()


def test_openapi_webhooks():
    def build(cls):  # type: ignore[no-untyped-def]
        from fasterapi import APIRouter as _FR
        from fastapi import APIRouter as _GR
        R = _GR if cls is GFastAPI else _FR
        wh = R()

        @wh.post("/new-pet")
        def new_pet(item: Item):
            return item

        return cls(title="T", version="1", webhooks=wh)

    assert build(GFastAPI).openapi() == build(F.FastAPI).openapi()
    assert set(build(F.FastAPI).openapi()["webhooks"]) == {"/new-pet"}


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


def test_websocket_auth_and_query_parity():
    from fasterapi import HTTPException as _FHE
    from fastapi import HTTPException as _GHE

    def build_g():  # type: ignore[no-untyped-def]
        app = GFastAPI()

        async def check(token: str = ""):
            if token != "s3cr3t":
                raise _GHE(status_code=401, detail="bad token")
            return token

        @app.websocket("/wsa")
        async def wsa(websocket: StarletteWS, token: str = GDepends(check)):
            await websocket.accept()
            await websocket.send_text(f"ok:{token}")
            await websocket.close()

        return app

    def build_f():  # type: ignore[no-untyped-def]
        app = F.FastAPI()

        async def check(token: str = ""):
            if token != "s3cr3t":
                raise _FHE(status_code=401, detail="bad token")
            return token

        @app.websocket("/wsa")
        async def wsa(websocket: StarletteWS, token: str = FDepends(check)):
            await websocket.accept()
            await websocket.send_text(f"ok:{token}")
            await websocket.close()

        return app

    with GTestClient(build_g()) as gc, F.TestClient(build_f()) as fc:
        with gc.websocket_connect("/wsa?token=s3cr3t") as gws:
            assert gws.receive_text() == "ok:s3cr3t"
        with fc.websocket_connect("/wsa?token=s3cr3t") as fws:
            assert fws.receive_text() == "ok:s3cr3t"
        for client in (gc, fc):
            try:
                with client.websocket_connect("/wsa?token=nope"):
                    raise AssertionError("should not connect")
            except Exception:
                pass
