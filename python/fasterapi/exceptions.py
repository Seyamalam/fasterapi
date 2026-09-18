"""fasterapi.exceptions — 1:1 FastAPI exception surface."""

from __future__ import annotations

from typing import Any, Optional


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


class RequestValidationError(Exception):
    """Mirrors ``fastapi.exceptions.RequestValidationError``.

    ``errors()`` returns Pydantic-style ``list[dict]`` with ``loc``/``msg``/
    ``type`` so differential parity tests can compare ``.errors()`` 1:1.
    """

    def __init__(self, errors: list[dict[str, Any]], *, body: Any = None) -> None:
        super().__init__(errors)
        self._errors = errors
        self.body = body

    def errors(self) -> list[dict[str, Any]]:
        return self._errors

    def __str__(self) -> str:
        return str(self._errors)


class ResponseValidationError(Exception):
    def __init__(self, errors: list[dict[str, Any]], *, body: Any = None) -> None:
        super().__init__(errors)
        self._errors = errors
        self.body = body

    def errors(self) -> list[dict[str, Any]]:
        return self._errors
