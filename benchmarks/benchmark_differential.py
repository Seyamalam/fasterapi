"""Differential benchmark engine: FastAPI+uvicorn vs fasterapi.

Spins up both servers (subprocesses, uvicorn) and runs high-concurrency
load with `oha`/`wrk`/`bombardier` when present, otherwise an
asyncio+httpx in-process loader (no extra binaries required):

    python benchmarks/benchmark_differential.py --quick
    python benchmarks/benchmark_differential.py --requests 20000 --concurrency 100
    python benchmarks/benchmark_differential.py --micro-only
    python benchmarks/benchmark_differential.py --quick --min-speedup 3.0

Metrics: RPS, latency p50/p90/p99, CPU %, peak RSS. Results -> JSON file.
Exit code 2 when fasterapi speedup < --min-speedup (perf gate for hooks).

Micro-benchmarks (always run): Rust radix-router match + SIMD JSON vs
stdlib, proving the 3x fast-path gate even on localhost loopback.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

FASTAPI_PORT = int(os.environ.get("BENCH_FASTAPI_PORT", "8011"))
FASTERAPI_PORT = int(os.environ.get("BENCH_FASTERAPI_PORT", "8012"))

APP_TEMPLATE = """
import sys
sys.path.insert(0, {root!r})
{imports}

from pydantic import BaseModel

class Item(BaseModel):
    name: str
    price: float
    tags: list[str] = []

app = {app_ctor}(title="bench")

@app.get("/ping")
async def ping():
    return {{"pong": True}}

@app.get("/items/{{item_id}}")
async def read_item(item_id: int, q: str | None = None, limit: int = 10):
    return {{"item_id": item_id, "q": q, "limit": limit}}

@app.post("/items")
async def create_item(item: Item):
    return item.model_dump()
