"""Extended differential parity: edge shapes + new feature coverage.

Each test builds the *same* app twice (fastapi / fasterapi) and asserts
identical wire behavior. Internal-only features (rate limiter, fast router)
get direct tests.
"""

from __future__ import annotations

import asyncio
import enum
import json
import time
import warnings

import fastapi as _fa
import fasterapi as _f2
import pytest
from pydantic import BaseModel, Field


def _both():
    return _fa.FastAPI(), _f2.FastAPI()


def _client(app):
    from starlette.testclient import TestClient
    return TestClient(app, raise_server_exceptions=False)


def _get(app, path):
    return _client(app).get(path)


def _post(app, path, **kw):
    return _client(app).post(path, **kw)


# --------------------------------------------------------- error collection ---

def test_multi_dep_errors_collected():
    """Errors across deps + params are all returned (not just the first)."""
    for mod in (_fa, _f2):
        app = mod.FastAPI()

        async def d1(x: int = mod.Query(...)):
            return x

        async def d2(y: int = mod.Query(...)):
            return y

        @app.get("/t")
        def t(a=mod.Depends(d1), b=mod.Depends(d2), q: int = mod.Query(...)):
            return {}  # pragma: no cover

        detail = _get(app, "/t?x=1").json()["detail"]
        assert [e["loc"] for e in detail] == [["query", "y"], ["query", "q"]], mod

        detail = _get(app, "/t").json()["detail"]
        assert [e["loc"] for e in detail] == [
            ["query", "x"], ["query", "y"], ["query", "q"]], mod


def test_required_query_param_missing():
    """A required Query(...) with no default 422s instead of leaking Ellipsis."""
    for mod in (_fa, _f2):
        app = mod.FastAPI()

        @app.get("/q")
        def q(x: int = mod.Query(...)):
            return {"x": x}  # pragma: no cover

        r = _get(app, "/q")
        assert r.status_code == 422, mod
        assert r.json()["detail"][0]["loc"] == ["query", "x"], mod


def test_required_header_cookie_missing():
    for mod in (_fa, _f2):
        app = mod.FastAPI()

        @app.get("/h")
        def h(tok: str = mod.Header(...), sess: str = mod.Cookie(...)):
            return {}  # pragma: no cover

        r = _get(app, "/h")
        assert r.status_code == 422, mod
        locs = {tuple(e["loc"]) for e in r.json()["detail"]}
        assert ("header", "tok") in locs and ("cookie", "sess") in locs, mod


# ----------------------------------------------------------- parallel deps ---

def test_independent_deps_run_concurrently():
    """Two 60ms async deps resolve in ~60ms, not ~120ms (DI fan-out)."""
    app = _f2.FastAPI()

    async def a():
        await asyncio.sleep(0.06)
        return 1

    async def b():
        await asyncio.sleep(0.06)
        return 2

    @app.get("/d")
    def d(x=Depends_shim(a), y=Depends_shim(b)):
        return {"x": x, "y": y}

    t0 = time.monotonic()
    r = _client(app).get("/d")
    elapsed = time.monotonic() - t0
    assert r.json() == {"x": 1, "y": 2}
    assert elapsed < 0.115, f"deps ran sequentially: {elapsed:.3f}s"


def Depends_shim(fn):
    return _f2.Depends(fn)


def test_shared_dep_computed_once():
    """use_cache dep shared by two siblings runs exactly once."""
    calls = []
    app = _f2.FastAPI()

    async def shared():
        calls.append(1)
        await asyncio.sleep(0.01)
        return "v"

    async def l(s=_f2.Depends(shared)):
        return s

    async def r(s=_f2.Depends(shared)):
        return s

    @app.get("/s")
    def ep(a=_f2.Depends(l), b=_f2.Depends(r)):
        return {"a": a, "b": b}

    assert _client(app).get("/s").json() == {"a": "v", "b": "v"}
    assert len(calls) == 1


def test_dep_exception_deterministic():
    """HTTPException from an earlier dep aborts — same as sequential solve."""
    app = _f2.FastAPI()

    async def boom():
        raise _f2.HTTPException(status_code=401, detail="nope")

    async def ok():
        return 1

    @app.get("/e")
    def ep(a=_f2.Depends(boom), b=_f2.Depends(ok)):
        return {}  # pragma: no cover

    r = _client(app).get("/e")
    assert r.status_code == 401
    assert r.json() == {"detail": "nope"}


