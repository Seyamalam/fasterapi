"""Thread-safety soak: concurrent requests + shared native handles.

Runs on any build (GIL or free-threaded) and proves functional correctness
under threads: exact totals, no lost updates, no races in the bridge.
On `--disable-gil` interpreters the same tests exercise true parallelism
(Rust paths are `allow_threads` + lock-clean; Python state is per-request).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import fasterapi as F
from fasterapi import Depends, FastAPI


def test_concurrent_requests_exact_totals():
    import threading as _t

    lock = _t.Lock()
    total = {"n": 0}
    app = FastAPI()

    async def counter() -> int:
        with lock:
            total["n"] += 1
            return total["n"]

    @app.get("/n")
    async def n(i: int = Depends(counter)):
        return {"i": i}

    with F.TestClient(app) as client:
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: client.get("/n").json()["i"], range(200)))
    assert sorted(results) == list(range(1, 201))
    assert total["n"] == 200


def test_rust_bridge_thread_shared():
    from fasterapi import _rust_bridge as bridge

    router = bridge.make_router()
    router.insert("/items/{item_id}", "read_item")
    bucket = bridge.make_token_bucket(10_000, 10_000)
    dag = bridge.make_dag()
    dag.add_edge("a", "b")
    errors: list[str] = []

    def worker(n: int) -> None:
        try:
            for i in range(200):
                assert bridge.fast_json_loads('{"a":1}') == {"a": 1}
                assert bridge.validate_int_rs(str(i % 100)) == i % 100
                assert bucket.check() is True
                found = router.match("/items/7") if bridge.HAS_RUST else router.match("/items/7")
                assert found is not None and found[0] == "read_item"
                assert dag.topological_sort() == ["a", "b"]
        except Exception as exc:  # pragma: no cover
            errors.append(str(exc))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_free_threaded_readiness_notes():
    """Documents the free-threading contract (asserts the mechanisms exist)."""
    import inspect

    import fasterapi.routing as routing

    assert hasattr(routing, "_SIG_CACHE") and hasattr(routing, "_HINTS_CACHE")
    # Per-request mutable state lives in locals/handler scope, never globals.
    src = inspect.getsource(routing._make_http_handler)
    assert "global " not in src
    assert F.HAS_RUST is True
