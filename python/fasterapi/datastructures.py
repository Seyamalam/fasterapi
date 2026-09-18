"""fasterapi.datastructures — UploadFile (1:1 with ``fastapi.UploadFile``)."""

from __future__ import annotations

from typing import BinaryIO, Optional, Union

from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as _StarletteUploadFile


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
