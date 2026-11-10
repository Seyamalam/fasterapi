"""fasterapi._rust_bridge — optional native accelerator.

Tries to import the compiled ``_fasterapi_rs`` PyO3 extension. When it is
absent (pure-Python checkout, no ``maturin develop`` yet) every symbol falls
back to a thread-safe pure-Python implementation so the test suite and the
benchmark harness keep working. ``HAS_RUST`` tells callers which path is live.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Optional

try:  # pragma: no cover - depends on maturin build
    try:
        from fasterapi._fasterapi_rs import (  # type: ignore
            DependencyDAG as _RsDAG,
            RustRouter as _RsRouter,
            TokenBucket as _RsBucket,
            fast_json_dumps as _rs_dumps,
            fast_json_loads as _rs_loads,
            has_simd as _rs_has_simd,
            hyper_health as _rs_hyper_health,
            parse_query_pairs as _rs_parse_qs,
            percent_decode as _rs_pct_decode,
            process_rss_bytes as _rs_rss,
            serve_static_config as _rs_static_cfg,
            tokio_worker_count as _rs_workers,
            validate_bool as _rs_vbool,
            validate_float as _rs_vfloat,
            validate_int as _rs_vint,
            solve_scalars as _rs_solve_scalars,
            sse_encode as _rs_sse_encode,
            validate_str as _rs_vstr,
        )
    except ImportError:  # top-level layout (pip installed ext next to package)
        from _fasterapi_rs import (  # type: ignore
            DependencyDAG as _RsDAG,
            RustRouter as _RsRouter,
            TokenBucket as _RsBucket,
            fast_json_dumps as _rs_dumps,
            fast_json_loads as _rs_loads,
            has_simd as _rs_has_simd,
            hyper_health as _rs_hyper_health,
            parse_query_pairs as _rs_parse_qs,
            percent_decode as _rs_pct_decode,
            process_rss_bytes as _rs_rss,
            serve_static_config as _rs_static_cfg,
            tokio_worker_count as _rs_workers,
            validate_bool as _rs_vbool,
            validate_float as _rs_vfloat,
            validate_int as _rs_vint,
            solve_scalars as _rs_solve_scalars,
            sse_encode as _rs_sse_encode,
            validate_str as _rs_vstr,
        )

    HAS_RUST = True
except Exception:  # pure-python fallback
    HAS_RUST = False
    _RsRouter = None  # type: ignore
    _RsBucket = None  # type: ignore
    _RsDAG = None  # type: ignore
    _rs_loads = None  # type: ignore
    _rs_dumps = None  # type: ignore
    _rs_vint = None  # type: ignore
    _rs_vfloat = None  # type: ignore
    _rs_vbool = None  # type: ignore
    _rs_vstr = None  # type: ignore
    _rs_parse_qs = None  # type: ignore
    _rs_pct_decode = None  # type: ignore


# ---------------------------------------------------------------- JSON ---

def fast_json_loads(payload: str) -> Any:
    """Parse JSON.

    Measured at all payload sizes (x0.6–0.8): materializing a generic
    serde tree and walking it into PyObjects cannot beat CPython's fused
    C parser — PyObject creation dominates and is identical either way.
    Stdlib is therefore the primitive here; the Rust parser remains
    exported for Rust-internal flows. Bodies with a model go through
    Pydantic `validate_json` (single-pass, x2+) and never touch this.
    """
    return json.loads(payload)


def fast_json_dumps(obj: Any, *, strict: bool = False) -> str:
    """Serialize to compact UTF-8 JSON, Rust writer when available.

    ``strict=True`` mirrors ``json.dumps(..., allow_nan=False)``: non-finite
    floats raise ``ValueError`` instead of emitting NaN/Infinity.
    """
    if HAS_RUST:
        return _rs_dumps(obj, strict)  # type: ignore[misc]
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=not strict)


# -------------------------------------------------------------- Router ---

class PyRouterFallback:
    """Minimal `{param}` matcher used when the Rust ext is absent."""

    def __init__(self) -> None:
        self._routes: list[tuple[str, str]] = []
        self._lock = threading.RLock()

    def insert(self, pattern: str, target: str) -> None:
        with self._lock:
            self._routes.append((pattern, target))

    def match(self, path: str) -> Optional[tuple[str, dict[str, str]]]:
        with self._lock:
            routes = list(self._routes)
        for pattern, target in routes:
            params = _match_pattern(pattern, path)
            if params is not None:
                return target, params
        return None

    def __len__(self) -> int:
        with self._lock:
            return len(self._routes)


def _match_pattern(pattern: str, path: str) -> Optional[dict[str, str]]:
    p_segs = pattern.strip("/").split("/") if pattern.strip("/") else []
    s_segs = path.strip("/").split("/") if path.strip("/") else []
    if len(p_segs) != len(s_segs):
        return None
    out: dict[str, str] = {}
    for p, s in zip(p_segs, s_segs):
        if p.startswith("{") and p.endswith("}"):
            out[p[1:-1]] = s
        elif p != s:
            return None
    return out


def make_router():  # type: ignore[no-untyped-def]
    if HAS_RUST:
        return _RsRouter()  # type: ignore[operator]
    return PyRouterFallback()


# ---------------------------------------------------------- Rate limit ---

class PyTokenBucketFallback:
    """Thread-safe token bucket mirroring the Rust `governor` semantics."""

    def __init__(self, per_second: int, burst: Optional[int] = None) -> None:
        self._rate = max(1.0, float(per_second))
        self._capacity = float(burst if burst is not None else per_second)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        self._updated = now
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)

    def check(self) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


def make_token_bucket(per_second: int, burst: Optional[int] = None):  # type: ignore[no-untyped-def]
    if HAS_RUST:
        return _RsBucket(per_second, burst)  # type: ignore[operator]
    return PyTokenBucketFallback(per_second, burst)


# ----------------------------------------------------------------- DAG ---

class PyDAGFallback:
    def __init__(self) -> None:
        self._nodes: list[str] = []
        self._index: dict[str, int] = {}
        self._edges: list[tuple[int, int]] = []

    def add_node(self, name: str) -> int:
        if name in self._index:
            return self._index[name]
        i = len(self._nodes)
        self._nodes.append(name)
        self._index[name] = i
        return i

    def add_edge(self, frm: str, to: str) -> None:
        a = self.add_node(frm)
        b = self.add_node(to)
        if (a, b) not in self._edges:
            self._edges.append((a, b))

    def node_count(self) -> int:
        return len(self._nodes)

    def edge_count(self) -> int:
        return len(self._edges)

    def topological_sort(self) -> list[str]:
        from collections import deque

        n = len(self._nodes)
        indeg = [0] * n
        adj: list[list[int]] = [[] for _ in range(n)]
        for a, b in self._edges:
            adj[a].append(b)
            indeg[b] += 1
        q = deque([i for i, d in enumerate(indeg) if d == 0])
        order: list[int] = []
        while q:
            u = q.popleft()
            order.append(u)
            for v in adj[u]:
                indeg[v] -= 1
                if indeg[v] == 0:
                    q.append(v)
        if len(order) != n:
            raise ValueError("DependencyDAG: cycle detected in Depends() graph")
        return [self._nodes[i] for i in order]

    def concurrent_levels(self) -> list[list[str]]:
        n = len(self._nodes)
        indeg = [0] * n
        adj: list[list[int]] = [[] for _ in range(n)]
        for a, b in self._edges:
            adj[a].append(b)
            indeg[b] += 1
        current = [i for i, d in enumerate(indeg) if d == 0]
        levels: list[list[str]] = []
        seen = 0
        while current:
            levels.append([self._nodes[i] for i in current])
            seen += len(current)
            nxt: list[int] = []
            for u in current:
                for v in adj[u]:
                    indeg[v] -= 1
                    if indeg[v] == 0:
                        nxt.append(v)
            current = nxt
        if seen != n:
            raise ValueError("DependencyDAG: cycle detected in Depends() graph")
        return levels


def make_dag():  # type: ignore[no-untyped-def]
    if HAS_RUST:
        return _RsDAG()  # type: ignore[operator]
    return PyDAGFallback()


# ---------------------------------------------------------------- Host ---

def process_rss_bytes() -> int:
    if HAS_RUST:
        try:
            return int(_rs_rss())  # type: ignore[misc]
        except Exception:
            pass
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 0


def tokio_worker_count() -> int:
    if HAS_RUST:
        try:
            return int(_rs_workers())  # type: ignore[misc]
        except Exception:
            pass
    import os

    return os.cpu_count() or 4


# ------------------------------------------------- Type-safe validators ---
# Rust-first scalar coercion. Python fallbacks mirror the exact same
# accept/reject rules so parity holds with or without the extension.

def validate_int_rs(raw: str, *, ge=None, le=None, gt=None, lt=None) -> int:  # type: ignore[no-untyped-def]
    """Strict int coercion + range checks (Rust when compiled)."""
    if HAS_RUST:
        return int(_rs_vint(raw, ge, le, gt, lt))  # type: ignore[misc]
    s = raw.strip()
    body = s.lstrip("+-")
    if not s or not body or not body.isdigit():
        raise ValueError("Input should be a valid integer, unable to parse string as an integer")
    v = int(s)
    if ge is not None and v < ge:
        raise ValueError(f"Input should be greater than or equal to {ge}")
    if le is not None and v > le:
        raise ValueError(f"Input should be less than or equal to {le}")
    if gt is not None and v <= gt:
        raise ValueError(f"Input should be greater than {gt}")
    if lt is not None and v >= lt:
        raise ValueError(f"Input should be less than {lt}")
    return v


def validate_float_rs(raw: str, *, ge=None, le=None, gt=None, lt=None) -> float:  # type: ignore[no-untyped-def]
    if HAS_RUST:
        return float(_rs_vfloat(raw, ge, le, gt, lt))  # type: ignore[misc]
    try:
        v = float(raw.strip())
    except Exception:
        raise ValueError("Input should be a valid number, unable to parse string as a number")
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError("Input should be a finite number")
    if ge is not None and v < ge:
        raise ValueError(f"Input should be greater than or equal to {ge}")
    if le is not None and v > le:
        raise ValueError(f"Input should be less than or equal to {le}")
    if gt is not None and v <= gt:
        raise ValueError(f"Input should be greater than {gt}")
    if lt is not None and v >= lt:
        raise ValueError(f"Input should be less than {lt}")
    return v


def validate_bool_rs(raw: str) -> bool:
    if HAS_RUST:
        return bool(_rs_vbool(raw))  # type: ignore[misc]
    low = raw.strip().lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off"):
        return False
    raise ValueError("Input should be a valid boolean")


def validate_str_rs(raw: str, *, min_length=None, max_length=None, pattern=None) -> str:  # type: ignore[no-untyped-def]
    if HAS_RUST:
        return str(_rs_vstr(raw, min_length, max_length, pattern))  # type: ignore[misc]
    import re as _re

    n = len(raw)
    if min_length is not None and n < min_length:
        raise ValueError(f"String should have at least {min_length} characters")
    if max_length is not None and n > max_length:
        raise ValueError(f"String should have at most {max_length} characters")
    if pattern is not None and not _re.search(pattern, raw):
        raise ValueError(f"String should match pattern {pattern!r}")
    return raw


def parse_query_pairs_rs(qs: str) -> list[tuple[str, str]]:
    if HAS_RUST:
        return list(_rs_parse_qs(qs))  # type: ignore[misc]
    from urllib.parse import parse_qsl

    return parse_qsl(qs.lstrip("?"), keep_blank_values=True)
