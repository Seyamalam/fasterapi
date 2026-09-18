"""fasterapi.security — auth schemes (1:1 with ``fastapi.security``).

OAuth2 (password + authorization-code), OpenID Connect, HTTP
(Basic/Bearer/Digest) and API-key (header/query/cookie) dependencies with
identical wire behavior: 401 + ``{"detail": "Not authenticated"}`` +
per-scheme ``WWW-Authenticate`` when `auto_error` fires, credential objects
otherwise. OpenAPI scheme metadata (`scheme_name`, `description`) is stored
for the docs generator; enforcement beyond presence/format matches FastAPI
(scope checks ride on user auth middleware via `SecurityScopes`).
"""

from __future__ import annotations

import base64
import binascii
from typing import Any, Optional

from pydantic import BaseModel
from starlette.requests import Request

from ..exceptions import HTTPException
from ..params import Form


def get_authorization_scheme_param(authorization_header_value: Optional[str]) -> tuple[str, str]:
    """Split `"<scheme> <credentials>"` (mirror of FastAPI's helper)."""
    if not authorization_header_value:
        return "", ""
    scheme, _, param = authorization_header_value.partition(" ")
    return scheme, param.strip()


# ------------------------------------------------------------ base ---

class SecurityBase:
    def __init__(self, *, scheme_name: Optional[str] = None, description: Optional[str] = None) -> None:
        self.scheme_name = scheme_name or self.__class__.__name__
        self.description = description


class SecurityScopes:
    """Scopes declared by the app; populated from user auth middleware.

    Without authentication middleware the scope list is empty — exactly like
    FastAPI — so presence/format checks still behave 1:1.
    """

    def __init__(self, scopes: Optional[list[str]] = None) -> None:
        self.scopes: list[str] = list(scopes or [])

    def __repr__(self) -> str:  # pragma: no cover
        return f"SecurityScopes(scopes={self.scopes})"


# ------------------------------------------------------------ OAuth2 ---