def test_yield_dep_ordering_preserved():
    """Yield deps stay sequential: teardown order is LIFO entry order."""
    app = _f2.FastAPI()
    order = []

    async def y1():
        order.append("y1-in")
        yield "1"
        order.append("y1-out")

    async def y2():
        order.append("y2-in")
        yield "2"
        order.append("y2-out")

    @app.get("/y")
    def ep(a=_f2.Depends(y1), b=_f2.Depends(y2)):
        return {"a": a, "b": b}

    assert _client(app).get("/y").json() == {"a": "1", "b": "2"}
    assert order == ["y1-in", "y2-in", "y2-out", "y1-out"]


# --------------------------------------------------------------- mounting ---

def test_mount_sub_app():
    for mod in (_fa, _f2):
        app = mod.FastAPI()

        async def sub(scope, receive, send):
            from starlette.responses import PlainTextResponse
            await PlainTextResponse("mounted")(scope, receive, send)

        app.mount("/sub", sub)

        @app.get("/ping")
        def ping():
            return {"pong": True}

        assert _get(app, "/sub").text == "mounted", mod
        assert _get(app, "/ping").json() == {"pong": True}, mod


def test_mount_staticfiles():
    import os
    import tempfile

    from starlette.staticfiles import StaticFiles

    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "f.txt"), "w") as fh:
            fh.write("static-bytes")
        for mod in (_fa, _f2):
            app = mod.FastAPI()
            app.mount("/static", StaticFiles(directory=d))
            r = _get(app, "/static/f.txt")
            assert r.status_code == 200 and r.text == "static-bytes", mod


# ---------------------------------------------------------------- on_event ---

def test_on_event_startup_shutdown():
    for mod in (_fa, _f2):
        app = mod.FastAPI()
        log = []
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            @app.on_event("startup")
            def _s():
                log.append("up")

            @app.on_event("shutdown")
            def _d():
                log.append("down")

        @app.get("/x")
        def x():
            return {}

        with _client(app) as c:
            c.get("/x")
        assert log == ["up", "down"], mod


# ------------------------------------------------------------- route kwargs ---

def test_include_in_schema():
    for mod in (_fa, _f2):
        app = mod.FastAPI()

        @app.get("/shown")
        def shown():
            return {}

        @app.get("/hidden", include_in_schema=False)
        def hidden():
            return {"ok": True}

        spec = app.openapi()
        assert "/shown" in spec["paths"] and "/hidden" not in spec["paths"], mod
        assert _get(app, "/hidden").json() == {"ok": True}, mod  # still serves


def test_openapi_extra_and_responses():
    for mod in (_fa, _f2):
        app = mod.FastAPI()

        @app.get("/e", openapi_extra={"x-custom": "yes"},
                 responses={418: {"description": "teapot"}})
        def e():
            return {}

        op = app.openapi()["paths"]["/e"]["get"]
        assert op["x-custom"] == "yes", mod
        assert op["responses"]["418"]["description"] == "teapot", mod
        assert "200" in op["responses"], mod


def test_callbacks_in_openapi():
    for mod in (_fa, _f2):
        app = mod.FastAPI()
        cb = mod.APIRouter()

        @cb.post("{$callback_url}/done")
        def done():
            return {}

        @app.get("/trigger", callbacks=[cb])
        def trig():
            return {}

        op = app.openapi()["paths"]["/trigger"]["get"]
        if mod is _f2:
            assert "done" in op.get("callbacks", {}), mod


# ------------------------------------------------------------ edge validation

class _Color(enum.Enum):
    red = "red"
    blue = "blue"


class _Inner(BaseModel):
    v: int


class _Outer(BaseModel):
    name: str = Field(min_length=2)
    inner: _Inner
    tags: list[str] = []


def test_union_and_optional_params():
    for mod in (_fa, _f2):
        app = mod.FastAPI()

        @app.get("/u")
        def u(x: int | None = mod.Query(default=None)):
            return {"x": x}

        assert _get(app, "/u").json() == {"x": None}, mod
        assert _get(app, "/u?x=5").json() == {"x": 5}, mod


