"""Closed-vacancy check (job `freshness`).

The scanner notices an archived vacancy only when a search returns it again — and closed vacancies drop out of
search results, so a candidate that closed on hh.ru would stay in «Кандидаты» with a ready letter forever. Once a
day the panel re-fetches the cards of candidates that no search has returned for `FRESHNESS_STALE_DAYS` days
(application token, one request per second): archived or gone → `filtered_out` «закрыта на hh.ru»; still open →
the responses counter and publish date are refreshed and `last_seen_at` is bumped, so the card is not re-checked
for another `FRESHNESS_STALE_DAYS` days.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Vacancy
from app.db.session import release_writes
from app.hh.errors import HHAuthError, HHCaptchaRequired, HHError, HHNotFound
from app.hh.vacancy import normalize_vacancy
from app.services.audit import audit

log = structlog.get_logger("freshness")

# statuses worth checking: waiting for a decision or ready to apply; packets, queued/sent applications are left alone
CHECK_STATUSES = ("new", "prescored", "evaluated", "suitable", "maybe", "letter_ready", "needs_manual")
CLOSED_REASON = "закрыта на hh.ru (архив)"
GONE_REASON = "закрыта на hh.ru (удалена)"


async def check_closed(runtime: Any, *, stale_days: int | None = None, limit: int = 500) -> dict[str, Any]:
    settings = runtime.settings
    days = stale_days if stale_days is not None else settings.freshness_stale_days
    now = utcnow()
    since = now - timedelta(days=max(0, days))
    summary: dict[str, Any] = {"checked": 0, "closed": 0, "refreshed": 0, "errors": 0}
    client = runtime.hh_client()
    async with runtime.session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(Vacancy)
                    .where(
                        Vacancy.pipeline_status.in_(CHECK_STATUSES),
                        Vacancy.archived.is_(False),
                        Vacancy.hidden.is_(False),
                        Vacancy.last_seen_at < since,
                    )
                    .order_by(Vacancy.last_seen_at.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        summary["candidates"] = len(rows)
        for row in rows:
            await release_writes(session)  # closed/refreshed rows are committed before the next hh request
            try:
                raw = await client.public(f"/vacancies/{row.hh_id}")
            except HHNotFound:
                _close(row, GONE_REASON)
                summary["closed"] += 1
                summary["checked"] += 1
                await audit(session, "vacancies", f"Закрыта на hh.ru (удалена): «{row.name}»", vacancy_id=row.id)
                continue
            except (HHAuthError, HHCaptchaRequired):
                raise
            except HHError as exc:
                summary["errors"] += 1
                log.info("freshness_fetch_failed", hh_id=row.hh_id, error=exc.summary())
                continue
            summary["checked"] += 1
            if not isinstance(raw, dict):
                summary["errors"] += 1
                continue
            data = normalize_vacancy(raw, full=True)
            if data.archived:
                _close(row, CLOSED_REASON)
                summary["closed"] += 1
                await audit(session, "vacancies", f"Закрыта на hh.ru: «{row.name}»", vacancy_id=row.id)
                continue
            if data.responses_count is not None:
                row.responses_count = data.responses_count
            if data.published_at is not None:
                row.published_at = data.published_at
            row.last_seen_at = now
            summary["refreshed"] += 1
        await session.commit()
    if summary["closed"]:
        log.info("freshness_closed", **summary)
    return summary


def _close(row: Vacancy, reason: str) -> None:
    row.archived = True
    row.pipeline_status = "filtered_out"
    row.pipeline_reason = reason
    row.batch_id = None
    row.last_seen_at = utcnow()
