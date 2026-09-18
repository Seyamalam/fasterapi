# Migrating from FastAPI to fasterapi

fasterapi is a 1:1 drop-in: change the import, keep everything else.

```python
# from fastapi import FastAPI, Depends, HTTPException, Query, Path, Body, APIRouter
from fasterapi import FastAPI, Depends, HTTPException, Query, Path, Body, APIRouter
```

The differential suite (`tests/test_parity*.py`, 50+ cases) locks behavior:
status codes, JSON bodies, 422 shapes (`loc`/`type`/`ctx`), dependency order
and `yield` teardown order, auth challenges, docs bytes, SSE frames and the
OpenAPI document itself.

## Serving

```bash
fasterapi run myapp:app --port 8000            # uvicorn (default, max compat)
fasterapi run myapp:app --port 8000 --hyper    # Rust Hyper host (fast fan-out)
fasterapi run myapp:app --reload               # dev watcher
```

The Hyper host is byte-identical for documented behavior. Intentional,
documented differences from uvicorn:

| Area | uvicorn | Hyper host |
|---|---|---|
| `Server` header | `uvicorn` | `fasterapi-hyper` |
| Request bodies | ~100 MB default limit | 16 MiB cap → `413` |
| HTTP versions | 1.1 (+h11/httptools) | 1.1 only |
| WebSockets | yes | not yet (falls back — see below) |
| Shutdown | SIGTERM/SIGINT | SIGTERM, and SIGINT with a proper process group |
| Response streaming | streamed | concatenated (same bytes) |

WebSocket routes work through `TestClient` and uvicorn; the Hyper host
currently answers them via the same ASGI path only where the handshake
fits HTTP semantics — use uvicorn serving if you depend on websockets in
production today.

## Import map

Everything in the table below imports from the same place. Not ported on
purpose: `fastapi.cli` (use `fasterapi run`), `fastapi.utils` Pydantic-v1
internals, `fastapi.dependencies.*` internals (architecture differs;
`Depends`/`Security` themselves are fully supported), `openapi.webhooks`.

| fastapi | fasterapi | Notes |
|---|---|---|
| `FastAPI`, `APIRouter`, `Depends`, `Security` | same | full DI: sub-deps, cache, `yield` |
| `Query/Path/Body/Header/Cookie/Form/File` | same | constraints enforced in Rust when compiled |
| `UploadFile`, `BackgroundTasks`, `status` | same | |
| `Request`, `Response`, `WebSocket` | same (Starlette types) | |
| `HTTPException`, `RequestValidationError` | same | exact 422 wire shapes |
| `TestClient` | same | HTTPX semantics |
| `security.*` | same | all schemes, exact 401s |
| `sse.*`, `responses.*` | same | byte-identical frames/pages |
| `openapi.docs.*`, `openapi.utils.get_openapi` | same | full-spec equality tested |
| `middleware.cors/gzip/...`, `staticfiles`, `templating` | same | thin re-exports |
| `encoders.jsonable_encoder` | same | |
| `concurrency.run_in_threadpool` | same | |
| `utils.generate_unique_id` | same | |

## Response models

- Unset `response_model` falls back to the return annotation (like FastAPI);
  pass `response_model=None` explicitly to disable filtering.
- `response_model_include/exclude/exclude_unset/exclude_none/...` supported.
- A bare `list`/`tuple`/`set` body param without `Body()` is unsatisfiable
  (422) on both frameworks — wrap it in `Body()` or a model.

## Validation errors

422 payloads match FastAPI key-for-key (`type`/`loc`/`msg`/`input`, plus
`ctx` for pattern errors). Malformed JSON yields `json_invalid` with the
same offset FastAPI reports.

## Free-threading

Run `bash scripts/free-threaded-check.sh`. Thread-safe by construction
(Rust `allow_threads`, per-request Python state); needs a
`--disable-gil` interpreter for the GIL-off proof.

## Getting help

Open an issue with a minimal differential case (both apps, ~20 lines) —
parity bugs are fixed with a regression test first, performance issues
with a bench number attached.
