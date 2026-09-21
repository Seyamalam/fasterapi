"""Side-by-side audit runner: FastAPI vs fasterapi.

Boots both comparison apps (uvicorn, identical host), then per stage:

1. Differential — the same request matrix at both; status + JSON body +
   key headers must match (framework-agnostic probes, normalized only for
   genuinely nondeterministic fields like timestamps/tokens).
2. Perf — identical RPS/latency load per stage endpoint; reports speedup.
3. Report — appends a JSONL record to ``audit-results/history.jsonl`` and
   regenerates ``AUDIT.md`` (running verdicts + run history).

Usage:
    .venv/bin/python examples/comparison/audit.py                       # all stages
    .venv/bin/python examples/comparison/audit.py --stages 2            # one stage
    .venv/bin/python examples/comparison/audit.py --diff-only           # outputs only
    .venv/bin/python examples/comparison/audit.py --requests 3000 --concurrency 100

Exit codes: 0 ok, 1 differential mismatch / boot failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "audit-results"
HISTORY_JSONL = RESULTS_DIR / "history.jsonl"

PORT_A = int(os.environ.get("AUDIT_FASTAPI_PORT", "8021"))      # fastapi
PORT_B = int(os.environ.get("AUDIT_FASTERAPI_PORT", "8022"))    # fasterapi

STAGES: dict[int, dict[str, str]] = {
    1: {"name": "core+validation+errors", "includes": "routing, query/path validation, 404/422 shapes, custom exception handler, openapi, static, middleware"},
    2: {"name": "db+auth+jobs", "includes": "async SQLAlchemy CRUD + pagination, JWT bearer, RBAC, forms, uploads, background tasks, aggregate stats"},
    3: {"name": "streaming+realtime+docs", "includes": "SSE, websocket, docs surface, CORS preflight, compute-heavy endpoint"},
}


# ─────────────────────────────────────────────────────────────── probes ──
# Each probe: stage, name, method, path, kwargs (httpx), expect status,
# optional drop (JSON fields ignored in comparison), optional header check.

def build_probes(token_cache: dict[str, str]) -> list[dict[str, Any]]:
    alice = token_cache.get("alice", "")
    bob = token_cache.get("bob", "")
    return [
        # ── stage 1: core + validation + errors ──────────────────────────
        dict(stage=1, name="GET /ping", method="GET", path="/ping", expect=200),
        dict(stage=1, name="GET /config", method="GET", path="/config", expect=200),
        dict(stage=1, name="GET /whoami (middleware)", method="GET", path="/whoami", expect=200,
             check_header_names=["x-request-id"]),
        dict(stage=1, name="404 detail", method="GET", path="/items/999", expect=404),
        dict(stage=1, name="422 path (str for int)", method="GET", path="/items/notanint", expect=422),
        dict(stage=1, name="422 query (page=0)", method="GET", path="/items?page=0", expect=422),
        dict(stage=1, name="422 malformed JSON", method="POST", path="/items", expect=422,
             kwargs={"content": b"{not json", "headers": {"content-type": "application/json"}}),
        dict(stage=1, name="custom exception handler (402)", method="GET", path="/boom", expect=402),
        dict(stage=1, name="static file", method="GET", path="/static/hello.txt", expect=200,
             raw_body=True),
        dict(stage=1, name="openapi paths", method="GET", path="/openapi.json", expect=200,
             openapi_paths=True),
        # ── stage 2: db + auth + jobs ────────────────────────────────────
        dict(stage=2, name="GET /items page=2", method="GET", path="/items?page=2&per_page=5", expect=200),
        dict(stage=2, name="GET /items?tag=b", method="GET", path="/items?tag=b", expect=200),
        dict(stage=2, name="GET /items/7", method="GET", path="/items/7", expect=200),
        dict(stage=2, name="POST /items (auth+db+bg)", method="POST", path="/items", expect=201,
             kwargs={"json": {"name": "audit-item", "price": 9.5, "tags": ["x"]}},
             auth="alice"),
        dict(stage=2, name="POST /items unauthenticated", method="POST", path="/items", expect=401,
             kwargs={"json": {"name": "nope", "price": 1.0, "tags": []}}),
        dict(stage=2, name="POST /items price<=0 (422)", method="POST", path="/items", expect=422,
             kwargs={"json": {"name": "bad", "price": 0, "tags": []}}, auth="alice"),
        dict(stage=2, name="POST /auth/token ok", method="POST", path="/auth/token", expect=200,
             kwargs={"data": {"username": "alice", "password": "wonderland"}},
             drop=["access_token"]),
        dict(stage=2, name="POST /auth/token bad pw", method="POST", path="/auth/token", expect=401,
             kwargs={"data": {"username": "alice", "password": "wrong"}}),
        dict(stage=2, name="GET /me (jwt)", method="GET", path="/me", expect=200, auth="alice"),
        dict(stage=2, name="GET /admin/ping as admin", method="GET", path="/admin/ping", expect=200,
             auth="alice"),
        dict(stage=2, name="GET /admin/ping as user (403)", method="GET", path="/admin/ping", expect=403,
             auth="bob"),
        dict(stage=2, name="GET /api-key-ping ok", method="GET", path="/api-key-ping?x_api_key=audit-api-key",
             expect=200),
        dict(stage=2, name="GET /api-key-ping bad", method="GET", path="/api-key-ping?x_api_key=nope",
             expect=401),
        dict(stage=2, name="POST /forms/echo", method="POST", path="/forms/echo", expect=200,
             kwargs={"data": {"name": "widget", "qty": "3"}}),
        dict(stage=2, name="POST /upload (multipart)", method="POST", path="/upload", expect=200,
             kwargs={"files": {"file": ("audit.txt", b"hello audit", "text/plain")}}),
        dict(stage=2, name="GET /stats (aggregate)", method="GET", path="/stats", expect=200,
             drop=["generated_at"]),
        dict(stage=2, name="GET /items/expensive (orjson)", method="GET", path="/items/expensive",
             expect=200),
        # ── stage 3: streaming + realtime + docs ─────────────────────────
        dict(stage=3, name="SSE /sse/ticks", method="GET", path="/sse/ticks?count=3", expect=200,
             raw_body=True, check_headers=["x-sse-count", "content-type"]),
        dict(stage=3, name="CORS preflight", method="OPTIONS", path="/ping", expect=200,
             kwargs={"headers": {"Origin": "http://localhost:3000",
                                 "Access-Control-Request-Method": "GET"}},
             check_header_names=["access-control-allow-origin"]),
        # Parity note: FastAPI does NOT auto-serve HEAD on GET routes (405).
        # fasterapi used to answer 200 here — fixed in routing.py to match.
        dict(stage=3, name="HEAD /ping (405 parity)", method="HEAD", path="/ping", expect=405),
        dict(stage=3, name="GET /docs (surface)", method="GET", path="/docs", expect=200,
             status_only=True),
        dict(stage=3, name="websocket echo (in-process)", method="WS", path="/ws/echo", expect=200,
             ws_send="audit-ping"),
    ]


# ─────────────────────────────────────────────────────── server control ──

def reset_dbs() -> None:
    """Fresh seed for both apps every run — differential probes that expose
    mutable state (e.g. /stats item counts) are only comparable when both
    DBs start identical. Perf-phase writes happen after the diff phase."""
    for f in HERE.glob("audit_*.db"):
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(f) + suffix).unlink()
            except FileNotFoundError:
                pass


def start_server(app_module: str, port: int) -> subprocess.Popen:
    cmd = [sys.executable, "-m", "uvicorn", f"{app_module}:app",
           "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"]
    return subprocess.Popen(cmd, cwd=str(HERE),
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                            env={**os.environ, "PYTHONUNBUFFERED": "1"})


def wait_ready(port: int, timeout: float = 40.0) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def stop(p: subprocess.Popen | None) -> None:
    if p is None:
        return
    p.terminate()
    try:
        p.wait(timeout=5)
    except Exception:
        p.kill()


# ───────────────────────────────────────────────────── differential run ──

async def fetch(http, method: str, url: str, probe: dict, token: str | None) -> Any:
    kwargs: dict[str, Any] = dict(probe.get("kwargs", {}))
    if token and "headers" not in kwargs:
        kwargs["headers"] = {}
    if token:
        kwargs["headers"] = {**kwargs.get("headers", {}), "authorization": f"Bearer {token}"}
    if probe["method"] == "HEAD":
        r = await http.head(url)
    elif probe["method"] == "OPTIONS":
        r = await http.options(url, **kwargs)
    elif probe["method"] == "POST":
        r = await http.post(url, **kwargs)
    else:
        r = await http.get(url, **kwargs)
    return r


async def run_probe_against(base: str, probe: dict, token: str | None) -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=30) as http:
        r = await fetch(http, probe["method"], base + probe["path"], probe, token)
        result: dict[str, Any] = {"status": r.status_code}
        if "check_headers" in probe:
            result["headers"] = {h: r.headers.get(h, "") for h in probe["check_headers"]}
        if "check_header_names" in probe:
            # only presence matters (values may be nondeterministic, e.g. request ids)
            result["header_names"] = sorted(h for h in r.headers.keys()
                                             if h in probe["check_header_names"])
        if probe.get("status_only"):
            return result
        if probe.get("openapi_paths"):
            body = r.json()
            result["paths"] = sorted(body.get("paths", {}).keys())
            result["title"] = body.get("info", {}).get("title")
            return result
        if probe.get("raw_body") or probe["method"] in ("HEAD", "OPTIONS"):
            result["body"] = r.text
            return result
        try:
            body = r.json()
        except Exception:
            body = r.text
        if isinstance(body, dict) and probe.get("drop"):
            body = {k: v for k, v in body.items() if k not in probe["drop"]}
        result["body"] = body
        return result


async def ws_probe(app_module: str) -> dict:
    """WebSocket parity via each framework's TestClient (in-process)."""
    mod = __import__(app_module)
    if app_module == "fastapi_app":
        from fastapi.testclient import TestClient  # type: ignore[attr-defined]
    else:
        from fasterapi.testclient import TestClient  # type: ignore[attr-defined]
    with TestClient(mod.app) as client:  # enters lifespan (seeds db)
        with client.websocket_connect("/ws/echo") as ws:
            ws.send_text("audit-ping")
            return {"status": 200, "body": ws.receive_json()}


