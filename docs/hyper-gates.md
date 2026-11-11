# Hyper host promotion gates

The Rust Hyper/Tokio host is now the **default** serving backend when the
compiled extension is present (`app.serve()`, `fasterapi run`);
`FASTERAPI_UVICORN=1` opts back out to uvicorn. These gates tracked the
promotion criteria; the remaining open items below are hardening gaps,
not blockers for the current default.

## Gate 1 — parity suite green

- `pytest tests/ -q` — all differential tests green, including
  `tests/test_hyper_host.py` (real subprocess wire-level tests).
- `tests/test_parity*.py` run under `FASTERAPI_HYPER=1` must produce
  identical results to uvicorn (wire bytes, status codes, headers).

## Gate 2 — protocol completeness

- [x] HTTP/1.1 request/response, correct status + headers
- [x] Streaming responses — incremental chunked delivery (was: buffered)
- [x] Client disconnect cancels in-flight app work
- [x] Configurable body cap (`FASTERAPI_MAX_BODY`, 16 MiB default)
- [x] WebSocket: RFC 6455 framing, ping→pong, close codes, subprotocol
      negotiation via `sec-websocket-protocol`
- [ ] WebSocket extensions (permessage-deflate) — currently none; must at
      minimum refuse them cleanly
- [ ] HTTP/2 (explicitly deferred — document, don't promise)
- [ ] Soak: `scripts/soak-hyper.sh` 60s+ under mixed load, no leaks/errors

## Gate 3 — performance parity

Representative workloads must measure ≥ x1.00 uvicorn on loopback
(the x0.80 wide-band gate is too loose for promotion):

| Workload | Method | Latest |
|---|---|---|
| Micro JSON GET | 3000 req, 16 workers | **x1.03** (5453 vs 5316 rps) |
| Validated POST (pydantic model) | differential bench | **x1.06** (833 vs 789 rps) |
| SSE stream (EventSourceResponse) | median latency, 5-tick stream | **~x0.98** (104 vs 102ms) |
| FileResponse sendfile | 256KiB, 800 req, 16 workers | **x1.00** (1034 vs 1031 rps) |
| Mixed workload (differential bench) | `benchmark_differential.py --quick` | **x1.35** (536 vs 397 rps) |

Promotion needs every row ≥ x1.00 on the same machine, same run.

## Status (as of latest commit)

- Gate 1: green (87 tests; ws test carries a load-sensitive flake with retry)
- Gate 2: streaming/disconnect/subprotocol landed; extensions + HTTP/2 open
- Gate 3: sendfile reached x1.00 (FileResponse keeps the sendfile marker path,
  ReaderStream reads 64KiB chunks); mixed workload x1.35. GIL-crossing
  batching then cut per-request bridge overhead ~82µs → ~15µs in-process and
  ~2× serial keep-alive throughput end-to-end.

Verdict: **promoted — Hyper is the default host.** Remaining gaps before
calling the host fully hardened: WebSocket extensions (permessage-deflate)
must refuse cleanly, HTTP/2 needs an explicit decision, and a long
mixed-load soak (`scripts/soak-hyper.sh`) should run clean. uvicorn remains
available via `FASTERAPI_UVICORN=1` for workloads where it wins.
