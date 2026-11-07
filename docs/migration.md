# Migrating from FastAPI to fasterapi

fasterapi aims for FastAPI API and behavior compatibility: change the import,
then validate your app against the compatibility notes below. It is not a
complete implementation of every FastAPI version or integration.

```python
# from fastapi import FastAPI, Depends, HTTPException, Query, Path, Body, APIRouter
from fasterapi import FastAPI, Depends, HTTPException, Query, Path, Body, APIRouter
```

The differential suite (`tests/test_parity*.py`) covers status codes, JSON
bodies, 422 shapes (`loc`/`type`/`ctx`), dependency order and `yield` teardown,
auth challenges, docs bytes, SSE frames, and OpenAPI output. Its coverage is
broad but finite; run application-specific compatibility tests before
migrating. FastAPI's optional native telemetry module is not implemented.

## Serving

```bash
fasterapi run myapp:app --port 8000            # uvicorn (default, max compat)
fasterapi run myapp:app --port 8000 --hyper    # experimental Rust Hyper host
fasterapi run myapp:app --reload               # dev watcher
```

The Hyper host is experimental and not guaranteed to be byte-identical for
all ASGI apps. Known differences from uvicorn include:

| Area | uvicorn | Hyper host |
|---|---|---|
| `Server` header | `uvicorn` | `fasterapi-hyper` |
| Request bodies | ~100 MB default limit | 16 MiB cap → `413` |
| HTTP versions | 1.1 (+h11/httptools) | 1.1 only |
| WebSockets | yes | basic RFC 6455 host support; test your subprotocol/extensions and deployment needs |
| Shutdown | SIGTERM/SIGINT | SIGTERM, and SIGINT with a proper process group |
| Response streaming | streamed | concatenated (same bytes) |

WebSocket routes are implemented in the Hyper host and covered by basic
echo/auth tests. The host's WebSocket support is still a spike rather than a
full production protocol stack; use uvicorn if your app relies on advanced
extensions, subprotocol negotiation, or broader WebSocket compatibility.

## Import map

Everything in the table below imports from the same place. Not ported on
purpose: `fastapi.cli` (use `fasterapi run`), `fastapi.utils` Pydantic-v1
internals, `fastapi.dependencies.*` internals (architecture differs;
`Depends`/`Security` themselves are fully supported), `openapi.webhooks`.

| fastapi | fasterapi | Notes |
|---|---|---|
| `FastAPI`, `APIRouter`, `Depends`, `Security` | same | full DI: sub-deps, cache, `yield` |
| `Query/Path/Body/Header/Cookie/Form/File` | compatible subset | selected scalar constraints use Rust when compiled; other cases use Python/Pydantic |
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

`bash scripts/free-threaded-check.sh` runs thread-soak tests and probes whether
the selected interpreter has the GIL disabled. The project does not yet claim
full free-threaded support: use a compatible PyO3 build and validate on the
target `--disable-gil` Python version before deployment.

## Getting help

Open an issue with a minimal differential case (both apps, ~20 lines) —
parity bugs are fixed with a regression test first, performance issues
with a bench number attached.