async def differential(stages: list[int]) -> tuple[list[dict], bool]:
    token_cache: dict[str, str] = await get_tokens_pair()
    probes = [p for p in build_probes(token_cache) if p["stage"] in stages]
    rows: list[dict] = []
    all_ok = True

    # get token per server for probes that need auth
    async def token_for(side: str, user: str) -> str | None:
        return token_cache.get(f"{side}:{user}") or token_cache.get(user)

    for probe in probes:
        entry: dict[str, Any] = {"stage": probe["stage"], "name": probe["name"]}
        try:
            if probe["method"] == "WS":
                a = await ws_probe("fastapi_app")
                b = await ws_probe("fasterapi_app")
                ta = tb = None
            else:
                ta = await token_for("a", probe.get("auth", "")) if probe.get("auth") else None
                tb = await token_for("b", probe.get("auth", "")) if probe.get("auth") else None
                a = await run_probe_against(f"http://127.0.0.1:{PORT_A}", probe, ta)
                b = await run_probe_against(f"http://127.0.0.1:{PORT_B}", probe, tb)
        except Exception as exc:
            entry.update(ok=False, verdict="error", error=repr(exc), a=None, b=None)
            all_ok = False
            rows.append(entry)
            continue

        ok = a.get("status") == b.get("status") == probe["expect"] and a.get("body") == b.get("body")
        if ok and a.get("headers") is not None:
            ok = a["headers"] == b["headers"]
        if ok and a.get("header_names") is not None:
            ok = a["header_names"] == b["header_names"]
        if ok and a.get("paths") is not None:
            ok = a["paths"] == b["paths"]
        if ok and probe.get("openapi_paths"):
            ok = a.get("title") == b.get("title")
        entry.update(ok=bool(ok), a=a, b=b,
                     verdict="PASS" if ok else f"MISMATCH (expect {probe['expect']})")
        all_ok = all_ok and bool(ok)
        rows.append(entry)
    return rows, all_ok


