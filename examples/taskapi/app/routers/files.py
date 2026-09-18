"""taskapi file routes — upload to disk, download via FileResponse (sendfile)."""

from __future__ import annotations

import os

from fasterapi import APIRouter, Depends, File, HTTPException, UploadFile
from fasterapi.responses import FileResponse

from ..core.config import settings
from ..core.deps import get_current_user
from ..core.security import data_dir
from ..models.schemas import UploadOut

router = APIRouter(prefix="/files", tags=["files"])


def _safe_name(filename: str | None) -> str:
    base = os.path.basename(filename or "upload.bin")
    return base or "upload.bin"


@router.post("", response_model=UploadOut, status_code=201)
async def upload(file: UploadFile = File(), user: dict = Depends(get_current_user)):
    data = await file.read()
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="File too large")
    name = _safe_name(file.filename)
    dest = os.path.join(data_dir(), f"{user['username']}__{name}")
    with open(dest, "wb") as fh:
        fh.write(data)
    return {"filename": name, "size": len(data), "content_type": file.content_type}


@router.get("/{filename}")
async def download(filename: str, user: dict = Depends(get_current_user)):
    dest = os.path.join(data_dir(), f"{user['username']}__{_safe_name(filename)}")
    if not os.path.isfile(dest):
        raise HTTPException(status_code=404, detail=f"File {filename} not found")
    return FileResponse(dest)
