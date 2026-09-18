# taskapi — example backend on fasterapi

A complete little task-manager backend proving the drop-in story end to end:
routers, OAuth2 password flow, background jobs, file upload/download
(`FileResponse` → sendfile on the Hyper host), an SSE activity feed, rate
limiting, CORS, lifespan seeding, and a green test suite.

## Run it

```bash
# from the fasterapi repo root:
pip install -e ".[standard]"
pip install -r examples/taskapi/requirements.txt

cd examples/taskapi
fasterapi run app.main:app --reload            # uvicorn (default)
fasterapi run app.main:app --reload --hyper    # Rust Hyper host
```

Then: docs at <http://127.0.0.1:8120/docs>, health at `/health`.

Demo login: username `demo`, password `demo1234`.

```bash
TOKEN=$(curl -s -X POST localhost:8120/auth/token \
  -d 'username=demo&password=demo1234' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -s -X POST localhost:8120/tasks -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"title":"Write docs","priority":1}'
curl -s "localhost:8120/tasks?status=open" -H "Authorization: Bearer $TOKEN"
curl -s localhost:8120/feed -H "Authorization: Bearer $TOKEN" | head -8
```

## Test it

```bash
cd examples/taskapi
python -m pytest tests/ -q
```

## Layout

```
taskapi/
├── app/
│   ├── main.py            # app factory, lifespan seed, middleware, routers
│   ├── core/
│   │   ├── config.py      # env-driven settings (stdlib only)
│   │   ├── security.py    # PBKDF2 passwords, opaque demo tokens
│   │   └── deps.py        # get_store, get_current_user, pagination
│   ├── db/store.py        # lock-guarded in-memory store (swap for SQL later)
│   ├── models/schemas.py  # Pydantic v2 request/response models
│   └── routers/
│       ├── auth.py        # password-grant login, /me
│       ├── tasks.py       # CRUD + filters + completion jobs
│       ├── files.py       # upload/download via FileResponse
│       └── stream.py      # SSE feed + health
└── tests/test_api.py      # end-to-end flows
```

## Notes & limits

- Auth is demo-grade (opaque server-side tokens, in-memory users). Swap
  `issue_token`/`token_owner` for JWT to go further — routers don't change.
- The store is in-memory; restart wipes it. The repository-shaped `Store`
  API is the seam for SQLite/Postgres.
- Uploads live under `TASKAPI_DATA` (default `./data`), capped at
  `TASKAPI_MAX_UPLOAD` bytes.
- `TASKAPI_RATE` sets the per-second rate limit (default 200).