async def get_tokens_pair() -> dict[str, str]:
    """Login on both servers once; cache tokens for probes + load phase."""
    import httpx

    cache: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=15) as http:
        for side, port in (("a", PORT_A), ("b", PORT_B)):
            for user, pw in (("alice", "wonderland"), ("bob", "builder")):
                r = await http.post(f"http://127.0.0.1:{port}/auth/token",
                                    data={"username": user, "password": pw})
                if r.status_code == 200:
                    cache[f"{side}:{user}"] = r.json()["access_token"]
    return cache


# ──────────────────────────────────────────────────────────── perf run ──

PERF_ENDPOINTS: dict[int, list[dict[str, Any]]] = {
    1: [dict(name="GET /ping", method="GET", path="/ping")],
    2: [
        dict(name="GET /items (db+paginate)", method="GET", path="/items?page=2&per_page=10"),
        # Write endpoints hit SQLite's single-writer ceiling; a gentler wave
        # keeps the pool healthy for BOTH apps (same limits = fair compare).
        dict(name="POST /items (auth+db+bg)", method="POST", path="/items",
             json_body={"name": "load-item", "price": 3.25, "tags": ["load"]}, auth="alice",
             expect=201, requests=300, concurrency=10),
        dict(name="GET /items/expensive (compute)", method="GET", path="/items/expensive"),
    ],
    3: [
        dict(name="GET /me (jwt+db)", method="GET", path="/me", auth="alice"),
        dict(name="GET /sse/ticks (stream)", method="GET", path="/sse/ticks?count=1"),
    ],
}


