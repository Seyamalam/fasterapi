"""taskapi auth routes — password-grant login + current user."""

from __future__ import annotations

from fasterapi import APIRouter, Depends, HTTPException
from fasterapi.security import OAuth2PasswordRequestForm

from ..core.deps import get_current_user, get_store
from ..core.security import hash_password, new_token, verify_password
from ..db.store import Store
from ..models.schemas import TokenOut, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/token", response_model=TokenOut)
async def login(form: OAuth2PasswordRequestForm = Depends(), db: Store = Depends(get_store)):
    user = db.get_user(form.username)
    if user is None or not verify_password(form.password, user["password_hash"], user["salt"]):
        raise HTTPException(status_code=401, detail="Incorrect username or password",
                            headers={"WWW-Authenticate": "Bearer"})
    token = new_token()
    db.issue_token(user["username"], token)
    return {"access_token": token, "token_type": "bearer"}


@router.get("/me", response_model=UserOut)
async def me(user: dict = Depends(get_current_user)):
    return {"username": user["username"], "created_at": user["created_at"]}
