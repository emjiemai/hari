"""Engine and sessions. The schema creates itself on start (no migration step)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from hari.config import normalize_database_url, settings
from hari.db.models import Base

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def configure(url: str | None = None) -> AsyncEngine:
    """Create the engine (tests pass an in-memory SQLite URL)."""
    global _engine, _sessionmaker
    url, connect_args = normalize_database_url(url or settings.database_url)
    kwargs: dict = {}
    if url.startswith("postgresql"):
        # Render closes idle connections: test each one before use, and keep the pool small (small plans).
        kwargs.update(pool_pre_ping=True, pool_size=5, max_overflow=5, pool_recycle=1800,
                      connect_args=connect_args)
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url:
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
    _engine = create_async_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(_engine.sync_engine, "connect")
        def _fk_on(dbapi_conn, _record):  # noqa: ANN001 — SQLite enforces foreign keys only when asked
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def create_schema() -> None:
    if _engine is None:
        configure()
    assert _engine is not None
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def ping() -> bool:
    """For the health check: can the database answer right now?"""
    if _engine is None:
        return False
    try:
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 — any failure means "not healthy", the reason is in the logs
        return False


async def dispose() -> None:
    if _engine is not None:
        await _engine.dispose()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """One unit of work: commit on success, roll back on any error."""
    if _sessionmaker is None:
        configure()
    assert _sessionmaker is not None
    async with _sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
