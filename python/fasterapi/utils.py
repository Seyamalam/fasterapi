"""fasterapi.utils — routing/OpenAPI helpers (1:1 ``fastapi.utils`` subset).

Behavior-identical pure helpers. Pydantic-v1 compatibility internals
(`ModelField`, `create_model_field`, `DefaultPlaceholder`, …) are
intentionally absent — Pydantic v2 only, same as supported FastAPI.
"""

from __future__ import annotations

import re
import warnings
from typing import Any

from .exceptions import FastAPIDeprecationWarning as FastAPIDeprecationWarning  # noqa: F401
from pydantic.fields import FieldInfo as FieldInfo  # noqa: F401


def is_body_allowed_for_status_code(status_code: int | str | None) -> bool:
    if status_code is None:
        return True
    # Ref: https://github.com/OAI/OpenAPI-Specification/blob/main/versions/3.1.0.md#patterned-fields-1
    if status_code in {"default", "1XX", "2XX", "3XX", "4XX", "5XX"}:
        return True
    try:
        code = int(status_code)
    except (TypeError, ValueError):
        return True
    return not (100 <= code < 200 or code in (204, 304))


def get_path_param_names(path: str) -> set[str]:
    return set(re.findall("{(.*?)}", path))


def generate_unique_id(route: Any) -> str:
    path_format = getattr(route, "path_format", None) or getattr(route, "path", "")
    operation_id = f"{route.name}{path_format}"
    operation_id = re.sub(r"\W", "_", operation_id)
    methods = getattr(route, "methods", ["GET"]) or ["GET"]
    operation_id = f"{operation_id}_{list(methods)[0].lower()}"
    return operation_id


def generate_operation_id_for_path(*, name: str, path: str, method: str) -> str:  # pragma: nocover
    warnings.warn(
        message="fasterapi.utils.generate_operation_id_for_path() was deprecated, "
        "it is not used internally, and will be removed soon",
        category=DeprecationWarning,
        stacklevel=2,
    )
    operation_id = f"{name}{path}"
    operation_id = re.sub(r"\W", "_", operation_id)
    operation_id = f"{operation_id}_{method.lower()}"
    return operation_id


def deep_dict_update(main_dict: dict[Any, Any], update_dict: dict[Any, Any]) -> None:
    for key, value in update_dict.items():
        if key in main_dict and isinstance(main_dict[key], dict) and isinstance(value, dict):
            deep_dict_update(main_dict[key], value)
        elif key in main_dict and isinstance(main_dict[key], list) and isinstance(update_dict[key], list):
            main_dict[key] = list(set(main_dict[key] + update_dict[key]))
        else:
            main_dict[key] = value
