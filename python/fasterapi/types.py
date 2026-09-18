"""fasterapi.types — shared typing helpers (1:1 ``fastapi.types``)."""

from __future__ import annotations

from typing import Any, Callable, TypeVar

DecoratedCallable = TypeVar("DecoratedCallable", bound=Callable[..., Any])
