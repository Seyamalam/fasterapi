"""taskapi settings — stdlib only (no extra config dependency)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _getenv(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    app_name: str = "taskapi"
    host: str = "127.0.0.1"
    port: int = 8120
    data_dir: str = "./data"
    max_upload_bytes: int = 5 * 1024 * 1024
    rate_per_second: int = 200
    cors_origins: tuple[str, ...] = ("http://localhost:3000",)


def load_settings() -> Settings:
    return Settings(
        app_name=_getenv("TASKAPI_NAME", "taskapi"),
        host=_getenv("TASKAPI_HOST", "127.0.0.1"),
        port=int(_getenv("TASKAPI_PORT", "8120")),
        data_dir=_getenv("TASKAPI_DATA", "./data"),
        max_upload_bytes=int(_getenv("TASKAPI_MAX_UPLOAD", str(5 * 1024 * 1024))),
        rate_per_second=int(_getenv("TASKAPI_RATE", "200")),
    )


settings = load_settings()
