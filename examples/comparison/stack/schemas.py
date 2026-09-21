"""Shared Pydantic v2 schemas — identical wire shapes for both frameworks."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    price: float = Field(gt=0)
    tags: list[str] = []


class ItemOut(ItemIn):
    id: int
    owner_id: int | None = None


class ItemPage(BaseModel):
    items: list[ItemOut]
    total: int
    page: int
    pages: int


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MeOut(BaseModel):
    username: str
    full_name: str
    role: str


class StatsOut(BaseModel):
    items: int
    users: int
    avg_price: float
    generated_at: str