async def load(url: str, requests: int, concurrency: int, method: str = "GET",
               json_body: dict | None = None, token: str | None = None,
               expect: int = 200) -> list[float]:
    import httpx

    headers = {"authorization": f"Bearer {token}"} if token else {}
    lat: list[float] = []
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=60, headers=headers) as client:
        async def one() -> None:
            async with sem:
                t0 = time.perf_counter()
                if method == "POST":
                    r = await client.post(url, json=json_body)
                else:
                    r = await client.get(url)
                assert r.status_code == expect, f"{r.status_code} on {url}"
                lat.append((time.perf_counter() - t0) * 1000)

        for _ in range(min(30, requests)):
            await one()
        lat.clear()
        t0 = time.perf_counter()
        await asyncio.gather(*[one() for _ in range(requests)])
        _ = t0  # elapsed measured by caller via returned list count only
    return lat


def summarize(lat: list[float], elapsed_s: float) -> dict:
    s = sorted(lat)

    def pct(p: float) -> float:
        if not s:
            return 0.0
        return s[min(len(s) - 1, int(p / 100 * len(s)))]

    return {"requests": len(s), "rps": round(len(s) / elapsed_s, 1) if elapsed_s > 0 else 0.0,
            "p50_ms": round(pct(50), 2), "p90_ms": round(pct(90), 2),
            "p99_ms": round(pct(99), 2),
            "mean_ms": round(statistics.fmean(s), 2) if s else 0.0}


async def timed_load(url: str, requests: int, concurrency: int, **kw: Any) -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=60) as client:
        t0 = time.perf_counter()
        try:
            await client.get(url)
        except Exception:
            pass
    lat = await load(url, requests, concurrency, **kw)
    elapsed = time.perf_counter() - t0
    return summarize(lat, elapsed)


