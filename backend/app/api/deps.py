from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.hh.client import HHClient
from app.runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


def get_settings_dep(request: Request) -> Settings:
    return get_runtime(request).settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with get_runtime(request).session_factory() as session:
        yield session


def get_hh_client(request: Request) -> HHClient:
    return get_runtime(request).hh_client()
