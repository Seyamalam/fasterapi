"""fasterapi.sse — Server-Sent Events (1:1 ``fastapi.sse``).

`response_class=EventSourceResponse` on a generator endpoint streams
`text/event-stream` bytes with identical wire encoding: `ServerSentEvent`
items control the frame, plain objects are JSON-encoded into `data:`.
"""

from __future__ import annotations

from typing import Annotated, Any, Optional

from pydantic import AfterValidator, BaseModel, Field
from pydantic import model_validator as model_validator  # noqa: F401 (surface parity)
from starlette.responses import StreamingResponse


class EventSourceResponse(StreamingResponse):
    """Streaming response with `text/event-stream` media type.

    Use as `response_class=EventSourceResponse` on a *path operation* that
    uses `yield` to enable Server Sent Events (SSE) responses.
    """

    media_type = "text/event-stream"


def _check_single_line(v: Optional[str], field_name: str) -> Optional[str]:
    if v is not None and ("\r" in v or "\n" in v):
        raise ValueError(f"SSE '{field_name}' must be a single line")
    return v


def _check_event_single_line(v: Optional[str]) -> Optional[str]:
    return _check_single_line(v, "event")


def _check_id_valid(v: Optional[str]) -> Optional[str]:
    if v is not None and "\0" in v:
        raise ValueError("SSE 'id' must not contain null characters")
    return _check_single_line(v, "id")


class ServerSentEvent(BaseModel):
    """A single Server-Sent Event.

    `data` is always JSON-serialized (`"hello"` becomes `data: "hello"`);
    `raw_data` is placed verbatim and is mutually exclusive with `data`.
    """

    data: Any = None
    raw_data: Optional[str] = None
    event: Annotated[Optional[str], AfterValidator(_check_event_single_line)] = None
    id: Annotated[Optional[str], AfterValidator(_check_id_valid)] = None
    retry: Optional[int] = Field(default=None, ge=0)
    comment: Optional[str] = None

    @classmethod
    def _check_data_exclusive(cls, values: Any) -> Any:  # pragma: no cover - wired below
        return values

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        if self.data is not None and self.raw_data is not None:
            raise ValueError(
                "Cannot set both 'data' and 'raw_data' on the same "
                "ServerSentEvent. Use 'data' for JSON-serialized payloads "
                "or 'raw_data' for pre-formatted strings."
            )


def _split_sse_lines(value: str) -> list[str]:
    # Split on SSE-spec line terminators only (\n, \r\n, \r), preserving
    # trailing empty strings.
    return value.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def format_sse_event(
    *,
    data_str: Optional[str] = None,
    event: Optional[str] = None,
    id: Optional[str] = None,
    retry: Optional[int] = None,
    comment: Optional[str] = None,
) -> bytes:
    """Build SSE wire-format bytes from **pre-serialized** data.

    The result always ends with `\\n\\n` (the event terminator).
    """
    lines: list[str] = []

    if comment is not None:
        for line in _split_sse_lines(comment):
            lines.append(f": {line}")

    if event is not None:
        lines.append(f"event: {event}")

    if data_str is not None:
        for line in _split_sse_lines(data_str):
            lines.append(f"data: {line}")

    if id is not None:
        lines.append(f"id: {id}")

    if retry is not None:
        lines.append(f"retry: {retry}")

    lines.append("")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


# Keep-alive comment, per the SSE spec recommendation
KEEPALIVE_COMMENT = b": ping\n\n"

# Seconds between keep-alive pings when a generator is idle.
# Private but importable so tests can monkeypatch it.
_PING_INTERVAL: float = 15.0
