"""fasterapi.security.oauth2 — OAuth2 schemes (1:1 ``fastapi.security.oauth2``)."""

from __future__ import annotations

from starlette.requests import Request as Request  # noqa: F401

from fasterapi.exceptions import HTTPException as HTTPException  # noqa: F401
from fasterapi.params import Form as Form  # noqa: F401
from fasterapi.security import OAuth2 as OAuth2  # noqa: F401
from fasterapi.security import OAuth2AuthorizationCodeBearer as OAuth2AuthorizationCodeBearer  # noqa: F401
from fasterapi.security import OAuth2PasswordBearer as OAuth2PasswordBearer  # noqa: F401
from fasterapi.security import OAuth2PasswordRequestForm as OAuth2PasswordRequestForm  # noqa: F401
from fasterapi.security import OAuth2PasswordRequestFormStrict as OAuth2PasswordRequestFormStrict  # noqa: F401
from fasterapi.security import SecurityBase as SecurityBase  # noqa: F401
from fasterapi.security import SecurityScopes as SecurityScopes  # noqa: F401
from fasterapi.security import get_authorization_scheme_param as get_authorization_scheme_param  # noqa: F401
