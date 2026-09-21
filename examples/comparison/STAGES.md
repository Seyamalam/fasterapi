# Stage ladder — complexity added over time

The audit climbs a ladder: each stage **adds** real-world complexity to *both*
apps (kept in lockstep) and re-runs differential + perf. Nothing is ever
removed, so stage N ⊃ stage N−1.

| Stage | Name | Complexity added | Differential probes | Perf endpoints |
|---|---|---|---|---|
| 1 | core+validation+errors | routing, query/path validation, 404/422 wire shapes (incl. malformed JSON `json_invalid`), custom exception handler, OpenAPI surface, StaticFiles mount, pure-ASGI middleware | 10 | GET /ping |
| 2 | db+auth+jobs | async SQLAlchemy 2.0 (aiosqlite) CRUD + pagination + aggregate stats, JWT bearer auth (PyJWT), RBAC dependency chain, OAuth2-style form login, multipart upload, BackgroundTasks audit-log writes, YAML-backed settings | 17 | GET /items, POST /items, GET /items/expensive (orjson compute) |
| 3 | streaming+realtime+docs | SSE stream, websocket echo, CORS preflight, HEAD method parity, docs surface | 5 | GET /me, GET /sse/ticks |

Run results land in `AUDIT.md` (regenerated every run) and
`audit-results/history.jsonl` (append-only trail, one record per run).

## Adding stage 4+ (the point of this project)

1. **Add to both twins in lockstep** — `fastapi_app.py` and
   `fasterapi_app.py` must stay identical except the framework-import block
   and the marked *plugin section*. If they diverge, the audit is lying.
   Ideas that are queued: APIRouter sub-packages, `response_model` response
   validation errors, deeper ORM relationships (eager loading), a caching
   layer, scheduled jobs, file streaming, rate-limited routes, multi-worker
   load.
2. **Add probes** to `build_probes()` in `audit.py` with
   `stage=4, expect=…`, using `drop=[...]` for nondeterministic JSON fields
   and `check_header_names=[...]` when only header presence matters.
3. **Add perf endpoints** to `PERF_ENDPOINTS[4]` (write-heavy endpoints
   should set `requests=`/`concurrency=` below the default wave — SQLite has
   a single-writer ceiling that hits both frameworks equally).
4. **Register the stage** in the `STAGES` dict.
5. Rerun: `../../.venv/bin/python audit.py --stages 4` (or all stages).

## Known-by-design asymmetries

- `fasterapi_app.py` wires the Rust `RateLimiter` edge guard (fasterapi-only
  plugin, ceiling set high enough to never trip); `fastapi_app.py` has an
  empty plugin section. Both apps expose `/meta/framework` so the audit can
  assert which framework + plugins are actually live.
- `fasterapi_app.py` calls `app.build()` (AOT router + DAG compile) — a
  no-op on the fastapi side by design.
