"""The state row of the panel (`system_state`, id 1) on a new base: the first page asks several endpoints at once and
each finds no row. The second insert used to wait for the write lock of the first and then fail on the key — HTTP 500
on one of the first requests."""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from app.config import Settings
from app.db.models import SystemState
from app.runtime import Runtime
from app.services.state import get_system_state


async def test_two_first_requests_of_a_new_base_share_one_state_row(settings: Settings) -> None:
    runtime = await Runtime.create(settings)
    try:
        async with runtime.session_factory() as session:
            assert await session.get(SystemState, 1) is None  # a new base has no row yet
        async with runtime.session_factory() as first, runtime.session_factory() as second:
            state = await get_system_state(first)  # inserts the row and holds the write lock until its commit
            # the second request finds no committed row and waits for the lock to insert one of its own
            other = asyncio.create_task(get_system_state(second))
            await asyncio.sleep(0.3)
            assert not other.done()
            await first.commit()
            same = await other  # before the fix: IntegrityError «UNIQUE constraint failed: system_state.id»
            await second.commit()
        assert state.id == same.id == 1
        async with runtime.session_factory() as session:
            assert (await session.execute(select(func.count()).select_from(SystemState))).scalar_one() == 1
            row = await session.get(SystemState, 1)
            assert row is not None and (row.apply_mode, row.apply_mode_override) == ("assist", "auto")
            assert row.created_at is not None and row.paused_reason is None
    finally:
        await runtime.close()
