"""fasterapi.exception_handlers — default handlers (1:1 ``fastapi.exception_handlers``)."""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.responses import Response as Response  # noqa: F401
from starlette.websockets import WebSocket as WebSocket  # noqa: F401

from .encoders import jsonable_encoder as jsonable_encoder  # noqa: F401
from .exceptions import HTTPException as HTTPException  # noqa: F401
from .exceptions import RequestValidationError
from .exceptions import WebSocketRequestValidationError as WebSocketRequestValidationError  # noqa: F401
from .utils import is_body_allowed_for_status_code as is_body_allowed_for_status_code  # noqa: F401


async def http_exception_handler(request: Request, exc: Any) -> JSONResponse:  # type: ignore[no-untyped-def]
    return JSONResponse({"detail": getattr(exc, "detail", str(exc))},
                        status_code=getattr(exc, "status_code", 500))


async def request_validation_exception_handler(request: Request, exc: Any) -> JSONResponse:
    errors = exc.errors() if hasattr(exc, "errors") else [{"msg": str(exc)}]
    return JSONResponse({"detail": errors}, status_code=422)


async def websocket_request_validation_exception_handler(websocket: Any, exc: Any) -> None:  # type: ignore[no-untyped-def]
    await websocket.close(code=1008, reason=str(getattr(exc, "errors", lambda: str(exc))()))
