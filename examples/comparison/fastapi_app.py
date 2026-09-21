"""The comparison app on **FastAPI** (the baseline).

Identical to ``fasterapi_app.py`` except for the framework-import block and
the marked *plugin section* (fasterapi has none here). If the two files
diverge anywhere else, the audit is lying — keep them in lockstep.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import orjson
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.websockets import WebSocketState

# ── framework imports (THE delta vs fasterapi_app.py) ────────────────────────
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Security,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

import stack.db as db
from stack.schemas import ItemIn, ItemOut, ItemPage, MeOut, StatsOut, TokenOut
from stack.security import check_api_key, create_token, decode_token

# ── app + lifespan (create/seed async SQLite) ────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine, sessionmaker = db.make_engine_and_sessionmaker("audit_fastapi.db")
    db.register_state(engine, sessionmaker)
    await db.init_db(sessionmaker, engine)
    app.state.engine = engine
    app.state.ready = True
    yield
    await engine.dispose()


app = FastAPI(
    title="comparison-stack",
    version="1.0.0",
    description="Side-by-side audit app (fastapi flavor)",
    docs_url="/docs",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# ── plugins/middleware ───────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://example.com"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class RequestIDMiddleware:  # pure-ASGI middleware (both frameworks accept it)
    def __init__(self, app):  # type: ignore[no-untyped-def]
        self.app = app

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = f"req-{abs(hash((scope.get('path'), datetime.now(timezone.utc).isoformat()))) % 10**10:010d}"
        scope.setdefault("state", {})["request_id"] = rid

        async def send_wrapper(message):  # type: ignore[no-untyped-def]
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", rid.encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(RequestIDMiddleware)

# ── BEGIN fastapi-only plugin section ────────────────────────────────────────
# (empty — fastapi side uses stock third-party middleware only)
_USING_FASTERAPI_GUARDS = False
# ── END fastapi-only plugin section ──────────────────────────────────────────

# /static mount (StaticFiles plugin surface, mirrored in fasterapi_app.py)
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_DIR.mkdir(exist_ok=True)
(_STATIC_DIR / "hello.txt").write_text("hello from static\n", encoding="utf-8")
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# ── auth dependencies ────────────────────────────────────────────────────────

bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer),
    session: AsyncSession = Depends(db.get_session),
) -> db.User:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = decode_token(credentials.credentials)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc
    username = str(payload.get("sub", ""))
    result = await session.scalars(select(db.User).where(db.User.username == username))
    user = result.first()
    if user is None:
        raise HTTPException(status_code=401, detail="unknown user")
    return user


async def require_admin(user: db.User = Depends(get_current_user)) -> db.User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin required")
    return user


# ── custom exception handler (differential probe) ────────────────────────────


class PaymentRequired(Exception): ...


@app.exception_handler(PaymentRequired)
async def payment_required_handler(request: Request, exc: PaymentRequired) -> JSONResponse:
    return JSONResponse(status_code=402, content={"detail": "payment required", "handler": "custom"})


# ── basic routes ─────────────────────────────────────────────────────────────


@app.get("/ping", tags=["meta"])
async def ping() -> dict:
    return {"pong": True, "app": app.title}


@app.get("/config", tags=["meta"])
async def config() -> dict:
    from stack.settings import get_settings

    s = get_settings()
    return {"app_name": s.app_name, "items_per_page": s.items_per_page,
            "db_url": s.db_url.split(":///")[-1]}


@app.get("/whoami", tags=["meta"])
async def whoami(request: Request) -> dict:
    return {"path": request.url.path, "method": request.method,
            "request_id": getattr(request.state, "request_id", None) is not None}


@app.get("/boom", tags=["meta"])
async def boom() -> dict:
    raise PaymentRequired()


@app.get("/stats", response_model=StatsOut, tags=["db"])
async def stats(session: AsyncSession = Depends(db.get_session)) -> StatsOut:
    items_n = (await session.scalars(select(func.count(db.Item.id)))).one()
    users_n = (await session.scalars(select(func.count(db.User.id)))).one()
    avg_price = (await session.scalars(select(func.coalesce(func.avg(db.Item.price), 0.0)))).one()
    return StatsOut(
        items=items_n,
        users=users_n,
        avg_price=round(float(avg_price), 3),
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


# ── items CRUD (validation + DB + pagination) ────────────────────────────────


@app.get("/items", response_model=ItemPage, tags=["items"])
async def list_items(
    page: int = Query(1, ge=1),
    per_page: int = Query(10, ge=1, le=50),
    tag: str | None = Query(None),
    session: AsyncSession = Depends(db.get_session),
) -> ItemPage:
    stmt = select(db.Item).order_by(db.Item.id)
    if tag:
        stmt = stmt.where(db.Item.tags.contains(tag))
    total = (await session.scalars(select(func.count(db.Item.id)))).one()
    rows = (await session.scalars(stmt.offset((page - 1) * per_page).limit(per_page))).all()
    items = [ItemOut(id=i.id, name=i.name, price=i.price, tags=list(i.tags or []), owner_id=i.owner_id)
             for i in rows]
    return ItemPage(items=items, total=total, page=page,
                    pages=max(1, -(-total // per_page)))


@app.get("/items/expensive", tags=["items"])
async def expensive() -> JSONResponse:
    n = 300
    grid = [[(i * j) % 97 / 7 for j in range(n)] for i in range(n)]
    total = sum(sum(row) for row in grid)
    return JSONResponse(orjson.loads(orjson.dumps({"grid_checksum": total, "n": n})))


@app.get("/items/{item_id}", response_model=ItemOut, tags=["items"])
async def get_item(
    item_id: int,
    with_owner: bool = Query(False),
    session: AsyncSession = Depends(db.get_session),
) -> ItemOut:
    result = await session.scalars(select(db.Item).where(db.Item.id == item_id))
    item = result.first()
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return ItemOut(id=item.id, name=item.name, price=item.price,
                   tags=list(item.tags or []), owner_id=item.owner_id)


async def _audit_log_write(session_factory, action: str, detail: str) -> None:
    """Background task: write an audit row (proves BackgroundTasks in both)."""
    async with session_factory() as session:
        entry = db.AuditLog(action=action, detail=detail)
        entry.stamp()
        session.add(entry)
        await session.commit()


@app.post("/items", response_model=ItemOut, status_code=201, tags=["items"])
async def create_item(
    item: ItemIn,
    tasks: BackgroundTasks,
    user: db.User = Depends(get_current_user),
    session: AsyncSession = Depends(db.get_session),
) -> ItemOut:
    row = db.Item(name=item.name, price=item.price, tags=list(item.tags), owner_id=user.id)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    tasks.add_task(_audit_log_write, db.get_state()[1], "item.create", row.name)
    return ItemOut(id=row.id, name=row.name, price=row.price,
                   tags=list(row.tags or []), owner_id=row.owner_id)


# ── auth routes (OAuth2-style form + JWT) ────────────────────────────────────


class PasswordForm(BaseModel):
    username: str
    password: str


@app.post("/auth/token", response_model=TokenOut, tags=["auth"])
async def token(
    request: Request,
    session: AsyncSession = Depends(db.get_session),
) -> TokenOut:
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    result = await session.scalars(select(db.User).where(db.User.username == username))
    user = result.first()
    if user is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    from stack.settings import get_settings

    if get_settings().users.get(username) != password:
        raise HTTPException(status_code=401, detail="invalid credentials")
    return TokenOut(access_token=create_token(username))


@app.get("/me", response_model=MeOut, tags=["auth"])
async def me(user: db.User = Depends(get_current_user)) -> MeOut:
    return MeOut(username=user.username, full_name=user.full_name, role=user.role)


@app.get("/admin/ping", tags=["auth"])
async def admin_ping(user: db.User = Depends(require_admin)) -> dict:
    return {"admin": user.username}


@app.get("/api-key-ping", tags=["auth"])
async def api_key_ping(x_api_key: str | None = None) -> dict:
    if not check_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="bad api key")
    return {"ok": True}


# ── streaming: SSE ───────────────────────────────────────────────────────────


@app.get("/sse/ticks", tags=["stream"])
async def sse_ticks(count: int = Query(3, ge=1, le=10)) -> StreamingResponse:
    async def gen() -> AsyncIterator[str]:
        for i in range(count):
            payload = orjson.dumps({"tick": i}).decode()
            yield f"id: {i}\nevent: tick\ndata: {payload}\n\n"
            await asyncio.sleep(0.01)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"x-sse-count": str(count)})


# ── websocket echo ───────────────────────────────────────────────────────────


@app.websocket("/ws/echo")
async def ws_echo(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            msg = await websocket.receive_text()
            await websocket.send_json({"echo": msg})
    except WebSocketDisconnect:
        with contextlib.suppress(Exception):
            await websocket.close()
    except Exception:
        with contextlib.suppress(Exception):
            if websocket.client_state != WebSocketState.DISCONNECTED:
                await websocket.close(code=1011)


# ── form/multipart probes ────────────────────────────────────────────────────


@app.post("/upload", tags=["meta"])
async def upload(file: UploadFile = File(...)) -> dict:
    data = await file.read()
    return {"filename": file.filename, "size": len(data),
            "content_type": file.content_type or "application/octet-stream"}


@app.post("/forms/echo", tags=["meta"])
async def forms_echo(name: str = Form(...), qty: int = Form(1)) -> dict:
    return {"name": name, "qty": qty}


# ── meta: which framework am I ───────────────────────────────────────────────


@app.get("/meta/framework", tags=["meta"])
async def meta_framework() -> dict:
    return {"framework": "fastapi", "using_rust_guards": _USING_FASTERAPI_GUARDS,
            "has_build": hasattr(app, "build")}
