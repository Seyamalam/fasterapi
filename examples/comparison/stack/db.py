"""Shared async SQLAlchemy layer (aiosqlite) — identical for both frameworks.

In-memory SQLite + StaticPool: each app process seeds its own copy at
startup, so differential output comparison sees identical rows/IDs while
the perf load never hits disk-lock contention.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, JSON, Float, ForeignKey, Integer, String, select, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.pool import StaticPool

from .settings import get_settings


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    role: Mapped[str] = mapped_column(String(16), default="user")

    items: Mapped[list[Item]] = relationship(back_populates="owner")


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    price: Mapped[float] = mapped_column(Float)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=True)

    owner: Mapped[User | None] = relationship(back_populates="items")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[str] = mapped_column(String(32), default="")

    def stamp(self) -> None:  # tiny "model logic" so the ORM isn't purely dumb
        self.created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_engine_and_sessionmaker(db_name: str | None = None) -> tuple[object, async_sessionmaker[AsyncSession]]:
    """Engine + session factory (called per app process).

    ``db_name`` gives each app process its own SQLite file so differential
    runs start from identical seeds without cross-process interference.
    WAL + busy_timeout keep concurrent readers/writers well-behaved.
    """
    s = get_settings()
    url = f"sqlite+aiosqlite:///{db_name}" if db_name else s.db_url
    kwargs: dict = {}
    if ":memory:" in url:
        kwargs["poolclass"] = StaticPool
    # Fixed, generous pool: under concurrent load (e.g. audit POST waves) a
    # starved pool surfaces as 500s/timeouts that have nothing to do with the
    # framework being measured. Same settings for both apps = fair comparison.
    kwargs.setdefault("pool_size", 20)
    kwargs.setdefault("max_overflow", 0)
    kwargs.setdefault("pool_timeout", 60)
    engine = create_async_engine(url, echo=False, **kwargs)

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragma(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def init_db(sessionmaker: async_sessionmaker[AsyncSession], engine=None) -> None:
    """Create tables + seed identical rows in both apps (idempotent)."""
    if engine is None:  # pragma: no cover - callers pass the engine
        engine, sessionmaker = make_engine_and_sessionmaker()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with sessionmaker() as session:
        existing = (await session.scalars(select(func.count(User.id)))).one()
        if existing:
            return
        users = {
            "alice": User(id=1, username="alice", full_name="Alice Liddell", role="admin"),
            "bob": User(id=2, username="bob", full_name="Bob the Builder", role="user"),
            "carol": User(id=3, username="carol", full_name="Carol Danvers", role="user"),
        }
        session.add_all(users.values())
        for i in range(1, 26):
            session.add(
                Item(
                    id=i,
                    name=f"item-{i:02d}",
                    price=round(1.5 * i + 0.25, 2),
                    tags=["a", "b"] if i % 2 == 0 else ["a"],
                    owner_id=1 if i % 3 == 0 else 2,
                )
            )
        await session.commit()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-compatible yield dependency (works identically in both)."""
    _engine, sessionmaker = get_state()
    async with sessionmaker() as session:
        yield session


# Per-process state registry (each app registers itself at startup).
_STATE: dict[str, object] = {}


def register_state(engine: object, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    _STATE["engine"] = engine
    _STATE["sessionmaker"] = sessionmaker


def get_state() -> tuple[object, async_sessionmaker[AsyncSession]]:
    return _STATE["engine"], _STATE["sessionmaker"]  # type: ignore[index,return-value]


async def count_items(session: AsyncSession, tag: str | None = None) -> int:
    stmt = select(Item)
    if tag:
        stmt = stmt.where(Item.tags.contains(tag))
    rows = await session.scalars(stmt)
    return len(rows.all())
