"""fasterapi.security.open_id_connect_url — OIDC scheme (1:1 upstream path)."""

from __future__ import annotations

from starlette.requests import Request as Request  # noqa: F401

from fasterapi.exceptions import HTTPException as HTTPException  # noqa: F401
from fasterapi.security import OpenIdConnect as OpenIdConnect  # noqa: F401
from fasterapi.security import SecurityBase as SecurityBase  # noqa: F401
