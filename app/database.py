"""
Database engine + session factory.

Uses SQLAlchemy 2.0 async style with asyncpg.
- `engine`         — module-level singleton async engine
- `AsyncSessionLocal` — session factory (call it to get a new session)
- `get_db()`       — FastAPI dependency that yields a session per request
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

# ---- Engine (one per process) -----------------------------------------------
# `pool_pre_ping=True` transparently reconnects if a connection has been
# dropped by the DB (common with pgbouncer / long-idle workers).
engine = create_async_engine(
    settings.database_url,
    echo=settings.database_echo,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_pre_ping=True,
    future=True,
)

# ---- Session factory --------------------------------------------------------
# `expire_on_commit=False` keeps ORM objects usable after commit, which is
# what we want in FastAPI (we often read fields after the transaction ends).
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


# ---- FastAPI dependency ----------------------------------------------------
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Yields a database session for a single request. Guarantees the session
    is closed even if the handler raises.

    Usage in a route:
        @router.get("/foo")
        async def foo(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        # Session is closed automatically by the `async with` block.


# ---- Lifecycle helpers -----------------------------------------------------
async def dispose_engine() -> None:
    """Called on FastAPI shutdown to close the connection pool cleanly."""
    await engine.dispose()
