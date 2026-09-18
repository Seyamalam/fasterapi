"""fasterapi.concurrency — threadpool helpers (1:1 ``fastapi.concurrency``)."""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool as run_in_threadpool  # noqa: F401
