# Hyper host promotion gates

The Rust Hyper/Tokio host promotes from experimental to default only when
**all** gates below pass on the same commit. Until then `serve()` defaults
to uvicorn and Hyper is opt-in (`FASTERAPI_HYPER=1` / `--hyper`).

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
| FileResponse sendfile | 256KiB, 800 req, 16 workers | **x0.21** (255 vs 1031 rps) — FAIL |
| Mixed workload (differential bench) | `benchmark_differential.py` | not yet run |

Promotion needs every row ≥ x1.00 on the same machine, same run.

## Status (as of latest commit)

- Gate 1: green (87 tests; ws test carries a load-sensitive flake with retry)
- Gate 2: streaming/disconnect/subprotocol landed; extensions + HTTP/2 open
- Gate 3: 3/5 workloads — micro-JSON x1.03, validated-POST x1.06, SSE ~x0.98.
  sendfile x0.21 FAILS: FileResponse graduates to a stream session, the
  sendfile lane serves the file but the parked session worker is never
  released (leak) — and throughput is 5x slower either way. Root fix:
  don't graduate sendfile-marked responses to sessions (Python-side), plus
  always stream_close parked sessions on non-stream responses (Rust-side).

Verdict: **Hyper remains experimental.** The trend is right — the socket
+ dispatch + JSON wins finally outweigh the Python-bridge cost — but the
matrix isn't complete and ws extensions are unhandled.
