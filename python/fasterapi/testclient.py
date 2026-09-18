"""fasterapi.testclient — drop-in ``fastapi.testclient.TestClient``."""

from __future__ import annotations

from typing import Any

from starlette.testclient import TestClient as _StarletteTestClient


class TestClient(_StarletteTestClient):
    """HTTPX-based test client with ``fastapi.testclient`` semantics.

    Accepts the same constructor (``app``, ``base_url``, ``raise_server_exceptions``,
    ``root_path``, ``backend``, ``cookies``, ``headers`` …) and exposes
    ``get/post/put/delete/patch/options/head`` plus ``websocket_connect``.
    """

    def __init__(self, app: Any, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("raise_server_exceptions", True)
        super().__init__(app, *args, **kwargs)  # type: ignore[arg-type]
