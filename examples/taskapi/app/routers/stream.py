"""taskapi live routes — SSE activity feed + health."""

from __future__ import annotations

from fasterapi import APIRouter, Depends
from fasterapi.responses import EventSourceResponse
from fasterapi.sse import ServerSentEvent

from ..core.deps import get_current_user, get_store
from ..db.store import Store
from ..models.schemas import HealthOut

router = APIRouter(tags=["live"])


@router.get("/feed", response_class=EventSourceResponse)
async def feed(user: dict = Depends(get_current_user), db: Store = Depends(get_store)):
    for event in db.read_feed(limit=20):
        yield ServerSentEvent(data={"id": event["id"], "kind": event["kind"], "text": event["text"]},
                              event="activity", id=str(event["id"]))
    yield {"hello": user["username"]}


@router.get("/health", response_model=HealthOut)
async def health():
    import fasterapi

    return {"status": "ok", "version": fasterapi.__version__, "has_rust": fasterapi.HAS_RUST}
