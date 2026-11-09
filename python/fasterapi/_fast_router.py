"""fasterapi._fast_router — Starlette ``Router`` with a radix-tree fast path.

``Router.app`` scans ``self.routes`` linearly, running each route's regex
until a FULL match. This subclass compiles the plain ``Route`` entries into
the Rust radix tree (``matchit`` via ``_rust_bridge``) and answers the common
case — one unambiguous FULL match — without a Python-side scan.

Anything the tree cannot prove identical to Starlette's
order-of-registration semantics falls back to ``super().app`` (the original
linear scan), which also handles partial matches (405), Mount/Host entries,
websocket routes, trailing-slash redirects and the default responder:

- a route whose pattern matchit's winner could shadow out of registration
  order (e.g. ``/{a}/y`` before ``/x/{b}``) is marked ambiguous → linear;
- a hit index sitting after a skipped entry (Mount/Host/convertor pattern)
  could be shadowed by it → linear;
- duplicate same-shape patterns resolve through an ordered candidate list,
  and a method mismatch on every candidate → linear.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from starlette.routing import Route as StarletteRoute
from starlette.routing import Router, get_route_path
from starlette.types import Receive, Scope, Send

from . import _rust_bridge as _bridge

# Starlette convertor syntax `{p:int}` converts params to non-str types; the
# radix tree only yields strings, so those patterns stay on the linear path.
_CONVERTOR_RE = re.compile(r"\{[^}/]*:")


def _seg_flags(pattern: str) -> list[bool]:
    """Per-segment ``is {param}`` flags for a route path."""
    return [s.startswith("{") and s.endswith("}") and len(s) > 2
            for s in pattern.split("/")]


def _co_matchable(a_flags: list[bool], a_segs: list[str],
                  b_flags: list[bool], b_segs: list[str]) -> bool:
    """True when some concrete path could match both patterns."""
    if len(a_segs) != len(b_segs):
        return False
    return all(
        af or bf or a_seg == b_seg
        for af, bf, a_seg, b_seg in zip(a_flags, b_flags, a_segs, b_segs)
    )


class FastRouter(Router):
    """Drop-in ``Router`` that dispatches unambiguous FULL hits via matchit."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._fr_router: Any = None
        self._fr_routes: list[Any] = []
        self._fr_multi: dict[int, list[int]] = {}
        self._fr_ambiguous: set[int] = set()
        self._fr_first_skip: int = 1 << 62
        self.compile_fast_routes()

    # -- compilation ----------------------------------------------------------
    def compile_fast_routes(self) -> None:
        """Rebuild the radix tree from ``self.routes`` (idempotent)."""
        self._fr_routes = list(self.routes)
        self._fr_router = None
        self._fr_multi = {}
        self._fr_ambiguous = set()
        self._fr_first_skip = 1 << 62
        router = _bridge.make_router()
        if router is None:
            return
        inserted: list[tuple[int, list[str], list[bool]]] = []
        skeleton_owner: dict[tuple[str, ...], int] = {}
        for i, route in enumerate(self.routes):
            if type(route) is not StarletteRoute or _CONVERTOR_RE.search(route.path):
                self._fr_first_skip = min(self._fr_first_skip, i)
                continue
            segs = route.path.split("/")
            flags = _seg_flags(route.path)
            try:
                router.insert(route.path, str(i))
            except Exception:
                # matchit shape conflict: an earlier pattern has the same
                # literal skeleton (differs only in {param} names). Both match
                # the identical path set — order resolves via the multi list.
                owner = skeleton_owner.get(tuple(flags))
                if owner is None:
                    # Find the earliest inserted entry with this skeleton.
                    for j, psegs, pflags in inserted:
                        if pflags == flags and all(
                            pf or ps == s for ps, pf, s in zip(psegs, pflags, segs)
                        ):
                            owner = j
                            break
                if owner is not None:
                    self._fr_multi.setdefault(owner, []).append(i)
                    skeleton_owner[tuple(flags)] = owner
                    continue
                self._fr_first_skip = min(self._fr_first_skip, i)
                continue
            skeleton_owner.setdefault(tuple(flags), i)
            # Ambiguity: for each earlier pattern that can co-match a concrete
            # path, matchit prefers whichever has the literal at the FIRST
            # segment where literal-vs-param differs; Starlette prefers the
            # earlier registration. They disagree iff at that position the
            # earlier pattern is the param and this one is the literal.
            for _j, ese_gs, eflags in inserted:
                if not _co_matchable(eflags, ese_gs, flags, segs):
                    continue
                for eflag, pflag in zip(eflags, flags):
                    if eflag != pflag:
                        if eflag:  # earlier=param, new=literal → new shadows
                            self._fr_ambiguous.add(i)
                        break
            inserted.append((i, segs, flags))
        self._fr_router = router

    def _ensure_compiled(self) -> None:
        """Recompile if ``self.routes`` changed outside a resync."""
        snap = self._fr_routes
        routes = self.routes
        if len(routes) != len(snap) or any(a is not b for a, b in zip(routes, snap)):
            self.compile_fast_routes()

    # -- dispatch ---------------------------------------------------------------
    async def app(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self._fr_router is not None:
            self._ensure_compiled()
            if self._fr_router is not None:
                hit = self._fr_router.match(get_route_path(scope))
                if hit is not None:
                    idx, params = hit
                    idx = int(idx)
                    if idx not in self._fr_ambiguous and idx < self._fr_first_skip:
                        candidates = [idx, *self._fr_multi.get(idx, ())]
                        method = scope["method"]
                        for j in candidates:
                            route = self.routes[j]
                            if not route.methods or method in route.methods:
                                scope["route"] = route
                                scope.update(
                                    endpoint=route.endpoint,
                                    path_params={
                                        **scope.get("path_params", {}),
                                        **params,
                                    },
                                )
                                await route.handle(scope, receive, send)
                                return
        await super().app(scope, receive, send)
