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
    from _fasterapi_rs import (  # type: ignore
        DependencyDAG as _RsDAG,
        RustRouter as _RsRouter,
        TokenBucket as _RsBucket,
        fast_json_dumps as _rs_dumps,
        fast_json_loads as _rs_loads,
        has_simd as _rs_has_simd,
        hyper_health as _rs_hyper_health,
        process_rss_bytes as _rs_rss,
        serve_static_config as _rs_static_cfg,
        tokio_worker_count as _rs_workers,
    )

    HAS_RUST = True
except Exception:  # pure-python fallback
    HAS_RUST = False
    _RsRouter = None  # type: ignore
    _RsBucket = None  # type: ignore
    _RsDAG = None  # type: ignore
    _rs_loads = None  # type: ignore
    _rs_dumps = None  # type: ignore


# ---------------------------------------------------------------- JSON ---

def fast_json_loads(payload: str) -> Any:
    """Parse JSON, SIMD path in Rust when available."""
    if HAS_RUST:
        return _rs_loads(payload)  # type: ignore[misc]
    return json.loads(payload)


def fast_json_dumps(obj: Any) -> str:
    """Serialize JSON, Rust path when available."""
    if HAS_RUST:
        return _rs_dumps(obj)  # type: ignore[misc]
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


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
