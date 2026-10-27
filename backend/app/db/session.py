"""Async engine/session factory and Alembic migrations runner."""

from __future__ import annotations

import asyncio
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import BACKEND_DIR, Settings
from app.db import changes as _changes  # noqa: F401 - registers the write listeners behind the analytics cache

SQLITE_CACHE_KIB = 32 * 1024  # page cache per connection; SQLite's default is 2 MB


def unicode_lower(value: Any) -> Any:
    """SQL lower() with Python's case mapping. SQLite's own lower() and LIKE fold ASCII letters only, so
    `ilike` / `icontains` (`lower(x) LIKE lower(y)`) took «аналитик» and «Аналитик» for different words. Any value
    becomes text, as in the built-in; NULL stays NULL."""
    if isinstance(value, str):
        return value.lower()
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").lower()
    return str(value).lower()


def make_engine(url: str) -> AsyncEngine:
    engine = create_async_engine(url, echo=False, connect_args={"timeout": 30})
    if url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _page_cache(dbapi_connection: Any, _record: Any) -> None:
            # the statistics join thousands of applications to their vacancies (random pages of a table of hundreds
            # of MB): with the default 2 MB cache every run re-reads them
            cursor = dbapi_connection.cursor()
            cursor.execute(f"PRAGMA cache_size=-{SQLITE_CACHE_KIB}")
            cursor.close()

        @event.listens_for(engine.sync_engine, "connect")
        def _unicode_lower(dbapi_connection: Any, _record: Any) -> None:
            # replaces the built-in lower() on the panel's connections only: alembic, backups and VACUUM run on
            # their own connections with the built-in one, so no index, view or trigger may use lower() (none does)
            dbapi_connection.create_function("lower", 1, unicode_lower, deterministic=True)

    return engine


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def release_writes(session: AsyncSession) -> None:
    """Commit what the session holds before a network call (hh.ru request, a pause).

    SQLite has one writer: once a transaction has written (a flush, an autoflush before a query), the write lock stays
    until commit. Left open across an hh request (up to 4 retries × 30 s) it makes every other write of the panel —
    the UI included — wait and fail with «database is locked». Objects stay usable (expire_on_commit=False)."""
    if session.in_transaction():
        await session.commit()


async def enable_wal(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(text("PRAGMA foreign_keys=ON"))


def alembic_config(settings: Settings) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "app" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.sync_db_url)
    return cfg


def run_migrations_sync(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(settings), "head")


async def run_migrations(settings: Settings) -> None:
    await asyncio.to_thread(run_migrations_sync, settings)
