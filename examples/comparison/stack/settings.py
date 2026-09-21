"""Shared configuration for both comparison apps.

Pure pydantic — identical for fastapi and fasterapi (framework-agnostic).
PyYAML-backed overrides + env-style defaults keep "config" in the stack.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import BaseModel


def _load_yaml_overrides() -> dict:
    """Optional stack.yaml overrides — exercises PyYAML in the shared stack."""
    try:
        import yaml  # noqa: PLC0415

        path = os.path.join(os.path.dirname(__file__), "stack.yaml")
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


class Settings(BaseModel):
    app_name: str = "comparison-stack"
    secret_key: str = "audit-secret-key-not-for-production"
    jwt_algorithm: str = "HS256"
    token_expire_seconds: int = 3600
    items_per_page: int = 10
    users: dict[str, str] = {"alice": "wonderland", "bob": "builder"}
    rate_limit_per_second: int = 10_000  # high enough to never trip under load
    api_key: str = "audit-api-key"
    db_url: str = "sqlite+aiosqlite:///./audit.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    overrides = _load_yaml_overrides()
    return Settings(**{k: v for k, v in overrides.items() if k in Settings.model_fields})
