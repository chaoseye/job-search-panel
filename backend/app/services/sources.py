"""Where a vacancy lives (migration 0032): hh.ru or HireHi. The names, the links and the key of a
vacancy in the packets for Claude, so that every place says it the same way."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Vacancy

HH = "hh"
HIREHI = "hirehi"
SOURCES = (HH, HIREHI)
SOURCE_LABELS = {HH: "hh.ru", HIREHI: "HireHi"}
# the key of a HireHi vacancy in a packet and its result: `hirehi-<job id>` (an hh.ru vacancy keeps its number; the
# stubs of the other tasks are `advice-`, `studio-`, `tender-`)
KEY_PREFIX = {HIREHI: "hirehi-"}


def is_hh(row: Vacancy) -> bool:
    """A vacancy of hh.ru — one not saved yet has no `source` (the default comes with the INSERT), it is hh.ru."""
    return (row.source or HH) == HH


def source_label(source: str | None) -> str:
    return SOURCE_LABELS.get(source or HH, source or "hh.ru")


def vacancy_url(row: Vacancy) -> str | None:
    """The page of the vacancy on its site."""
    if row.alternate_url:
        return row.alternate_url
    return f"https://hh.ru/vacancy/{row.hh_id}" if is_hh(row) and row.hh_id else None


def apply_url(row: Vacancy) -> str | None:
    """Where the response starts: the apply page of hh.ru, the page of the vacancy on HireHi (its «Отклик» button)."""
    return row.apply_alternate_url or vacancy_url(row)


def vacancy_key(row: Vacancy) -> str:
    """The id of the vacancy in a packet for Claude and in its result."""
    prefix = KEY_PREFIX.get(row.source)
    if prefix:
        return f"{prefix}{row.source_id}"
    return row.hh_id or f"panel-{row.id}"


async def vacancy_by_key(session: AsyncSession, key: str) -> Vacancy | None:
    """The vacancy of a packet key (vacancy_key)."""
    for source, prefix in KEY_PREFIX.items():
        if key.startswith(prefix):
            stmt = select(Vacancy).where(Vacancy.source == source, Vacancy.source_id == key[len(prefix) :])
            return (await session.execute(stmt)).scalar_one_or_none()
    if key.startswith("panel-") and key[6:].isdigit():
        return await session.get(Vacancy, int(key[6:]))
    return (await session.execute(select(Vacancy).where(Vacancy.hh_id == key))).scalar_one_or_none()
