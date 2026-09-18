"""fasterapi.exceptions — 1:1 FastAPI exception surface.

Mirrors ``fastapi.exceptions``: the ``ValidationException`` hierarchy with
endpoint context, ``FastAPIError`` family, warning type and the tiny
request/websocket error models.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Optional, TypedDict

from pydantic import BaseModel as BaseModel  # noqa: F401
from pydantic import create_model
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: F401
from starlette.exceptions import WebSocketException as StarletteWebSocketException  # noqa: F401


class EndpointContext(TypedDict, total=False):
    function: str
    path: str
    file: str
    line: int


class HTTPException(Exception):
    def __init__(
        self,
        status_code: int,
        detail: Any = None,
        headers: Optional[dict[str, str]] = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.headers = headers

    def __repr__(self) -> str:
        return f"HTTPException(status_code={self.status_code}, detail={self.detail!r})"


class WebSocketException(Exception):
    def __init__(self, code: int, reason: Optional[str] = None) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class FastAPIError(RuntimeError):
    pass


class DependencyScopeError(FastAPIError):
    pass


class PydanticV1NotSupportedError(FastAPIError):
    pass


class ValidationException(Exception):
    """Base validation error with optional endpoint context (mirrors FastAPI)."""

    def __init__(
        self,
        errors: Sequence[Any],
        *,
        endpoint_ctx: Optional[EndpointContext] = None,
    ) -> None:
        super().__init__(errors)
        self._errors = list(errors)
        self.endpoint_ctx = endpoint_ctx

        ctx: Mapping[str, Any] = endpoint_ctx or {}
        self.endpoint_function = ctx.get("function")
        self.endpoint_path = ctx.get("path")
        self.endpoint_file = ctx.get("file")
        self.endpoint_line = ctx.get("line")

    def errors(self) -> Sequence[Any]:
        return self._errors


class RequestValidationError(ValidationException):
    """Mirrors ``fastapi.exceptions.RequestValidationError``.

    ``errors()`` returns Pydantic-style ``list[dict]`` with ``loc``/``msg``/
    ``type`` so differential parity tests can compare ``.errors()`` 1:1.
    """

    def __init__(
        self,
        errors: Sequence[Any],
        *,
        body: Any = None,
        endpoint_ctx: Optional[EndpointContext] = None,
    ) -> None:
        super().__init__(errors, endpoint_ctx=endpoint_ctx)
        self.body = body

    def __str__(self) -> str:
        return str(self._errors)


class ResponseValidationError(ValidationException):
    def __init__(
        self,
        errors: Sequence[Any],
        *,
        body: Any = None,
        endpoint_ctx: Optional[EndpointContext] = None,
    ) -> None:
        super().__init__(errors, endpoint_ctx=endpoint_ctx)
        self.body = body


class WebSocketRequestValidationError(ValidationException):
    """Validation error on a websocket handshake (mirrors FastAPI)."""

    def __init__(
        self,
        errors: Sequence[Any],
        *,
        body: Any = None,
        endpoint_ctx: Optional[EndpointContext] = None,
    ) -> None:
        super().__init__(errors, endpoint_ctx=endpoint_ctx)
        self.body = body


RequestErrorModel: type[BaseModel] = create_model("Request")
WebSocketErrorModel: type[BaseModel] = create_model("WebSocket")


class FastAPIDeprecationWarning(UserWarning):
    pass
