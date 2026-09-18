"""fasterapi.security.api_key — API-key schemes (1:1 ``fastapi.security.api_key``)."""

from __future__ import annotations

from starlette.requests import Request as Request  # noqa: F401

from fasterapi.exceptions import HTTPException as HTTPException  # noqa: F401
from fasterapi.security import APIKeyBase as APIKeyBase  # noqa: F401
from fasterapi.security import APIKeyCookie as APIKeyCookie  # noqa: F401
from fasterapi.security import APIKeyHeader as APIKeyHeader  # noqa: F401
from fasterapi.security import APIKeyQuery as APIKeyQuery  # noqa: F401
from fasterapi.security import SecurityBase as SecurityBase  # noqa: F401
