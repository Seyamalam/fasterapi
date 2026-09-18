# fasterapi ⚡

**1:1 FastAPI drop-in replacement, Rust-powered.**

```python
# from fastapi import FastAPI, Depends, HTTPException, Query, Path, Body, APIRouter  # before
from fasterapi import FastAPI, Depends, HTTPException, Query, Path, Body, APIRouter  # after
```

`fasterapi` keeps the exact FastAPI interface — decorators, `Path/Query/Header/Cookie/Body/Form`,
Pydantic v2 validation, `Depends()` DI (sub-deps, `use_cache`, `yield` teardown),
`HTTPException`/`RequestValidationError`, `@app.exception_handler`, lifespan, middleware,
CORS, `TestClient`, OpenAPI — while the hot path moves to a native
**Rust (Tokio + Hyper + PyO3)** core: radix routing (`matchit`), SIMD JSON
(`simd-json`), token-bucket edge guards (`governor`), and an AOT-compiled
`Depends()` DAG. Targets **Python 3.13+ free-threaded (`--disable-gil`)**;
verified on 3.10–3.14.

> Can it be done? **Yes — incrementally.** v0.1 lands total parity on the
> battle-tested Starlette host (same engine FastAPI uses) with the Rust
> data-plane already compiled in (`RustRouter`, SIMD JSON, `TokenBucket`,
> `DependencyDAG`). The inverted-runtime Hyper host (Rust owns the socket,
> Python as embedded guest) is staged in `src/lib.rs` behind
> `FASTERAPI_HYPER=1` and promotes once loopback parity is green. No magic,
> all gates run locally.

---

## Contents

