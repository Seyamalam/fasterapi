#!/usr/bin/env bash
# scripts/soak-hyper.sh — sustained-load correctness soak for the Hyper host.
#
# Boots the comparison fasterapi app under Hyper, hammers mixed GET/POST
# (with reconnect churn) for --duration seconds, and asserts:
#   * zero non-2xx/4xx-expected responses (no 500s, no timeouts),
#   * RSS growth bounded (default < 150MB over the run).
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

DURATION="${1:-120}"
PORT="${FASTERAPI_SOAK_PORT:-8179}"
if [[ -n "${FASTERAPI_PY:-}" ]]; then PY="$FASTERAPI_PY";
elif [[ -x "$ROOT/.venv/bin/python" ]]; then PY="$ROOT/.venv/bin/python";
elif [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then PY="$VIRTUAL_ENV/bin/python";
else PY="python3"; fi
export PYTHONPATH="$ROOT/python${PYTHONPATH:+:$PYTHONPATH}"

echo "=== hyper soak: ${DURATION}s on :${PORT} ==="
"$PY" - "$PORT" "$DURATION" <<'PYEOF'
import asyncio
import os
import subprocess
import sys
import time
import urllib.request

port, duration = int(sys.argv[1]), int(sys.argv[2])
env = dict(os.environ, FASTERAPI_HYPER="1")
env.pop("PYTHONPATH", None)
srv = subprocess.Popen(
    [sys.executable, "-m", "fasterapi", "run", "fasterapi_app:app",
     "--host", "127.0.0.1", "--port", str(port), "--hyper"],
    cwd="examples/comparison", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
)
try:
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=2)
            break
        except Exception:
            time.sleep(0.3)

    import psutil
    proc = psutil.Process(srv.pid)
    rss0 = proc.memory_info().rss

    async def main():
        import httpx

        bad = {"n": 0}
        total = {"n": 0}
        stop = time.time() + duration

        async def worker(seed: int):
            # reconnect churn: fresh client every wave
            while time.time() < stop:
                async with httpx.AsyncClient(timeout=20) as c:
                    r = await c.post(f"http://127.0.0.1:{port}/auth/token",
                                     data={"username": "alice", "password": "wonderland"})
                    total["n"] += 1
                    if r.status_code != 200:
                        bad["n"] += 1
                        continue
                    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
                    for i in range(10):
                        if time.time() >= stop:
                            break
                        r = await c.post(f"http://127.0.0.1:{port}/items",
                                         json={"name": f"s{seed}-{i}", "price": 1.5}, headers=h)
                        total["n"] += 1
                        if r.status_code not in (200, 201):
                            bad["n"] += 1
                        r = await c.get(f"http://127.0.0.1:{port}/items?limit=5", headers=h)
                        total["n"] += 1
                        if r.status_code != 200:
                            bad["n"] += 1

        await asyncio.gather(*[worker(s) for s in range(8)])
        return total["n"], bad["n"]

    total, bad = asyncio.run(main())
    rss1 = proc.memory_info().rss
    growth_mb = (rss1 - rss0) / 1e6
    print(f"requests={total} bad={bad} rss_start={rss0/1e6:.0f}MB rss_end={rss1/1e6:.0f}MB growth={growth_mb:.0f}MB")
    if bad:
        print(f"SOAK FAILED: {bad} bad responses", file=sys.stderr)
        sys.exit(1)
    if growth_mb > 150:
        print(f"SOAK FAILED: RSS grew {growth_mb:.0f}MB", file=sys.stderr)
        sys.exit(1)
    print("SOAK PASSED")
finally:
    srv.terminate()
    try:
        srv.wait(timeout=15)
    except Exception:
        srv.kill()
PYEOF
