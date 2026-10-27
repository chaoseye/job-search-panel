"""Per-day counters (`daily_counters`): applies by mode, hh requests, packets, letters."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import DailyCounter

COUNTER_FIELDS = (
    "applies_api",
    "applies_assist_confirmed",
    "applies_external",
    "hh_requests",
    "batches_created",
    "letters_imported",
)


def local_date(settings: Settings, moment: datetime | None = None) -> str:
    tz = ZoneInfo(settings.timezone or "UTC")
    moment = as_utc(moment) or utcnow()
    return moment.astimezone(tz).strftime("%Y-%m-%d")


async def bump_counter(
    session: AsyncSession, settings: Settings, field: str, n: int = 1, *, moment: datetime | None = None
) -> DailyCounter | None:
    """Adds `n` to the counter of the local day that contains `moment` (default: now)."""
    if field not in COUNTER_FIELDS or n == 0:
        return None
    day = local_date(settings, moment)
    row = (await session.execute(select(DailyCounter).where(DailyCounter.date == day))).scalar_one_or_none()
    if row is None:
        row = DailyCounter(date=day)
        session.add(row)
        await session.flush()
    setattr(row, field, int(getattr(row, field) or 0) + int(n))
    return row


async def counters_between(session: AsyncSession, day_from: str, day_to: str) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(DailyCounter)
                .where(DailyCounter.date >= day_from, DailyCounter.date <= day_to)
                .order_by(DailyCounter.date)
            )
        )
        .scalars()
        .all()
    )
    return [{"date": r.date, **{f: int(getattr(r, f) or 0) for f in COUNTER_FIELDS}} for r in rows]


def sum_counters(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {f: sum(int(r.get(f) or 0) for r in rows) for f in COUNTER_FIELDS}
