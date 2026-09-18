"""fasterapi.dependencies — DI re-exports (1:1 ``fastapi.dependencies``)."""

from __future__ import annotations

from .params import Depends as Depends  # noqa: F401
from .params import Security as Security  # noqa: F401
