"""taskapi schemas — Pydantic v2 request/response models."""

from __future__ import annotations

import datetime
from enum import Enum

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    OPEN = "open"
    DOING = "doing"
    DONE = "done"


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    notes: str = Field(default="", max_length=2000)
    priority: int = Field(default=3, ge=1, le=5)


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)
    priority: int | None = Field(default=None, ge=1, le=5)
    status: TaskStatus | None = None


class TaskOut(BaseModel):
    id: int
    owner: str
    title: str
    notes: str
    priority: int
    status: TaskStatus
    created_at: datetime.datetime
    updated_at: datetime.datetime


class TaskPage(BaseModel):
    items: list[TaskOut]
    total: int
    skip: int
    limit: int


class UserOut(BaseModel):
    username: str
    created_at: datetime.datetime


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UploadOut(BaseModel):
    filename: str
    size: int
    content_type: str | None = None


class HealthOut(BaseModel):
    status: str
    version: str
    has_rust: bool