def verdict_for(speedup: float) -> str:
    if speedup >= 1.05:
        return "fasterapi"
    if speedup <= 0.95:
        return "fastapi"
    return "parity"


async def perf(stages: list[int], requests: int, concurrency: int) -> tuple[list[dict], bool]:
    token_cache = await get_tokens_pair()
    rows: list[dict] = []
    fasterapi_wins_any = False
    for stage in stages:
        for ep in PERF_ENDPOINTS.get(stage, []):
            kw: dict[str, Any] = {"method": ep["method"], "expect": ep.get("expect", 200)}
            if "json_body" in ep:
                kw["json_body"] = ep["json_body"]
            n_req = ep.get("requests", requests)
            n_con = ep.get("concurrency", concurrency)
            if ep.get("auth"):
                kw["token"] = token_cache.get(f"a:{ep['auth']}")
                kw_b = {**kw, "token": token_cache.get(f"b:{ep['auth']}")}
            else:
                kw_b = kw
            ra = await timed_load(f"http://127.0.0.1:{PORT_A}{ep['path']}", n_req, n_con, **kw)
            rb = await timed_load(f"http://127.0.0.1:{PORT_B}{ep['path']}", n_req, n_con, **kw_b)
            speedup = round(rb["rps"] / ra["rps"], 3) if ra["rps"] else 0.0
            v = verdict_for(speedup)
            fasterapi_wins_any = fasterapi_wins_any or v == "fasterapi"
            rows.append({"stage": stage, "endpoint": ep["name"], "fastapi": ra,
                         "fasterapi": rb, "speedup": speedup, "verdict": v})
            print(f"  [stage {stage}] {ep['name']}: fastapi {ra['rps']} rps vs "
                  f"fasterapi {rb['rps']} rps -> x{speedup} ({v})")
    return rows, fasterapi_wins_any


# ─────────────────────────────────────────────────────────── reporting ──

def env_meta() -> dict:
    import fastapi
    import uvicorn

    import fasterapi

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpus": os.cpu_count(),
        "fastapi": getattr(fastapi, "__version__", "?"),
        "fasterapi": getattr(fasterapi, "__version__", "?"),
        "fasterapi_has_rust": bool(getattr(fasterapi, "HAS_RUST", False)),
        "uvicorn": getattr(uvicorn, "__version__", "?"),
    }


def fmt_speedup(x: float) -> str:
    return f"x{x:.2f}"