def test_enum_param():
    for mod in (_fa, _f2):
        app = mod.FastAPI()

        @app.get("/c")
        def c(color: _Color = mod.Query(...)):
            return {"color": color.value}

        assert _get(app, "/c?color=red").json() == {"color": "red"}, mod
        assert _get(app, "/c?color=zzz").status_code == 422, mod


def test_nested_model_body():
    for mod in (_fa, _f2):
        app = mod.FastAPI()

        @app.post("/n")
        def n(item: _Outer):
            return item

        good = {"name": "ok", "inner": {"v": 3}, "tags": ["a"]}
        assert _post(app, "/n", json=good).json() == good, mod

        bad = {"name": "x", "inner": {"v": "no"}}
        r = _post(app, "/n", json=bad)
        assert r.status_code == 422, mod
        locs = [tuple(e["loc"]) for e in r.json()["detail"]]
        assert any("inner" in loc for loc in locs), mod


def test_alias_query_param():
    for mod in (_fa, _f2):
        app = mod.FastAPI()

        @app.get("/a")
        def a(item_id: int = mod.Query(alias="item-id")):
            return {"item_id": item_id}

        assert _get(app, "/a?item-id=7").json() == {"item_id": 7}, mod
        assert _get(app, "/a?item_id=7").status_code == 422, mod


# ------------------------------------------------------------ fast router ---

def test_route_ordering_param_before_literal():
    """Registration order wins: /items/{id} before /items/list → 'list' is id."""
    for mod in (_fa, _f2):
        app = mod.FastAPI()

        @app.get("/items/{item_id}")
        def read(item_id: str):
            return {"id": item_id}

        @app.get("/items/list")
        def lst():
            return {"list": True}

        # Param route registered first wins in BOTH frameworks.
        assert _get(app, "/items/list").json() == {"id": "list"}, mod


def test_route_ordering_literal_before_param():
    for mod in (_fa, _f2):
        app = mod.FastAPI()

        @app.get("/items/list")
        def lst():
            return {"list": True}

        @app.get("/items/{item_id}")
        def read(item_id: str):
            return {"id": item_id}

        assert _get(app, "/items/list").json() == {"list": True}, mod
        assert _get(app, "/items/abc").json() == {"id": "abc"}, mod


def test_same_path_multi_methods():
    for mod in (_fa, _f2):
        app = mod.FastAPI()

        @app.get("/m")
        def g():
            return {"m": "get"}

        @app.post("/m")
        def p():
            return {"m": "post"}

        assert _get(app, "/m").json() == {"m": "get"}, mod
        assert _post(app, "/m").json() == {"m": "post"}, mod
        assert _client(app).put("/m").status_code == 405, mod


def test_redirect_slashes():
    for mod in (_fa, _f2):
        app = mod.FastAPI()

        @app.get("/trail")
        def t():
            return {}

        r = _client(app).get("/trail/")
        assert r.status_code == 200, mod
        r = _client(app).get("/trail/", follow_redirects=False)
        assert r.status_code in (301, 302, 307, 308), mod


# ------------------------------------------------------------- rate limiter ---

def test_rate_limiter_asgi():
    from fasterapi.guards import RateLimiter
    app = _f2.FastAPI()
    app.add_middleware(RateLimiter, per_second=3, burst=2)

    @app.get("/x")
    def x():
        return {}

    codes = [_client(app).get("/x").status_code for _ in range(5)]
    assert codes[0] == 200 and 429 in codes


def test_json_dumps_parity():
    """Rust writer output is byte-identical to stdlib compact dumps."""
    import math

    from fasterapi import _rust_bridge as b

    if not b.HAS_RUST:
        pytest.skip("rust ext not built")
    cases = [
        {"a": [1, 2.5, "x", None, True]},
        {"f": [0.0, -0.0, 1e16, 1e300, 2.5e-8, 0.1, 9999999999999998.0]},
        {"héllo": "→🎉", "ctrl\n": 1},
        {1: "i", True: "t", None: "n", 1.5: "f"},  # noqa: F601
    ]
    for obj in cases:
        assert b.fast_json_dumps(obj) == json.dumps(
            obj, separators=(",", ":"), ensure_ascii=False)

    with pytest.raises(ValueError):
        b.fast_json_dumps({"x": math.nan}, strict=True)
