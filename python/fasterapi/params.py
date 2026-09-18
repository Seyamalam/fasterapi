"""fasterapi.params — 1:1 FastAPI parameter extractors.

``Path()``, ``Query()``, ``Header()``, ``Cookie()``, ``Body()``,
``Form()``, ``File()`` and ``Depends()`` share one ``Param`` carrier so
``inspect.signature`` introspection in ``routing.py`` behaves exactly like
FastAPI (default value carries the metadata).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

_UNSET: Any = object()


@dataclass
class Param:
    in_: str  # "path" | "query" | "header" | "cookie" | "body" | "form" | "file"
    default: Any = _UNSET
    alias: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    ge: Optional[float] = None
    le: Optional[float] = None
    gt: Optional[float] = None
    lt: Optional[float] = None
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    pattern: Optional[str] = None
    embed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def has_default(self) -> bool:
        return self.default is not _UNSET

    @property
    def required(self) -> bool:
        return self.default is Ellipsis or self.default is _UNSET


def Path(  # noqa: N802
    default: Any = ...,
    *,
    alias: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    ge: Optional[float] = None,
    le: Optional[float] = None,
    gt: Optional[float] = None,
    lt: Optional[float] = None,
    min_length: Optional[int] = None,
    max_length: Optional[int] = None,
    pattern: Optional[str] = None,
    **extra: Any,
) -> Any:
    return Param("path", default, alias, title, description, ge, le, gt, lt,
                 min_length, max_length, pattern, False, extra)


def Query(  # noqa: N802
    default: Any = _UNSET,
    *,
    alias: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    ge: Optional[float] = None,
    le: Optional[float] = None,
    gt: Optional[float] = None,
    lt: Optional[float] = None,
    min_length: Optional[int] = None,
    max_length: Optional[int] = None,
    pattern: Optional[str] = None,
    **extra: Any,
) -> Any:
    return Param("query", default, alias, title, description, ge, le, gt, lt,
                 min_length, max_length, pattern, False, extra)


def Header(  # noqa: N802
    default: Any = _UNSET,
    *,
    alias: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    min_length: Optional[int] = None,
    max_length: Optional[int] = None,
    pattern: Optional[str] = None,
    **extra: Any,
) -> Any:
    return Param("header", default, alias, title, description, None, None, None,
                 None, min_length, max_length, pattern, False, extra)


def Cookie(  # noqa: N802
    default: Any = _UNSET,
    *,
    alias: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    **extra: Any,
) -> Any:
    return Param("cookie", default, alias, title, description, None, None, None,
                 None, None, None, None, False, extra)


def Body(  # noqa: N802
    default: Any = _UNSET,
    *,
    alias: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    embed: bool = False,
    **extra: Any,
) -> Any:
    return Param("body", default, alias, title, description, None, None, None,
                 None, None, None, None, embed, extra)


def Form(  # noqa: N802
    default: Any = _UNSET,
    *,
    alias: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    min_length: Optional[int] = None,
    max_length: Optional[int] = None,
    pattern: Optional[str] = None,
    **extra: Any,
) -> Any:
    return Param("form", default, alias, title, description, None, None, None,
                 None, min_length, max_length, pattern, False, extra)


def File(  # noqa: N802
    default: Any = _UNSET,
    *,
    alias: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    **extra: Any,
) -> Any:
    return Param("file", default, alias, title, description, None, None, None,
                 None, None, None, None, False, extra)


class Depends:
    """Declare a dependency — mirrors ``fastapi.Depends``.

    ``dependency`` may be sync or async, a plain callable or a generator
    (``yield`` teardown supported). ``use_cache=True`` (default) memoizes
    per-request; sub-dependencies are resolved recursively.
    """

    def __init__(self, dependency: Optional[Callable[..., Any]] = None, *, use_cache: bool = True) -> None:
        self.dependency = dependency
        self.use_cache = use_cache

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        name = getattr(self.dependency, "__name__", self.dependency)
        return f"Depends({name}, use_cache={self.use_cache})"


# Re-exported sentinel for routing introspection.
UNSET = _UNSET
