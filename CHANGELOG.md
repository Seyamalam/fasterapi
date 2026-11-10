# Changelog

All notable changes to fasterapi. Format follows Keep a Changelog; versions
are `0.x` until the Hyper host promotes to default.

## [Unreleased]

### Added
- **Radix-tree dispatch is live**: `FastRouter` compiles all plain routes
  into `matchit` and answers unambiguous FULL matches in Rust; ambiguous
  orderings, Mount/Host entries, convertor patterns, method mismatches and
  redirects defer to Starlette's scan — byte-identical semantics.
- **DI fan-out**: yield-free `Depends` subtrees resolve concurrently
  (signature-ordered results, per-dep locks for `use_cache`, deterministic
  first-exception-wins). Yield deps stay sequential to preserve teardown
  order. Dependency validation errors now collect across all siblings like
  FastAPI (`{"detail": [...]}` lists every failure).
- **Fast JSON writer** (`itoa`/`ryu`, byte-wise escaping, Python-`repr`
  float adapter): `fast_json_dumps` ~x2.5 vs compact stdlib, byte-identical
  output (`ensure_ascii=False` contract, coerced-key quoting, `strict=`
  mirrors `allow_nan=False`). Wired into the default response path via
  `_FastJSONResponse`.
- **Socket-level rate limiting on Hyper**: `RateLimiter` is now a pure-ASGI
  middleware (no `Request` allocation); its bucket is registered on the app
  and checked in Rust before any Python runs (`429` pre-parse). Under
  uvicorn the same pure-ASGI path applies.
- **Route kwargs**: `include_in_schema`, `openapi_extra`, `callbacks`,
  `responses` (route-level merge over router default) — all wired into
  `get_openapi`.
- **`app.mount()`** verified for ASGI sub-apps and `StaticFiles`.
- **`on_event("startup"|"shutdown")`** on `FastAPI`/`APIRouter` with merge
  on `include_router` (deprecation-warned, matches FastAPI semantics).
- **`fasterapi.openapi.webhooks`** module (import-surface parity).
- **`FASTERAPI_MAX_BODY`** env var tunes the Hyper body cap (16 MiB default).
- **Free-threaded support**: the extension declares `gil_used = false`
  (PyO3 nogil-safe) — the full 87-test suite passes under `PYTHON_GIL=0`
  on CPython 3.14t.
- **True streaming on Hyper**: responses that keep producing past a 30ms
  settle graduate to a real-time session — chunks flow through an mpsc into
  Hyper's body writer (chunked encoding), the client sees incremental bytes,
  and client disconnect cancels the app task. `receive()` now correctly
  pends for disconnect instead of returning it eagerly.
- **WebSocket subprotocol negotiation** on Hyper: `sec-websocket-protocol`
  offers reach `scope["subprotocols"]`; the app's `accept(subprotocol=...)`
  is echoed in the 101.
- Hyper host gate run: ~x1.03 uvicorn RPS on a micro-JSON workload
  (5453 vs 5316 rps loopback) — first parity-or-better measurement.
- GitHub Actions CI (test matrix 3.10–3.14 + lint + wheel builds on tags)
  and a ruff advisory gate in `pre-commit`.
- `tests/test_parity_extended.py`: 23 differential tests — multi-dep error
  collection, required params, DI concurrency/cache/ordering, mounts,
  on_event, route kwargs, enum/union/nested-model validation, route
  ordering edge cases, rate limiter, JSON writer parity.

### Fixed
- **Required `Query`/`Header`/`Cookie` params** silently resolved to
  `Ellipsis` (dead `param.default is empty` check) — now 422 correctly.
- **Multi-dep 422s** collected only the first failing dep's errors —
  now returns every failure in signature order like FastAPI.

### Added (previous)
- Rust Hyper + Tokio ASGI host spike (`serve_asgi`, `FASTERAPI_HYPER=1`,
  `python -m fasterapi run app:app --hyper`): sockets + HTTP parsing in Rust,
  byte-identical app behavior, lifespan, graceful SIGTERM/SIGINT shutdown,
  413 body cap, `FASTERAPI_POOL` worker tuning.
- Zero-copy file streaming on the Hyper host (`FileResponse` sendfile lane,
  `Range` falls back to Starlette semantics).
- Direct dispatch: with no user middleware, Hyper skips Starlette's
  per-request stack (ServerError/Exception wrappers, router loop).
- `get_openapi()` with FastAPI-identical output (operationIds, typed params,
  `$ref` bodies, nested components, 422 contract, `servers`, securitySchemes,
  per-op `security`), byte-identical Swagger/ReDoc/oauth2-redirect pages.
- Auth surface: OAuth2 (password, auth-code), OpenID Connect, HTTP
  Basic/Bearer/Digest, API-key header/query/cookie, `Security` + scopes,
  password-grant forms (permissive + strict).
- `BackgroundTasks` injection (shared per-request instance, FastAPI-exact
  teardown-after-background ordering via send-inside-stack lifecycle).
- SSE streaming (`EventSourceResponse`, `ServerSentEvent`, byte-identical frames).
- `UploadFile`, `status`, `BackgroundTasks`, `Security`, `jsonable_encoder`,
  `ORJSONResponse`/`UJSONResponse` shims, `middleware.*`, `staticfiles`,
  `templating`, `logger`, `types`, `sse`, `utils` (`generate_unique_id`),
  `param_functions`, `concurrency`, `dependencies`, `exception_handlers` modules.
- Response-model inference from return annotations (explicit
  `response_model=None` disables), filter flags (`include`/`exclude`/
  `exclude_unset`/`exclude_none`/…), `deprecated` + `operationId` support.
- Bare `dict`/`list[Item]` JSON bodies; `validate_json`/`dump_json`
  single-pass paths; lazy body parsing; Rust scalar validators.
- `fasterapi` console script (`fasterapi run [--reload] [--hyper]`).
- Export-parity guard test, thread-soak tests, Hyper differential tests
  (file bytes, 413, middleware fallback, graceful lifespan).
- Auto-generated README parity/bench tables (`scripts/update_readme_tables.py`,
  refreshed every commit); local bench history; free-threaded check script.

### Fixed
- Form field constraints dropped before validation.
- `self` solved as a query param in class dependencies.
- Instance (non-function) async dependencies never awaited.
- `id()`-keyed caches colliding after GC (weak keys).
- Lifespan shutdown unreachable (loop closed by `asyncio.run`); persistent
  lifespan loop.
- Python SIGINT disposition racing tokio shutdown (neutralized during serve).
- `x-fasterapi-*` plan headers leaking to clients; `server` re-stamped.
- `Vec<u8>` crossing the boundary as `list[int]` instead of `bytes`.

### Performance
- Model validation x2.2 (`validate_json`), model dump x2.9 (`dump_json`),
  router match x3.3 (`matchit`), JSON dumps x1.1 (direct writer).
- Hyper GET fan-out up to x1.37 with far better p90/p99; validation-heavy
  POST trails on GIL builds (documented GIL physics).

### Known limitations
- Hyper host: HTTP/1.1 only, buffered bodies (16 MiB cap), concatenated
  streaming, no mid-response disconnect polling, no WebSockets yet.
- `fastapi.cli`, `fastapi.utils` v1-compat internals, `dependencies.*`
  internals, `openapi.webhooks` are not ported (documented in migration guide).
- Single-run loopback RPS swings ±15%; gates use a strict micro 3x plus a
  wide macro band.
