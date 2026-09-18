#!/usr/bin/env bash
# Quickstart: create venv, install deps, run parity tests + micro bench.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"
python3 -m venv .venv 2>/dev/null || true
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -U pip maturin
pip install -e ".[test,bench]"
bash scripts/install_hooks.sh
pytest tests/ -q
python benchmarks/benchmark_differential.py --micro-only
echo "OK — try: uvicorn examples.quickstart:app --reload"
