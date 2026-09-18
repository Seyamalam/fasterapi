#!/usr/bin/env bash
# scripts/free-threaded-check.sh — free-threading readiness audit.
#
# A `--disable-gil` (t-build) interpreter is required for a real GIL-off
# proof; this box ships a GIL build, so the script:
#   1. reports what the current interpreter supports,
#   2. runs the thread-soak suite (valid on any build),
#   3. if PYTHON_GIL=0 is honored, re-runs the suite + micro bench GIL-off.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

if [[ -n "${FASTERAPI_PY:-}" ]]; then PY="$FASTERAPI_PY";
elif [[ -x "$ROOT/.venv/bin/python" ]]; then PY="$ROOT/.venv/bin/python";
elif [[ -n "${VIRTUAL_ENV:-}" && -x "$VIRTUAL_ENV/bin/python" ]]; then PY="$VIRTUAL_ENV/bin/python";
else PY="python3"; fi
export PYTHONPATH="$ROOT/python${PYTHONPATH:+:$PYTHONPATH}"

echo "=== free-threading readiness ==="
"$PY" -c "import sysconfig; print('Py_GIL_DISABLED build:', sysconfig.get_config_var('Py_GIL_DISABLED'))"
"$PY" -c "import sys; print('GIL currently enabled:', sys._is_gil_enabled())"
echo "--- thread soak (any build) ---"
"$PY" -m pytest tests/test_threads.py -q
echo "--- GIL-off probe ---"
if PYTHON_GIL=0 "$PY" -c "import sys; raise SystemExit(0 if not sys._is_gil_enabled() else 1)" 2>/dev/null; then
  echo "GIL-off interpreter confirmed — running suite + micro bench without the GIL:"
  PYTHON_GIL=0 "$PY" -m pytest tests/ -q
  PYTHON_GIL=0 "$PY" benchmarks/benchmark_differential.py --micro-only
else
  echo "This interpreter is a GIL build (PYTHON_GIL=0 not honored)."
  echo "To prove GIL-off: build CPython with --disable-gil (or install a"
  echo "'t' release), create a venv, pip install -e '.[test]', then re-run:"
  echo "  PYTHON_GIL=0 FASTERAPI_PY=<t-venv> bash scripts/free-threaded-check.sh"
  echo "Design notes: Rust entry points are allow_threads + lock-clean"
  echo "(parking_lot), Python request state is per-request locals, shared"
  echo "caches use weak keys — no process-global mutables on the hot path."
fi
