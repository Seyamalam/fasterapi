"""taskapi auth — demo-grade credentials (stdlib hashing, opaque tokens).

NOT production auth: passwords use PBKDF2-SHA256 with per-user salts and
constant-time comparison, but tokens are random server-side strings. Swap
`issue_token`/`token_owner` for JWT (or sessions) without touching routers.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

_ITERATIONS = 120_000


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITERATIONS)
    return digest.hex(), salt


def verify_password(password: str, digest: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, digest)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def data_dir() -> str:
    path = os.environ.get("TASKAPI_DATA", "./data")
    os.makedirs(path, exist_ok=True)
    return path