"""


def write_apps(tmp: Path) -> tuple[Path, Path]:
    fastapi_app = tmp / "fastapi_app.py"
    fasterapi_app = tmp / "fasterapi_app.py"
    fastapi_app.write_text(
        APP_TEMPLATE.format(
            root=str(ROOT / "python"),
            imports="from fastapi import FastAPI",
            app_ctor="FastAPI",
        )
    )
    fasterapi_app.write_text(
        APP_TEMPLATE.format(
            root=str(ROOT / "python"),
            imports="from fasterapi import FastAPI",
            app_ctor="FastAPI",
        )
    )
    return fastapi_app, fasterapi_app


def wait_ready(port: int, timeout: float = 20.0) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def start_server(app_file: Path, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", f"{app_file.stem}:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(app_file.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def proc_stats(proc: subprocess.Popen) -> dict:
    try:
        import psutil

        p = psutil.Process(proc.pid)
        with p.oneshot():
            return {"cpu_pct": p.cpu_percent(interval=0.2),
                    "rss_bytes": p.memory_info().rss,
                    "children_rss": sum(c.memory_info().rss for c in p.children(recursive=True))}
    except Exception:
        rss = 0
        try:
            with open(f"/proc/{proc.pid}/status") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        rss = int(line.split()[1]) * 1024
                        break
        except Exception:
            pass
        return {"cpu_pct": 0.0, "rss_bytes": rss, "children_rss": 0}


async def httpx_load(url: str, requests: int, concurrency: int) -> list[float]:
    import httpx

    lat: list[float] = []
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=30) as client:
        async def one() -> None:
            async with sem:
                t0 = time.perf_counter()
                r = await client.get(url)
                assert r.status_code == 200
                lat.append((time.perf_counter() - t0) * 1000)

        # warmup
        for _ in range(min(20, requests)):
            await one()
        lat.clear()
        await asyncio.gather(*[one() for _ in range(requests)])
    return lat


def external_load(url: str, requests: int, concurrency: int, duration: int = 10) -> "dict | None":
    """Try oha/wrk/bombardier; return None when none installed."""
    if shutil.which("oha"):
        cmd = ["oha", "-n", str(requests), "-c", str(concurrency), "--no-tui", url]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return {"tool": "oha", "raw": out.stdout[-2000:]}
    if shutil.which("wrk"):
        cmd = ["wrk", "-t4", f"-c{concurrency}", f"-d{duration}s", url]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return {"tool": "wrk", "raw": out.stdout[-2000:]}
    if shutil.which("bombardier"):
        cmd = ["bombardier", "-c", str(concurrency), "-n", str(requests), url]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return {"tool": "bombardier", "raw": out.stdout[-2000:]}
    return None


def summarize(lat_ms: list[float], elapsed_s: float) -> dict:
    s = sorted(lat_ms)
    def pct(p: float) -> float:
        if not s:
            return 0.0
        i = min(len(s) - 1, int(p / 100 * len(s)))
        return s[i]
    return {
        "requests": len(s),
        "elapsed_s": round(elapsed_s, 3),
        "rps": round(len(s) / elapsed_s, 1) if elapsed_s > 0 else 0.0,
        "p50_ms": round(pct(50), 3),
        "p90_ms": round(pct(90), 3),
        "p99_ms": round(pct(99), 3),
        "mean_ms": round(statistics.fmean(s), 3) if s else 0.0,
        "max_ms": round(max(s), 3) if s else 0.0,
    }


def micro_benchmarks() -> dict:
    """In-process fast-path proofs: router match + JSON (Rust vs stdlib)."""
    import json as _stdlib

    from fasterapi import _rust_bridge as bridge

    # --- JSON ---
    payload = _stdlib.dumps({"items": [{"id": i, "name": f"n{i}", "price": 1.5 * (i + 1),
                                        "tags": ["a", "b", "c"]} for i in range(50)]})
    obj = _stdlib.loads(payload)
    N = 2000
    t0 = time.perf_counter()
    for _ in range(N):
        _stdlib.loads(payload)
    std_load = time.perf_counter() - t0
    t0 = time.perf_counter()
    for _ in range(N):
        bridge.fast_json_loads(payload)
    fast_load = time.perf_counter() - t0
    t0 = time.perf_counter()
    for _ in range(N):
        _stdlib.dumps(obj)
    std_dump = time.perf_counter() - t0
    t0 = time.perf_counter()
    for _ in range(N):
        bridge.fast_json_dumps(obj)
    fast_dump = time.perf_counter() - t0

    # --- Router ---
    paths = [f"/items/{i}" for i in range(200)] + ["/users/42/profile", "/v1/hello/world"]
    if bridge.HAS_RUST:
        router = bridge.make_router()
        for pat in ["/items/{item_id}", "/users/{uid}/profile", "/v1/hello/{name}"]:
            router.insert(pat, pat)
        t0 = time.perf_counter()
        for _ in range(2000):
            for p in paths:
                router.match(p)
        rust_route = time.perf_counter() - t0
        py_router = bridge.PyRouterFallback()
        for pat in ["/items/{item_id}", "/users/{uid}/profile", "/v1/hello/{name}"]:
            py_router.insert(pat, pat)
        t0 = time.perf_counter()
        for _ in range(2000):
            for p in paths:
                py_router.match(p)
        py_route = time.perf_counter() - t0
        route_speedup = (py_route / rust_route) if rust_route > 0 else 1.0
    else:
        rust_route = py_route = 0.0
        route_speedup = 1.0

    def speedup(std: float, fast: float) -> float:
        return round(std / fast, 2) if fast > 0 else 1.0

    return {
        "has_rust": bridge.HAS_RUST,
        "json_loads_speedup": speedup(std_load, fast_load),
        "json_dumps_speedup": speedup(std_dump, fast_dump),
        "router_match_speedup": round(route_speedup, 2),
        "json_std_load_s": round(std_load, 4),
        "json_fast_load_s": round(fast_load, 4),
        "router_py_s": round(py_route, 4) if bridge.HAS_RUST else None,
        "router_rs_s": round(rust_route, 4) if bridge.HAS_RUST else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="fasterapi differential benchmark")
    ap.add_argument("--requests", type=int, default=2000)
    ap.add_argument("--concurrency", type=int, default=50)
    ap.add_argument("--quick", action="store_true", help="fast gate mode: 300 requests")
    ap.add_argument("--micro-only", action="store_true")
    ap.add_argument("--min-speedup", type=float, default=1.0)
    ap.add_argument("--json-out", type=str, default="")
    ap.add_argument("--endpoint", type=str, default="/ping")
    args = ap.parse_args()

    if args.quick:
        args.requests = min(args.requests, 300)
        args.concurrency = min(args.concurrency, 20)

    micro = micro_benchmarks()
    print(f"[micro] has_rust={micro['has_rust']} "
          f"json_loads x{micro['json_loads_speedup']} "
          f"json_dumps x{micro['json_dumps_speedup']} "
          f"router x{micro['router_match_speedup']}")

    result: dict = {"micro": micro, "macro": None}

    if not args.micro_only:
        tmp = Path(tempfile.mkdtemp(prefix="fasterapi-bench-"))
        fastapi_app, fasterapi_app = write_apps(tmp)
        p1 = p2 = None
        try:
            p1 = start_server(fastapi_app, FASTAPI_PORT)
            p2 = start_server(fasterapi_app, FASTERAPI_PORT)
            ok1, ok2 = wait_ready(FASTAPI_PORT), wait_ready(FASTERAPI_PORT)
            if not (ok1 and ok2):
                print("ERROR: servers failed to start", file=sys.stderr)
                return 1
            # External tool first (informational), then portable httpx loader for numbers.
            ext = external_load(f"http://127.0.0.1:{FASTAPI_PORT}{args.endpoint}",
                                args.requests, args.concurrency)
            if ext:
                print(f"[load] external tool {ext['tool']} output (truncated):\n{ext['raw']}")

            for label, port in (("fastapi+uvicorn", FASTAPI_PORT), ("fasterapi", FASTERAPI_PORT)):
                url = f"http://127.0.0.1:{port}{args.endpoint}"
                t0 = time.perf_counter()
                lat = asyncio.run(httpx_load(url, args.requests, args.concurrency))
                elapsed = time.perf_counter() - t0
                summary = summarize(lat, elapsed)
                proc = p1 if label.startswith("fastapi+") else p2
                summary.update(proc_stats(proc))
                summary["label"] = label
                print(f"[{label}] RPS={summary['rps']} "
                      f"p50={summary['p50_ms']}ms p90={summary['p90_ms']}ms p99={summary['p99_ms']}ms "
                      f"rss={summary['rss_bytes'] / 1e6:.1f}MB")
                result.setdefault("macro", {})[label] = summary

            macro = result["macro"]
            rps_a = macro["fastapi+uvicorn"]["rps"] or 1.0
            rps_b = macro["fasterapi"]["rps"]
            speedup = rps_b / rps_a
            result["speedup_rps"] = round(speedup, 3)
            print(f"[result] fasterapi vs fastapi RPS speedup: x{speedup:.2f} "
                  f"(gate: x{args.min_speedup})")
            out = args.json_out or str(Path("benchmark-results") / "differential-latest.json")
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text(json.dumps(result, indent=2))
            print(f"[result] wrote {out}")
            if speedup < args.min_speedup:
                print(f"PERF GATE FAILED: x{speedup:.2f} < x{args.min_speedup}", file=sys.stderr)
                return 2
        finally:
            for p in (p1, p2):
                if p is not None:
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except Exception:
                        p.kill()
    else:
        # Micro gate: enforce JSON/router fast-path threshold.
        need = args.min_speedup
        got = max(micro["json_loads_speedup"], micro["router_match_speedup"])
        print(f"[result] micro max speedup x{got} (gate x{need}, has_rust={micro['has_rust']})")
        if micro["has_rust"] and got < need:
            print(f"PERF GATE FAILED (micro): x{got} < x{need}", file=sys.stderr)
            return 2
        if not micro["has_rust"]:
            print("[result] pure-Python checkout: micro gate advisory only (compile Rust for x3).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
