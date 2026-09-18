"""fasterapi.dag — AOT dependency-graph compiler.

Traverses ``Depends()`` graphs once at ``app.build()`` and compiles them
into a DAG (Rust ``DependencyDAG`` when the extension is present, otherwise
the pure-Python mirror in ``_rust_bridge``).

The compiled plan exposes:
  * ``order`` — topological evaluation order,
  * ``levels`` — Kahn levels that Tokio fans out concurrently,
  * ``rust_dag`` — the handle pushed to Rust for the Hyper host.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Optional

from . import _rust_bridge as _bridge
from .params import Depends


def _dep_name(fn: Callable[..., Any]) -> str:
    mod = getattr(fn, "__module__", "?")
    qual = getattr(fn, "__qualname__", getattr(fn, "__name__", repr(fn)))
    return f"{mod}.{qual}"


def _iter_dep_callables(fn: Callable[..., Any]) -> list[tuple[str, Depends]]:
    """Direct ``Depends`` edges declared by ``fn``'s signature."""
    out: list[tuple[str, Depends]] = []
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return out
    for pname, param in sig.parameters.items():
        default = param.default
        if isinstance(default, Depends):
            dep = default.dependency or (
                param.annotation if inspect.isfunction(param.annotation) or inspect.isclass(param.annotation) else None
            )
            if dep is not None:
                out.append((pname, Depends(dep, use_cache=default.use_cache)))
    return out


class CompiledDAG:
    def __init__(self, order: list[str], levels: list[list[str]], rust_dag: Any) -> None:
        self.order = order
        self.levels = levels
        self.rust_dag = rust_dag

    @property
    def node_count(self) -> int:
        return len(self.order)

    def __repr__(self) -> str:  # pragma: no cover
        return f"CompiledDAG(nodes={len(self.order)}, levels={len(self.levels)})"


def compile_endpoint_dag(endpoint: Callable[..., Any]) -> CompiledDAG:
    """Compile the transitive ``Depends`` closure of one endpoint."""
    dag = _bridge.make_dag()
    seen: set[str] = set()

    def visit(fn: Callable[..., Any], dependent: Optional[str]) -> None:
        name = _dep_name(fn)
        dag.add_node(name)
        if dependent is not None:
            dag.add_edge(name, dependent)
        if name in seen:
            return
        seen.add(name)
        for _pname, dep in _iter_dep_callables(fn):
            if dep.dependency is not None:
                visit(dep.dependency, name)

    root = _dep_name(endpoint)
    dag.add_node(root)
    seen.add(root)
    for _pname, dep in _iter_dep_callables(endpoint):
        if dep.dependency is not None:
            visit(dep.dependency, root)

    order = list(dag.topological_sort())
    levels = [list(lvl) for lvl in dag.concurrent_levels()]
    return CompiledDAG(order, levels, dag)


def compile_app_dag(endpoints: list[Callable[..., Any]]) -> CompiledDAG:
    """Merge per-endpoint graphs into one app-wide plan for ``app.build()``."""
    dag = _bridge.make_dag()
    seen: set[str] = set()

    def visit(fn: Callable[..., Any], dependent: Optional[str]) -> None:
        name = _dep_name(fn)
        dag.add_node(name)
        if dependent is not None:
            dag.add_edge(name, dependent)
        if name in seen:
            return
        seen.add(name)
        for _pname, dep in _iter_dep_callables(fn):
            if dep.dependency is not None:
                visit(dep.dependency, name)

    for ep in endpoints:
        root = _dep_name(ep)
        dag.add_node(root)
        if root not in seen:
            seen.add(root)
            for _pname, dep in _iter_dep_callables(ep):
                if dep.dependency is not None:
                    visit(dep.dependency, root)

    order = list(dag.topological_sort())
    levels = [list(lvl) for lvl in dag.concurrent_levels()]
    return CompiledDAG(order, levels, dag)
