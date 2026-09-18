# Changelog

All notable changes to fasterapi. Format follows Keep a Changelog; versions
are `0.x` until the Hyper host promotes to default.

## [Unreleased]

### Added
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
