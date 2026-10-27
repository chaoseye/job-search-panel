from __future__ import annotations

import json
from typing import Any

from sqlalchemy import ColumnElement, and_, case, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import as_utc
from app.db.models import AuditLog, Vacancy

# a record about a vacancy has its id at the top of the context: audit(..., vacancy_id=row.id) → {"vacancy_id": 12, …};
# the vacancy card shows these records as its history, the nightly prune keeps them whatever the age
VACANCY_KEY_LIKE = '%"vacancy_id"%'


async def audit(session: AsyncSession, area: str, message: str, level: str = "info", **context: Any) -> None:
    session.add(
        AuditLog(
            level=level,
            area=area,
            message=message,
            context_json=json.dumps(context, ensure_ascii=False, default=str) if context else None,
        )
    )


def about_vacancy(vacancy: Vacancy) -> ColumnElement[bool]:
    """Journal records about this vacancy. The LIKE on the key narrows the scan to the vacancy records, the number is
    compared exactly: LIKE '%"vacancy_id": 1%' also took 10–19, 100–199, … (the card of №1 would
    show the records of other vacancies only). json_extract fails the whole query on malformed JSON,
    CASE leaves such a record out instead. A record older than the vacancy is about an earlier one with its number:
    SQLite gives a new row the number of a deleted last one (deleted demo vacancies would leave their records to the
    first real №1–4)."""
    ctx = AuditLog.context_json
    number = case((func.json_valid(ctx), func.json_extract(ctx, "$.vacancy_id")))
    return and_(ctx.like(VACANCY_KEY_LIKE), number == vacancy.id, AuditLog.ts >= as_utc(vacancy.first_seen_at))
