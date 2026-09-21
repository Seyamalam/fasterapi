# comparison audit project

Two API projects side by side — one on **FastAPI**, one on **fasterapi** —
with a growing amount of shared "real project" complexity, differentially
compared on outputs and speed, with a running audit report.

```
comparison/
├── stack/              # shared complexity (identical for both apps):
│   ├── settings.py     #   pydantic settings + PyYAML overrides
│   ├── security.py     #   PyJWT issue/decode + API key
│   ├── db.py           #   async SQLAlchemy 2.0 + aiosqlite, WAL, seed
│   ├── schemas.py      #   pydantic v2 wire models
│   └── stack.yaml
├── fastapi_app.py      # the app on FastAPI (baseline)
├── fasterapi_app.py    # the SAME app on fasterapi (imports + plugin section)
├── audit.py            # the audit runner (differential + perf + report)
├── STAGES.md           # stage ladder + how to add the next stage
├── AUDIT.md            # generated report (regenerated every run)
└── audit-results/
    └── history.jsonl   # append-only run trail
```

## The apps

Both expose the same 23-route surface: health/config, OpenAPI, static mount,
CORS, request-ID middleware, paginated DB CRUD with owner joins, JWT auth
with RBAC, form login, multipart upload, background audit-log writes,
aggregate stats, orjson compute endpoint, SSE, websocket echo, custom
exception handler, and the full 404/401/403/422 error wire shapes (including
malformed-JSON `json_invalid`).

`fasterapi_app.py` differs only in the framework-import block, the marked
*plugin section* (Rust `RateLimiter` edge guard) and `app.build()`.

## Running the audit

```bash
# from repo root (env: .venv with fasterapi editable + fastapi[standard] + sql[asyncio] + aiosqlite + pyjwt + pyyaml + orjson)
.venv/bin/python examples/comparison/audit.py                        # all stages, diff + perf
.venv/bin/python examples/comparison/audit.py --stages 2             # one stage
.venv/bin/python examples/comparison/audit.py --diff-only            # outputs only (fast)
.venv/bin/python examples/comparison/audit.py --requests 3000 --concurrency 100
```

The runner (1) resets + re-seeds both SQLite DBs so mutable-state probes are
comparable, (2) boots both apps under the **same uvicorn host**, (3) fires
the differential probe matrix at both (status + JSON body + headers must
match), (4) runs per-stage perf waves, (5) appends to
`audit-results/history.jsonl` and regenerates `AUDIT.md`.

Exit code 1 on any differential mismatch or boot failure — the differential
phase is the hard gate; perf is reported, not gated (loopback noise).

## What it already caught

Fixed in `fasterapi` core after the first audit runs:

- 422 error payloads kept pydantic's `url` and dropped `ctx` (FastAPI does
  the opposite) — `routing.py`
- Malformed JSON on an authenticated route returned 401 before the body was
  parsed; FastAPI 422s with `json_invalid` before deps run — `routing.py`
- GET routes auto-served `HEAD` (FastAPI's `APIRoute` does not) — `routing.py`
