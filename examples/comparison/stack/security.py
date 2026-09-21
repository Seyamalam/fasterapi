"""Shared JWT auth helpers (PyJWT) + API-key check — framework-agnostic."""

from __future__ import annotations

import time
from typing import Any

import jwt

from .settings import get_settings


def create_token(username: str) -> str:
    s = get_settings()
    now = int(time.time())
    payload = {"sub": username, "iat": now, "exp": now + s.token_expire_seconds}
    return jwt.encode(payload, s.secret_key, algorithm=s.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """Raises jwt.PyJWTError subclasses on bad/expired tokens."""
    s = get_settings()
    return jwt.decode(token, s.secret_key, algorithms=[s.jwt_algorithm])


def check_api_key(key: str | None) -> bool:
    return bool(key) and key == get_settings().api_key