def write_report(record: dict) -> None:
    meta = record["meta"]
    lines: list[str] = []
    lines.append("# fasterapi vs FastAPI — running audit")
    lines.append("")
    lines.append(f"> Generated by `examples/comparison/audit.py` — do not edit by hand. "
                 f"Last run: `{meta['timestamp']}`.")
    lines.append("")
    lines.append(f"**Env:** python {meta['python']} · fastapi {meta['fastapi']} · "
                 f"fasterapi {meta['fasterapi']} (rust={meta['fasterapi_has_rust']}) · "
                 f"uvicorn {meta['uvicorn']} · {meta['cpus']} cpus")
    lines.append("")
    lines.append("Both apps run on the **same uvicorn host** with the same shared stack "
                 "(`stack/`: config+YAML, PyJWT auth, async SQLAlchemy + aiosqlite, pydantic v2 "
                 "schemas, orjson). The only variable is the framework.")
    lines.append("")

    # verdict block per stage
    lines.append("## Latest run — verdicts")
    lines.append("")
    for stage in record["stages"]:
        diff_rows = [r for r in record["differential"] if r["stage"] == stage]
        perf_rows = [r for r in record["perf"] if r["stage"] == stage]
        n_ok = sum(1 for r in diff_rows if r.get("ok"))
        speedups = [r["speedup"] for r in perf_rows] or [1.0]
        best = max(speedups)
        worst = min(speedups)
        lines.append(f"### Stage {stage} — {STAGES[stage]['name']}")
        lines.append("")
        lines.append(f"- Differential: **{n_ok}/{len(diff_rows)} probes match** "
                     f"({'ALL PASS' if n_ok == len(diff_rows) and diff_rows else 'MISMATCHES PRESENT'})")
        if perf_rows:
            lines.append(f"- Perf: speedup range **{fmt_speedup(worst)} … {fmt_speedup(best)}** "
                         f"(geomean **{fmt_speedup(round(_geomean(speedups), 3))}**)")
        lines.append(f"- Complexity included: {STAGES[stage]['includes']}")
        lines.append("")

    # detail tables
    lines.append("## Differential detail (latest run)")
    lines.append("")
    for stage in record["stages"]:
        lines.append(f"### Stage {stage}")
        lines.append("")
        lines.append("| Probe | fastapi | fasterapi | Verdict |")
        lines.append("|---|---|---|---|")
        for r in (x for x in record["differential"] if x["stage"] == stage):
            sa = r["a"].get("status") if r.get("a") else "ERR"
            sb = r["b"].get("status") if r.get("b") else "ERR"
            lines.append(f"| {r['name']} | {sa} | {sb} | {r['verdict']} |")
        lines.append("")

    lines.append("## Perf detail (latest run)")
    lines.append("")
    lines.append("| Stage | Endpoint | fastapi RPS | fasterapi RPS | Speedup | p50 fa→fa2 | Verdict |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in record["perf"]:
        fa, fb = r["fastapi"], r["fasterapi"]
        lines.append(f"| {r['stage']} | {r['endpoint']} | {fa['rps']} | {fb['rps']} | "
                     f"{fmt_speedup(r['speedup'])} | {fa['p50_ms']}→{fb['p50_ms']} ms | {r['verdict']} |")
    lines.append("")

    # mismatches, if any
    bad = [r for r in record["differential"] if not r.get("ok")]
    if bad:
        lines.append("## ⚠️ Mismatches (latest run)")
        lines.append("")
        for r in bad:
            lines.append(f"- `{r['name']}`: {r.get('verdict')}")
            lines.append(f"  - fastapi: `{json.dumps(r.get('a'), default=str)[:400]}`")
            lines.append(f"  - fasterapi: `{json.dumps(r.get('b'), default=str)[:400]}`")
        lines.append("")

    # run history
    hist = read_history()
    if hist:
        lines.append("## Run history (accumulating)")
        lines.append("")
        lines.append("| Run | Stages | Differential | Perf speedup range | Verdict |")
        lines.append("|---|---|---|---|---|")
        for h in hist[-12:]:
            sp = h.get("speedups") or {}
            lo = min(sp.values()) if sp else None
            hi = max(sp.values()) if sp else None
            rng = f"{fmt_speedup(lo)} … {fmt_speedup(hi)}" if lo is not None else "n/a"
            lines.append(f"| {h['meta']['timestamp']} | {','.join(map(str, h['stages']))} | "
                         f"{h['diff_passed']}/{h['diff_total']} | {rng} | {h.get('headline', 'n/a')} |")
        lines.append("")

    lines.append("## Reading the numbers")
    lines.append("")
    lines.append("- Both apps run on **the same uvicorn host** with the same shared stack "
                 "(async SQLAlchemy + aiosqlite, PyJWT, orjson, pydantic v2), so latency "
                 "deltas isolate framework overhead (routing, validation, DI solving, "
                 "response building).")
    lines.append("- DB endpoints are dominated by SQLite/SQLAlchemy work common to both; "
                 "`/ping` isolates pure request plumbing; `/items/expensive` is handler-CPU-bound.")
    lines.append("- Loopback RPS swings ±15% run-to-run; treat sub-x1.05 gaps as noise. "
                 "The differential phase is deterministic and is the hard gate.")
    lines.append("- fasterapi's next perf lever (per its README) is the Hyper inverted host "
                 "(`FASTERAPI_HYPER=1`), which this audit can pick up once promoted.")
    lines.append("")
    lines.append("## Stage ladder — complexity added over time")
    lines.append("")
    lines.append("| Stage | Name | What got added |")
    lines.append("|---|---|---|")
    for s, d in STAGES.items():
        lines.append(f"| {s} | {d['name']} | {d['includes']} |")
    lines.append("")
    lines.append("Next stages (add to `STAGES` + probes + `PERF_ENDPOINTS`, then rerun): "
                 "APIRouter sub-packages, response_model validation errors, "
                 "deeper ORM relationships, caching layer, scheduled jobs, file streaming, "
                 "rate-limited routes, multi-worker load.")
    lines.append("")

    (HERE / "AUDIT.md").write_text("\n".join(lines), encoding="utf-8")


def _geomean(xs: list[float]) -> float:
    import math

    return math.exp(statistics.fmean(math.log(max(x, 1e-9)) for x in xs))


def read_history() -> list[dict]:
    if not HISTORY_JSONL.exists():
        return []
    out = []
    for line in HISTORY_JSONL.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def append_history(record: dict) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    diff_passed = sum(1 for r in record["differential"] if r.get("ok"))
    speedups = {r["endpoint"]: r["speedup"] for r in record["perf"]}
    headline = ("ALL PARITY" if not speedups else
                ("fasterapi ahead" if max(speedups.values()) >= 1.05 else
                 ("fastapi ahead" if min(speedups.values()) <= 0.95 else "parity band")))
    slim = {"meta": record["meta"], "stages": record["stages"],
            "diff_passed": diff_passed, "diff_total": len(record["differential"]),
            "speedups": speedups, "headline": headline,
            "perf": record["perf"], "differential": record["differential"]}
    with open(HISTORY_JSONL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(slim, default=str) + "\n")
    record["speedups"] = speedups
    record["diff_passed"] = diff_passed
    record["diff_total"] = len(record["differential"])
    record["headline"] = headline


# ─────────────────────────────────────────────────────────────── main ──

def main() -> int:
    ap = argparse.ArgumentParser(description="fastapi vs fasterapi audit")
    ap.add_argument("--stages", default="1,2,3")
    ap.add_argument("--requests", type=int, default=1000)
    ap.add_argument("--concurrency", type=int, default=50)
    ap.add_argument("--diff-only", action="store_true")
    ap.add_argument("--perf-only", action="store_true")
    args = ap.parse_args()
    stages = sorted({int(s) for s in args.stages.split(",") if s.strip()})

    print(f"[audit] stages={stages} requests={args.requests} concurrency={args.concurrency}")
    reset_dbs()
    pa = pb = None
    try:
        pa = start_server("fastapi_app", PORT_A)
        pb = start_server("fasterapi_app", PORT_B)
        if not (wait_ready(PORT_A) and wait_ready(PORT_B)):
            print("ERROR: servers failed to boot", file=sys.stderr)
            return 1
        print(f"[audit] fastapi :{PORT_A}  fasterapi :{PORT_B}  both ready")

        record: dict[str, Any] = {"meta": env_meta(), "stages": stages,
                                  "differential": [], "perf": []}
        ok = True
        if not args.perf_only:
            print("[audit] differential phase…")
            record["differential"], diff_ok = asyncio.run(differential(stages))
            n_ok = sum(1 for r in record["differential"] if r.get("ok"))
            ok = ok and diff_ok
            print(f"[audit] differential: {n_ok}/{len(record['differential'])} probes match")
            for r in record["differential"]:
                mark = "PASS" if r.get("ok") else "FAIL"
                print(f"  [{mark}] s{r['stage']} {r['name']}" + ("" if r.get("ok") else f" — {r['verdict']}"))

        if not args.diff_only:
            print("[audit] perf phase…")
            record["perf"], _ = asyncio.run(perf(stages, args.requests, args.concurrency))

        append_history(record)
        write_report(record)
        print(f"[audit] report -> {HERE / 'AUDIT.md'}  history -> {HISTORY_JSONL}")
        return 0 if ok else 1
    finally:
        stop(pa)
        stop(pb)


if __name__ == "__main__":
    raise SystemExit(main())
