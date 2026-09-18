"""taskapi dependencies — db handle, current user, pagination."""

from __future__ import annotations

from typing import Any

from fasterapi import Depends, HTTPException, Query
from fasterapi.security import OAuth2PasswordBearer

from ..db import store as store_mod
from ..db.store import Store

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")


def get_store() -> Store:
    # Late binding on purpose: tests (and embedders) swap `store_mod.store`
    # in one place and every dependency follows.
    return store_mod.store


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Store = Depends(get_store),
) -> dict[str, Any]:
    username = db.token_owner(token)
    if username is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token",
                            headers={"WWW-Authenticate": "Bearer"})
    user = db.get_user(username)
    if user is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=401, detail="Unknown user")
    return user


def pagination(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, int]:
    return {"skip": skip, "limit": limit}
