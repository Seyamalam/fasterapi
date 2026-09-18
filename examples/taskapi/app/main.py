"""taskapi — example backend project on fasterapi.

Task manager API showcasing drop-in FastAPI patterns running on the
Rust-accelerated core: routers, OAuth2 password flow, background jobs,
file upload/download (sendfile), SSE feed, rate limiting, CORS, lifespan.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fasterapi import FastAPI
from fasterapi.guards import RateLimiter
from fasterapi.middleware.cors import CORSMiddleware

from .core.config import settings
from .core.security import data_dir, hash_password
from .db import store as store_mod
from .routers import auth, files, stream, tasks


@asynccontextmanager
async def lifespan(app: FastAPI):
    data_dir()
    try:
        digest, salt = hash_password("demo1234")
        store_mod.store.add_user("demo", digest, salt)
    except KeyError:
        pass
    store_mod.store.append_feed("app.start", f"{settings.app_name} ready")
    yield
    store_mod.store.append_feed("app.stop", f"{settings.app_name} stopping")


def create_app() -> FastAPI:
    app = FastAPI(title="taskapi", version="0.1.0",
                  description="Example task-manager backend on fasterapi",
                  lifespan=lifespan)
    app.add_middleware(RateLimiter, per_second=settings.rate_per_second)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(auth.router)
    app.include_router(tasks.router)
    app.include_router(files.router)
    app.include_router(stream.router)
    return app


app = create_app()


if __name__ == "__main__":
    app.build()
    app.serve(host=settings.host, port=settings.port)
