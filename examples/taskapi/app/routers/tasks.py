"""taskapi task routes — CRUD with pagination, filters and background jobs."""

from __future__ import annotations

from typing import Optional

from fasterapi import APIRouter, Depends, HTTPException, Path, Query
from fasterapi.background import BackgroundTasks

from ..core.deps import get_current_user, get_store, pagination
from ..db.store import Store
from ..models.schemas import TaskCreate, TaskOut, TaskPage, TaskStatus, TaskUpdate

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _notify_completed(db: Store, owner: str, task_id: int, title: str) -> None:
    db.append_feed("task.done", f"{owner} completed #{task_id} ({title})")


@router.post("", response_model=TaskOut, status_code=201)
async def create_task(payload: TaskCreate, user: dict = Depends(get_current_user),
                      db: Store = Depends(get_store)):
    return db.create_task(user["username"], payload.title, payload.notes, payload.priority)


@router.get("", response_model=TaskPage)
async def list_tasks(user: dict = Depends(get_current_user),
                     db: Store = Depends(get_store),
                     page: dict = Depends(pagination),
                     status: Optional[TaskStatus] = Query(default=None)):
    rows, total = db.list_tasks(user["username"], status=status.value if status else None,
                                skip=page["skip"], limit=page["limit"])
    return {"items": rows, "total": total, "skip": page["skip"], "limit": page["limit"]}


@router.get("/{task_id}", response_model=TaskOut)
async def read_task(task_id: int = Path(ge=1), user: dict = Depends(get_current_user),
                    db: Store = Depends(get_store)):
    task = db.get_task(user["username"], task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return task


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(payload: TaskUpdate, bg: BackgroundTasks, task_id: int = Path(ge=1),
                      user: dict = Depends(get_current_user),
                      db: Store = Depends(get_store)):
    patch = {k: v for k, v in
             {"title": payload.title, "notes": payload.notes,
              "priority": payload.priority,
              "status": payload.status.value if payload.status else None}.items()
             if v is not None}
    task = db.update_task(user["username"], task_id, patch)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    if task["status"] == TaskStatus.DONE.value:
        bg.add_task(_notify_completed, db, user["username"], task_id, task["title"])
    return task


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: int = Path(ge=1), user: dict = Depends(get_current_user),
                      db: Store = Depends(get_store)):
    if not db.delete_task(user["username"], task_id):
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return None