class OAuth2(SecurityBase):
    def __init__(
        self,
        *,
        flows: Optional[dict[str, Any]] = None,
        scheme_name: Optional[str] = None,
        description: Optional[str] = None,
        auto_error: bool = True,
    ) -> None:
        super().__init__(scheme_name=scheme_name, description=description)
        self.flows = flows or {}
        self.auto_error = auto_error

    async def __call__(self, request: Request) -> Optional[str]:
        authorization = request.headers.get("Authorization")
        scheme, param = get_authorization_scheme_param(authorization)
        if not authorization or scheme.lower() != "bearer":
            if self.auto_error:
                raise HTTPException(
                    status_code=401,
                    detail="Not authenticated",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return None
        return param


class OAuth2PasswordBearer(OAuth2):
    def __init__(
        self,
        tokenUrl: str,
        *,
        scheme_name: Optional[str] = None,
        scopes: Optional[dict[str, str]] = None,
        description: Optional[str] = None,
        auto_error: bool = True,
        refreshUrl: Optional[str] = None,
    ) -> None:
        self.tokenUrl = tokenUrl
        self.refreshUrl = refreshUrl
        flows = {"password": {"tokenUrl": tokenUrl, "refreshUrl": refreshUrl, "scopes": scopes or {}}}
        super().__init__(flows=flows, scheme_name=scheme_name, description=description, auto_error=auto_error)


class OAuth2AuthorizationCodeBearer(OAuth2):
    def __init__(
        self,
        authorizationUrl: str,
        tokenUrl: str,
        *,
        refreshUrl: Optional[str] = None,
        scheme_name: Optional[str] = None,
        scopes: Optional[dict[str, str]] = None,
        description: Optional[str] = None,
        auto_error: bool = True,
    ) -> None:
        self.authorizationUrl = authorizationUrl
        self.tokenUrl = tokenUrl
        self.refreshUrl = refreshUrl
        flows = {
            "authorizationCode": {
                "authorizationUrl": authorizationUrl,
                "tokenUrl": tokenUrl,
                "refreshUrl": refreshUrl,
                "scopes": scopes or {},
            }
        }
        super().__init__(flows=flows, scheme_name=scheme_name, description=description, auto_error=auto_error)


class OpenIdConnect(SecurityBase):
    def __init__(
        self,
        *,
        openIdConnectUrl: str,
        scheme_name: Optional[str] = None,
        description: Optional[str] = None,
        auto_error: bool = True,
    ) -> None:
        super().__init__(scheme_name=scheme_name, description=description)
        self.openIdConnectUrl = openIdConnectUrl
        self.auto_error = auto_error

    async def __call__(self, request: Request) -> Optional[str]:
        # OpenID Connect returns the whole Authorization header (FastAPI parity).
        authorization = request.headers.get("Authorization")
        scheme, _param = get_authorization_scheme_param(authorization)
        if not authorization or scheme.lower() != "bearer":
            if self.auto_error:
                raise HTTPException(
                    status_code=401,
                    detail="Not authenticated",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return None
        return authorization


class OAuth2PasswordRequestForm:
    """Form dependency collecting OAuth2 password-flow fields."""

    def __init__(
        self,
        *,
        grant_type: Optional[str] = Form(default=None, pattern="^password$"),
        username: str = Form(),
        password: str = Form(),
        scope: str = Form(default=""),
        client_id: Optional[str] = Form(default=None),
        client_secret: Optional[str] = Form(default=None),
    ) -> None:
        self.grant_type = grant_type
        self.username = username
        self.password = password
        self.scopes = scope.split()
        self.client_id = client_id
        self.client_secret = client_secret


class OAuth2PasswordRequestFormStrict(OAuth2PasswordRequestForm):
    """Strict variant: `grant_type` must be `"password"` (422 otherwise)."""

    def __init__(
        self,
        *,
        grant_type: str = Form(pattern="^password$"),
        username: str = Form(),
        password: str = Form(),
        scope: str = Form(default=""),
        client_id: Optional[str] = Form(default=None),
        client_secret: Optional[str] = Form(default=None),
    ) -> None:
        super().__init__(
            grant_type=grant_type,
            username=username,
            password=password,
            scope=scope,
            client_id=client_id,
            client_secret=client_secret,
        )


# ------------------------------------------------------------ HTTP ---

class HTTPBasicCredentials(BaseModel):
    username: str
    password: str


class HTTPAuthorizationCredentials(BaseModel):
    scheme: str
    credentials: str


class HTTPBase(SecurityBase):
    def __init__(
        self,
        *,
        scheme: Optional[str] = None,
        scheme_name: Optional[str] = None,
        description: Optional[str] = None,
        auto_error: bool = True,
    ) -> None:
        super().__init__(scheme_name=scheme_name, description=description)
        self.scheme = scheme
        self.auto_error = auto_error

    def make_authenticate_headers(self) -> dict[str, str]:
        return {"WWW-Authenticate": f"{self.scheme.title()}"} if self.scheme else {"WWW-Authenticate": "Bearer"}

    def make_not_authenticated_error(self) -> HTTPException:
        return HTTPException(status_code=401, detail="Not authenticated",
                             headers=self.make_authenticate_headers())


class HTTPBasic(HTTPBase):
    def __init__(
        self,
        *,
        scheme_name: Optional[str] = None,
        realm: Optional[str] = None,
        description: Optional[str] = None,
        auto_error: bool = True,
    ) -> None:
        super().__init__(scheme="basic", scheme_name=scheme_name, description=description, auto_error=auto_error)
        self.realm = realm

    def make_authenticate_headers(self) -> dict[str, str]:
        if self.realm:
            return {"WWW-Authenticate": f'Basic realm="{self.realm}"'}
        return {"WWW-Authenticate": "Basic"}

    async def __call__(self, request: Request) -> Optional[HTTPBasicCredentials]:
        authorization = request.headers.get("Authorization")
        scheme, param = get_authorization_scheme_param(authorization)
        if not authorization or scheme.lower() != "basic":
            if self.auto_error:
                raise self.make_not_authenticated_error()
            return None
        try:
            data = base64.b64decode(param).decode("ascii")
        except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
            raise self.make_not_authenticated_error() from exc
        username, separator, password = data.partition(":")
        if not separator:
            raise self.make_not_authenticated_error()
        return HTTPBasicCredentials(username=username, password=password)


class HTTPBearer(HTTPBase):
    def __init__(
        self,
        *,
        bearerFormat: Optional[str] = None,
        scheme_name: Optional[str] = None,
        description: Optional[str] = None,
        auto_error: bool = True,
    ) -> None:
        super().__init__(scheme="bearer", scheme_name=scheme_name, description=description, auto_error=auto_error)
        self.bearerFormat = bearerFormat

    async def __call__(self, request: Request) -> Optional[HTTPAuthorizationCredentials]:
        authorization = request.headers.get("Authorization")
        scheme, credentials = get_authorization_scheme_param(authorization)
        if not (authorization and scheme and credentials):
            if self.auto_error:
                raise self.make_not_authenticated_error()
            return None
        if scheme.lower() != "bearer":
            if self.auto_error:
                raise self.make_not_authenticated_error()
            return None
        return HTTPAuthorizationCredentials(scheme=scheme, credentials=credentials)


class HTTPDigest(HTTPBase):
    """Digest *scheme* check (full digest negotiation needs a subclass)."""

    def __init__(
        self,
        *,
        scheme_name: Optional[str] = None,
        description: Optional[str] = None,
        auto_error: bool = True,
    ) -> None:
        super().__init__(scheme="digest", scheme_name=scheme_name, description=description, auto_error=auto_error)

    async def __call__(self, request: Request) -> Optional[HTTPAuthorizationCredentials]:
        authorization = request.headers.get("Authorization")
        scheme, credentials = get_authorization_scheme_param(authorization)
        if not (authorization and scheme and credentials):
            if self.auto_error:
                raise self.make_not_authenticated_error()
            return None
        if scheme.lower() != "digest":
            if self.auto_error:
                raise self.make_not_authenticated_error()
            return None
        return HTTPAuthorizationCredentials(scheme=scheme, credentials=credentials)


# ------------------------------------------------------------ API key ---

class APIKeyBase(SecurityBase):
    openapi_in: str = "header"

    def __init__(
        self,
        *,
        name: str,
        scheme_name: Optional[str] = None,
        description: Optional[str] = None,
        auto_error: bool = True,
    ) -> None:
        super().__init__(scheme_name=scheme_name, description=description)
        self.name = name
        self.auto_error = auto_error

    def make_not_authenticated_error(self) -> HTTPException:
        return HTTPException(status_code=401, detail="Not authenticated",
                             headers={"WWW-Authenticate": "APIKey"})

    def check_api_key(self, api_key: Optional[str]) -> Optional[str]:
        if not api_key:
            if self.auto_error:
                raise self.make_not_authenticated_error()
            return None
        return api_key


class APIKeyQuery(APIKeyBase):
    openapi_in: str = "query"

    async def __call__(self, request: Request) -> Optional[str]:
        return self.check_api_key(request.query_params.get(self.name))


class APIKeyHeader(APIKeyBase):
    openapi_in: str = "header"

    async def __call__(self, request: Request) -> Optional[str]:
        return self.check_api_key(request.headers.get(self.name))


class APIKeyCookie(APIKeyBase):
    openapi_in: str = "cookie"

    async def __call__(self, request: Request) -> Optional[str]:
        return self.check_api_key(request.cookies.get(self.name))


# Submodule attributes (mirror `fastapi.security.*` import paths). Kept at
# the end: submodules import back from this fully-initialized package.
from . import api_key as api_key  # noqa: F401,E402
from . import base as base  # noqa: F401,E402
from . import http as http  # noqa: F401,E402
from . import oauth2 as oauth2  # noqa: F401,E402
from . import open_id_connect_url as open_id_connect_url  # noqa: F401,E402
from . import utils as utils  # noqa: F401,E402
