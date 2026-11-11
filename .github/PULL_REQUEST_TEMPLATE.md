## Summary

<!-- What changed and why -->

## Verification

- [ ] `pytest tests/ -q` — all differential tests green
- [ ] `maturin develop --release` after any `src/` change
- [ ] Parity vs FastAPI verified for behavioral changes
- [ ] Benchmarked if perf-sensitive (`benchmarks/benchmark_differential.py`)
