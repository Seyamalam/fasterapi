"""Native fasterapi unit tests (no FastAPI import)."""

from __future__ import annotations

from pydantic import BaseModel

import fasterapi as F
from fasterapi import Depends, FastAPI, HTTPException, Query
from fasterapi.dag import compile_app_dag, compile_endpoint_dag


def _client(app: FastAPI) -> F.TestClient:
    return F.TestClient(app)


def test_build_compiles_dag_and_router():
    app = FastAPI()

    async def dep_a(q: str = Query("x")):
        return q

    @app.get("/a/{x}")
    async def a(x: int, q: str = Query("d"), dep: str = Depends(dep_a)):
        return {"x": x, "q": q, "dep": dep}

    dag = app.build()
    assert dag.node_count >= 2
    assert any("dep_a" in n for n in dag.order)
    assert app._rust_router is not None
    with _client(app) as c:
        r = c.get("/a/5?q=z")
        assert r.status_code == 200
        assert r.json() == {"x": 5, "q": "z", "dep": "z"}


def test_use_cache_false_calls_twice():
    calls: list[str] = []
    app = FastAPI()

    async def dep():
        calls.append("x")
        return len(calls)

    @app.get("/c")
    async def c(a: int = Depends(dep, use_cache=False), b: int = Depends(dep, use_cache=False)):
        return {"a": a, "b": b}

    with _client(app) as client:
        # Two separate Depends() markers -> two evaluations (no shared cache key benefit).
        r = client.get("/c")
        assert r.status_code == 200
        assert r.json() == {"a": 1, "b": 2}


def test_guards_rate_limit_and_api_key():
    from fasterapi.guards import APIKeyGuard, RateLimiter

    app = FastAPI()
    app.add_middleware(RateLimiter, per_second=1000)
    app.add_middleware(APIKeyGuard, api_key="secret")

    @app.get("/secret")
    async def secret():
        return {"ok": True}

    with _client(app) as c:
        assert c.get("/secret").status_code == 401
        assert c.get("/secret", headers={"x-api-key": "secret"}).status_code == 200


def test_rust_bridge_fallback_api():
    from fasterapi import _rust_bridge as bridge

    assert bridge.fast_json_loads('{"a":1}') == {"a": 1}
    assert bridge.fast_json_loads('[1,2]') == [1, 2]
    r = bridge.make_router()
    r.insert("/items/{item_id}", "read_item")
    if bridge.HAS_RUST:
        found = r.match("/items/42")
        assert found is not None and found[0] == "read_item"
    else:
        assert r.match("/items/42") == ("read_item", {"item_id": "42"})
        assert r.match("/nope") is None
    b = bridge.make_token_bucket(10)
    assert b.check() is True
    dag = bridge.make_dag()
    dag.add_edge("a", "b")
    assert dag.topological_sort().index("a") < dag.topological_sort().index("b")
    assert isinstance(bridge.process_rss_bytes(), int)


def test_json_roundtrip_models():
    class M(BaseModel):
        x: int

    app = FastAPI()

    @app.post("/m", response_model=M)
    async def m(payload: M):
        return payload

    with _client(app) as c:
        assert c.post("/m", json={"x": 3}).json() == {"x": 3}
        assert c.post("/m", json={"x": "bad"}).status_code == 422


def test_http_exception_headers():
    app = FastAPI()

    @app.get("/h")
    async def h():
        raise HTTPException(status_code=403, detail="no", headers={"x-reason": "test"})

    with _client(app) as c:
        r = c.get("/h")
        assert r.status_code == 403
        assert r.headers.get("x-reason") == "test"
