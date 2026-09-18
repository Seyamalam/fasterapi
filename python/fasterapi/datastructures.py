"""fasterapi.datastructures — UploadFile (1:1 with ``fastapi.UploadFile``)."""

from __future__ import annotations

from typing import BinaryIO, Optional, Union

from pydantic import GetJsonSchemaHandler as GetJsonSchemaHandler  # noqa: F401
from starlette.datastructures import Address as Address  # noqa: F401
from starlette.datastructures import FormData as FormData  # noqa: F401
from starlette.datastructures import Headers as Headers  # noqa: F401
from starlette.datastructures import QueryParams as QueryParams  # noqa: F401
from starlette.datastructures import State as State  # noqa: F401
from starlette.datastructures import URL as URL  # noqa: F401
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as _StarletteUploadFile

StarletteUploadFile = _StarletteUploadFile


class UploadFile(_StarletteUploadFile):
    """Multipart upload handle with FastAPI-compatible constructor.

    ``UploadFile(file, *, size=None, filename=None, headers=None)`` — extra
    attributes (`filename`, `headers`, `size`) are plain instance state so
    handlers and tests observe the same surface as FastAPI.
    """

    def __init__(
        self,
        file: BinaryIO,
        *,
        size: Optional[int] = None,
        filename: Optional[str] = None,
        headers: Optional[Union[Headers, dict[str, str]]] = None,
    ) -> None:
        super().__init__(file, size=size, filename=filename, headers=headers)