- [Install](#install)
- [Quickstart](#quickstart)
- [Parity (Phase 1)](#parity-phase-1)
- [Rust core (Phase 2)](#rust-core-phase-2)
- [Modern Python (Phase 3)](#modern-python-phase-3)
- [Testing & benchmarking (Phase 4)](#testing--benchmarking-phase-4)
- [Git hooks](#git-hooks)
- [Directory structure](#directory-structure)
- [Free-threading](#free-threading)
- [Benchmarks](#benchmarks)
- [Roadmap](#roadmap)
- [License](#license)

---

## Install

Prereqs: Python ≥3.10, Rust stable (for the extension), `uvicorn` for serving.

```bash
git clone https://github.com/Seyamalam/fasterapi && cd fasterapi
bash scripts/quickstart.sh        # venv + pip install -e ".[test,bench]" + hooks + tests
# or manually:
pip install -e ".[test,bench]"
pip install maturin && maturin develop --release   # Rust fast-paths (optional but recommended)
bash scripts/install_hooks.sh     # pre-push parity+perf gate, pre-commit fast checks
```

Pure-Python works without compiling — `_rust_bridge` falls back automatically
(`fasterapi.HAS_RUST` tells you which path is live).

## Quickstart

```python
from fasterapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel

app = FastAPI(title="demo")

class Item(BaseModel):
    name: str
    price: float

async def common(q: str | None = Query(default=None)):
    return {"q": q}

@app.get("/items/{item_id}")
async def read_item(item_id: int, common: dict = Depends(common)):
    if item_id == 0:
        raise HTTPException(status_code=404, detail="Not found")
    return {"item_id": item_id, **common}

@app.post("/items")
async def create_item(item: Item) -> Item:
    return item

if __name__ == "__main__":
    app.build()   # AOT: Depends DAG + Rust radix router + JSON warmup
    app.serve(port=8000)
```

`examples/quickstart.py` is runnable as-is. Serve with `uvicorn examples.quickstart:app`.

## Parity (Phase 1)

| Feature | FastAPI | fasterapi v0.1 |
|---|---|---|
| `@app.get/post/put/delete/patch/options/head/trace`, `@app.websocket` | ✅ | ✅ |
| `APIRouter(prefix, tags, deps)`, `include_router` | ✅ | ✅ |
| `Path/Query/Header/Cookie/Body/Form/File`, `Depends(use_cache, yield)` | ✅ | ✅ |
| Pydantic v2 validation + `response_model` + 422 `{"detail": [...]}` | ✅ | ✅ |
| `HTTPException`, `RequestValidationError`, `@app.exception_handler` | ✅ | ✅ |
| Lifespan `@asynccontextmanager`, `add_middleware`, CORS, GZip | ✅ | ✅ |
| `TestClient` (HTTPX semantics), OpenAPI `/openapi.json` | ✅ | ✅ |
| PEP 695 generics `def h[T: BaseModel](p: T) -> Page[T]` | ✅ (3.12+) | ✅ (native syntax, validated at runtime) |

Run the differential suite (same endpoints, both frameworks):

```bash
pytest tests/test_parity.py -q
pytest tests/ -q
```

## Rust core (Phase 2)

`src/lib.rs` → `_fasterapi_rs` (PyO3):

- **`RustRouter`** — `matchit` radix tree; `app.build()` compiles every route
  once; lookups run under a read-lock with the GIL released.
- **`fast_json_loads/dumps`** — `simd-json` + `serde_json`, `py.allow_threads`
  so Tokio work-stealers never block on the GIL.
- **`TokenBucket`** — `governor` token bucket; `RateLimiter` middleware
  returns `429` without touching Python.
- **`DependencyDAG`** — `topological_sort()` + `concurrent_levels()` (Kahn)
  so Tokio fans out each level with `FuturesUnordered` before the endpoint.
- **Host helpers** — `hyper_health()`, `tokio_worker_count()`,
  `process_rss_bytes()`, `serve_static_config()` (sendfile plan).

```python
app.build()  # compile DAG + router once at startup
from fasterapi import _rust_bridge as b
b.fast_json_loads('{"a":1}')   # SIMD when compiled
```

Full inverted runtime (Hyper owns the socket, no ASGI dicts, C-struct bridge)
is staged — `FASTERAPI_HYPER=1 python -m uvicorn …` prints the host plan and
keeps uvicorn until loopback numbers promote it. See `src/lib.rs` header.

## Modern Python (Phase 3)

- **PEP 695 generics** — plain `async def h[T: BaseModel](payload: T)` works;
  validation goes through `TypeAdapter(T)` like any model.
- **Edge guards** — `app.add_middleware(RateLimiter, per_second=500)` and
  `app.add_middleware(APIKeyGuard, api_key="…")` execute in Rust when compiled
  (`401`/`429` with ~0 Python overhead).
- **Zero-copy files** — `from fasterapi import FileResponse` sends
  `x-fasterapi-serve: tokio-sendfile` + static plan; Tokio `sendfile`s media
  bypassing Python.

## Testing & benchmarking (Phase 4)

```bash
# parity (must be 100% green)
pytest tests/test_parity.py tests/test_fasterapi_native.py -q

# micro fast-paths only (no servers)
python benchmarks/benchmark_differential.py --micro-only

# full differential load (spins up :8011 fastapi+uvicorn vs :8012 fasterapi)
python benchmarks/benchmark_differential.py --quick
python benchmarks/benchmark_differential.py --requests 20000 --concurrency 100 --min-speedup 1.0
python benchmarks/benchmark_differential.py --quick --min-speedup 3.0  # strict gate
```

Metrics: RPS, p50/p90/p99/mean/max, CPU %, RSS — JSON to
`benchmark-results/differential-latest.json`. Uses `oha`/`wrk`/`bombardier`
when installed, else a portable asyncio+httpx loader. Exit `2` on gate failure.

## Git hooks

No GitHub Actions — everything runs locally before it can leave your machine.

```bash
bash scripts/install_hooks.sh   # sets core.hooksPath=.githooks
```

- **`pre-push`**: `maturin develop --release` → full parity suite (reject on
  any failure) → benchmark `--quick` (reject on regression below
  `FASTERAPI_MIN_SPEEDUP`, default `1.0`; set `3.0` for the strict 3x JSON/
  routing gate once Rust is compiled; `FASTERAPI_SKIP_PERF=1` to override).
- **`pre-commit`**: `cargo fmt --check` (advisory) + fast native unit tests.

## Directory structure

```
fasterapi/
├── Cargo.toml                  # Rust ext (_fasterapi_rs): pyo3, tokio, hyper, matchit, simd-json, governor
├── pyproject.toml              # maturin (pyo3 bindings, python-source=python)
├── rust-toolchain.toml
├── src/lib.rs                  # PyO3 core: router, SIMD JSON, TokenBucket, DAG, host helpers
├── python/fasterapi/
│   ├── __init__.py             # drop-in surface (FastAPI, APIRouter, Depends, …)
│   ├── applications.py         # FastAPI(Starlette): decorators, lifespan, openapi, build(), serve()
│   ├── routing.py              # APIRouter + validation + DI solver + ws
│   ├── params.py               # Path/Query/Header/Cookie/Body/Form/File/Depends
│   ├── exceptions.py           # HTTPException, RequestValidationError, …
│   ├── responses.py            # + FileResponse(sendfile hint)
│   ├── testclient.py           # fastapi.testclient-compatible TestClient
│   ├── dag.py                  # AOT Depends compiler (Rust DAG when present)
│   ├── guards.py               # RateLimiter, APIKeyGuard
│   └── _rust_bridge.py         # HAS_RUST switch + pure-Python fallbacks
├── tests/
│   ├── test_parity.py          # differential suite (FastAPI vs fasterapi)
│   └── test_fasterapi_native.py
├── benchmarks/
│   └── benchmark_differential.py
├── .githooks/{pre-push,pre-commit}
├── scripts/{install_hooks.sh,quickstart.sh}
└── examples/quickstart.py
```

## Free-threading

All Rust entry points use `py.allow_threads` + `parking_lot` locks; the Python
DI solver keeps per-request state in locals (no shared mutables), so
`PYTHON_GIL=0` / `python -X gil=0` (3.13t/3.14t) runs handlers concurrently in
one address space. The default build here targets stable ABI; for a
free-threaded interpreter rebuild with `Py_GIL_DISABLED=1` and
`maturin develop --release`.

## Benchmarks

Measured on this dev box (`maturin develop --release`, CPython 3.14,
`benchmark_differential.py --micro-only`; your numbers will differ — hooks
compare on *your* machine so hardware cancels out):

| Workload | stdlib/Python | fasterapi (Rust) | Notes |
|---|---|---|---|
| `GET /ping` (httpx, c=20) | ~baseline | ~1.0–1.2x | parity host today; Hyper host promotes this |
| JSON `loads` 50-obj payload x2000 | 1.0x | ~0.6–1.0x today | SIMD parse is fast; Py-object materialization dominates — zero-copy views to Pydantic are the roadmap item |
| JSON `dumps` x2000 | 1.0x | ~0.9x today | same note as loads |
| Router match x2000×203 paths | 1.0x | **~3.4x** (`matchit`) | pre-push 3x gate keys off this |
| Scalar validators (`int`/`bool`/`str` + constraints) | 1.0x (Pydantic) | Rust, zero-GIL | every query/path/header scalar avoids Python coercion |

Design rule going forward (your brief): **as much as possible in Rust,
type-safe, lighter** — routing, query parsing, scalar validation, JSON,
rate-limiting and static files live in `src/lib.rs` (strict types,
`allow_threads`, no shared mutables); Python keeps only FastAPI-compatible
orchestration (DI graph shape, Pydantic model validation, handler dispatch)
until the Hyper host promotes. `HAS_RUST` + `--micro-only` prove each step.

Set `FASTERAPI_MIN_SPEEDUP=3.0` once the extension is compiled to enforce the
3x routing fast-path gate from the spec.

## Roadmap

- [x] Phase 1 — drop-in parity + TestClient + OpenAPI
- [x] Phase 2 — Rust DAG/router/SIMD JSON/guards primitives + `app.build()`
- [x] Phase 3 — PEP 695, edge guards, sendfile `FileResponse`
- [x] Phase 4 — differential tests + benchmark engine + pre-push gate
- [ ] Promote Hyper inverted host (`FASTERAPI_HYPER=1` default) + `sendfile` static mount
- [ ] Tokio fan-out of `concurrent_levels()` for async deps (Python `asyncio.TaskGroup` today)
- [ ] `3.13t` wheel CI (local maturin only — no Actions per repo policy)

## License

MIT — see [LICENSE](LICENSE).
