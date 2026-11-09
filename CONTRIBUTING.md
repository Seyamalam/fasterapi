# Contributing to fasterapi

## Setup

```bash
bash scripts/quickstart.sh   # venv + deps + hooks + tests
# or: uv venv .venv && pip install -e ".[test,bench]" && maturin develop --release
```

`pip install -e` builds the Rust extension via maturin. Pure-Python works
without it (`fasterapi.HAS_RUST` tells you which path is live).

## The one rule: parity is king

fasterapi's contract is **byte-identical FastAPI behavior**. Every change
must keep the differential suite green:

```bash
pytest tests/ -q                          # full suite (differential vs real FastAPI)
pytest tests/test_parity_extended.py -q   # edge cases: DI, mounts, ordering, errors
```

Writing a fix? Add a **differential test first**: build the same app with
`fastapi.FastAPI()` and `fasterapi.FastAPI()`, assert identical wire output.
Regression tests live in `tests/test_parity*.py`.

## Performance claims need numbers

- Micro fast-paths: `python benchmarks/benchmark_differential.py --micro-only`
- Full loopback: `python benchmarks/benchmark_differential.py --quick`
- Measure end-to-end, not just primitives — a fast `matchit` lookup means
  nothing if dispatch still routes through Python (that gap is now closed
  via `FastRouter`; keep it that way).

README tables are auto-generated — `scripts/update_readme_tables.py` runs
on every commit via `.githooks/pre-commit`. Don't edit `PARITY:`/`BENCH:`
blocks by hand.

## Hooks

```bash
bash scripts/install_hooks.sh
```

- `pre-commit`: cargo fmt (advisory) + ruff (advisory) + README refresh + fast tests
- `pre-push`: full parity suite (reject on failure) + perf gates

## Areas with deliberate trade-offs (know before changing)

- `FastRouter` falls back to Starlette's linear scan for anything it can't
  prove identical (ambiguous pattern orderings, Mount/Host, convertor
  patterns, method mismatches). Correctness beats speed here.
- `_solve_inner` fans out only *yield-free* dep subtrees — yield deps stay
  sequential because their stack-entry order determines teardown order.
- `fast_json_loads` delegates to stdlib: measured slower at every payload
  size (PyObject materialization dominates). The Rust JSON writer is the
  fast path and is used in response serialization.
- The Hyper host is experimental; `serve()` defaults to uvicorn. See
  `docs/migration.md` for the known-differences table.

## Commit style

Conventional commits (`feat:`, `fix:`, `perf:`, `chore:`, `docs:`).
Parity bugs: `fix:` + differential regression test. Perf changes:
`perf:` + bench numbers in the commit message or README table refresh.
