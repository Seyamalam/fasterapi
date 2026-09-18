"""fasterapi.types — shared typing helpers (1:1 ``fastapi.types``)."""

from __future__ import annotations

import types as types  # noqa: F401 (surface parity with upstream)
from typing import Any, Callable, TypeVar

from pydantic import BaseModel as BaseModel  # noqa: F401

DecoratedCallable = TypeVar("DecoratedCallable", bound=Callable[..., Any])
