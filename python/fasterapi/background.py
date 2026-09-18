"""fasterapi.background — background tasks (1:1 with ``fastapi.background``)."""

from __future__ import annotations

from typing import ParamSpec

from starlette.background import BackgroundTask as BackgroundTask  # noqa: F401
from starlette.background import BackgroundTasks as BackgroundTasks  # noqa: F401

StarletteBackgroundTasks = BackgroundTasks
P = ParamSpec("P")
