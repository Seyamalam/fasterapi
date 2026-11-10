"""fasterapi.utils — fastapi.utils import-surface parity.

These are the helpers FastAPI exposes for route introspection and schema
generation; implementations mirror upstream semantics so code that imports
them from ``fastapi.utils`` works unchanged on fasterapi.
"""

from __future__ import annotations

import re
from typing import Any, Union

from .datastructures import DefaultPlaceholder, DefaultType


def deep_dict_update(main_dict: dict[Any, Any], update_dict: dict[Any, Any]) -> None:
    for key, value in update_dict.items():
        if (
            key in main_dict
            and isinstance(main_dict[key], dict)
            and isinstance(value, dict)
        ):
            deep_dict_update(main_dict[key], value)
        elif (
            key in main_dict
            and isinstance(main_dict[key], list)
            and isinstance(update_dict[key], list)
        ):
            main_dict[key] = main_dict[key] + update_dict[key]
        else:
            main_dict[key] = value


def get_path_param_names(path: str) -> set[str]:
    return set(re.findall(r"\{(.*?)\}", path))


def generate_operation_id_for_path(*, name: str, path: str, method: str) -> str:
    operation_id = f"{name}{path}"
    operation_id = re.sub(r"\W", "_", operation_id)
    return operation_id + f"_{method.lower()}"


def is_body_allowed_for_status_code(status_code: Union[int, str, None]) -> bool:
    if status_code is None:
        return True
    if isinstance(status_code, int):
        current_status_code = status_code
    else:
        assert len(status_code) == 3, "Status codes must be 3 digits"
        if status_code.startswith("*"):
            current_status_code = int(status_code.removeprefix("*")) // 100
        else:
            current_status_code = int(status_code)
    if status_code in {"default", "1XX", "2XX", "3XX", "4XX", "5XX"}:
        current_status_code = int(status_code[0])
    if not (isinstance(current_status_code, int) and 100 <= current_status_code <= 600):
        return True  # Non-standard: allow body (FastAPI behavior)
    return current_status_code not in {204, 205, 304} and not (
        isinstance(status_code, str) and status_code.endswith("XX")
        and int(status_code[0]) < 2
    ) and not (100 <= current_status_code < 200)


def get_value_or_default(
    first_item: Union[DefaultPlaceholder, DefaultType],
    *extra_items: Union[DefaultPlaceholder, DefaultType],
) -> Union[DefaultPlaceholder, DefaultType]:
    """Pick the first non-placeholder item (FastAPI's merge rule)."""
    items = (first_item,) + extra_items
    for item in items:
        if not isinstance(item, DefaultPlaceholder):
            return item
    return first_item


def generate_unique_id(route: Any) -> str:
    assert route.methods
    operation_id = f"{route.name}{route.path_format}"
    operation_id = re.sub(r"\W", "_", operation_id)
    assert route.methods
    method = sorted(route.methods)[0].lower()
    return operation_id + f"_{method}"


def create_model_field(
    name: str,
    type_: Any,
    default: Any = None,
    field_info: Any = None,
    alias: Any = None,
    mode: str = "validation",
) -> Any:
    """Compatibility shim: fasterapi validates params natively, so this
    returns a small record rather than pydantic's internal ModelField —
    enough for third-party code that introspects it."""
    from dataclasses import dataclass

    @dataclass
    class _ModelField:
        name: str
        type_: Any
        default: Any
        field_info: Any
        alias: Any
        required: bool

        def validate(self, value: Any) -> tuple[Any, Any]:
            return value, None

    return _ModelField(
        name=name,
        type_=type_,
        default=default,
        field_info=field_info,
        alias=alias if alias is not None else name,
        required=default is None or default is Ellipsis,
    )
