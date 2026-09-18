"""fasterapi.encoders — JSON-compatible encoding (1:1 ``fastapi.encoders``).

``jsonable_encoder`` converts Pydantic models, dataclasses, datetimes,
UUIDs, enums, bytes, Decimals, paths … into plain JSON-native structures.
Pydantic v2 does the heavy lifting in Rust (`mode="json"`); the Python layer
only dispatches containers so behavior matches FastAPI exactly.
"""

from __future__ import annotations

import dataclasses
from collections import deque
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import PurePath
from types import GeneratorType
from typing import Any
from uuid import UUID

from pydantic import BaseModel, TypeAdapter

_ANY_ADAPTER = TypeAdapter(Any)


def jsonable_encoder(obj: Any) -> Any:
    """Encode ``obj`` into JSON-native types (mirror of FastAPI's encoder)."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {jsonable_encoder(k): jsonable_encoder(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset, GeneratorType, deque)):
        return [jsonable_encoder(v) for v in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: jsonable_encoder(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Enum):
        return jsonable_encoder(obj.value)
    if isinstance(obj, (datetime, date, time, timedelta)):
        return _ANY_ADAPTER.dump_python(obj, mode="json")
    if isinstance(obj, (UUID, Decimal, PurePath)):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode()
    try:
        return _ANY_ADAPTER.dump_python(obj, mode="json")
    except Exception:
        return obj
