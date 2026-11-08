"""Closed-vacancy check (job `freshness`).

The scanner notices an archived vacancy only when a search returns it again — and closed vacancies drop out of
search results, so a candidate that closed on hh.ru would stay in «Кандидаты» with a ready letter forever. Once a
day the panel re-fetches the cards of candidates that no search has returned for `FRESHNESS_STALE_DAYS` days
(application token, one request per second): archived or gone → `filtered_out` «закрыта на hh.ru»; still open →
the publish date is refreshed and `last_seen_at` is bumped, so the card is not re-checked for another
`FRESHNESS_STALE_DAYS` days. The responses counter stays as the search results gave it: the full card of hh has no
`counters` (the job `responses` refreshes it from the search results, services/responses_refresh.py).

Then the responses nobody confirmed for the vacancies closed this way are cancelled (otherwise the open page of the
assistant would stay in «Нужно действие» for weeks) — but only after `UNCONFIRMED_GRACE_DAYS`: a page opened in the
evening may have been sent on hh.ru before the employer closed the vacancy that night, and «Да, отправлено» must still
work in the morning.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import Application, Vacancy
from app.db.session import release_writes
from app.hh.errors import HHAuthError, HHCaptchaRequired, HHError, HHNotFound
from app.hh.vacancy import normalize_vacancy
from app.services.audit import audit
from app.services.vacancies import OPEN_RESPONSE_STATUSES, cancel_response

log = structlog.get_logger("freshness")

# statuses worth checking: waiting for a decision or ready to apply; packets, queued/sent applications are left alone
CHECK_STATUSES = ("new", "prescored", "evaluated", "suitable", "maybe", "letter_ready", "needs_manual")
CLOSED_REASON = "закрыта на hh.ru (архив)"
GONE_REASON = "закрыта на hh.ru (удалена)"
# how long a response of a closed vacancy may wait for «Да, отправлено» before the check cancels it: a page opened on
# Friday evening and confirmed on Monday morning keeps it
UNCONFIRMED_GRACE_DAYS = 3
# what «Отклики» shows: the panel does not know whether it went, only that nobody confirmed it
UNCONFIRMED_ERROR = "отменён: вакансия закрыта на hh.ru, отправка не подтверждена"


async def check_closed(runtime: Any, *, stale_days: int | None = None, limit: int = 500) -> dict[str, Any]:
    settings = runtime.settings
    days = stale_days if stale_days is not None else settings.freshness_stale_days
    now = utcnow()
    since = now - timedelta(days=max(0, days))
    summary: dict[str, Any] = {"checked": 0, "closed": 0, "refreshed": 0, "errors": 0, "responses_cancelled": 0}
    client = runtime.hh_client()
    async with runtime.session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(Vacancy)
                    .where(
                        # a HireHi vacancy has no hh.ru number: HireHi checks its own (services/hirehi.check_closed)
                        Vacancy.source == "hh",
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
            if data.responses_count is not None:  # not on real cards so far: the full card has no `counters`
                row.responses_count = data.responses_count
            if data.published_at is not None:
                row.published_at = data.published_at
            row.last_seen_at = now
            summary["refreshed"] += 1
        await release_writes(session)
        summary["responses_cancelled"] = await cancel_unconfirmed(session, now, source="hh")
        await session.commit()
    if summary["closed"] or summary["responses_cancelled"]:
        log.info("freshness_closed", **summary)
    return summary


async def cancel_unconfirmed(session: AsyncSession, now: datetime, *, source: str = "hh") -> int:
    """Cancels the responses of the vacancies closed on their site (archived and `filtered_out`: this check or the scan
    closed them, tonight or on an earlier night) that have waited for «Да, отправлено» longer than
    UNCONFIRMED_GRACE_DAYS — the open page of the assistant and the API queue. A closed vacancy the user brought back
    («Вернуть») is no longer `filtered_out` and keeps its response; one without `queued_at` (old rows) counts as long
    waiting. Each source cancels its own: hh.ru from this check, HireHi from its check (services/hirehi.check_closed),
    with the same grace. Returns how many were cancelled."""
    from app.services.sources import source_label

    cutoff = now - timedelta(days=UNCONFIRMED_GRACE_DAYS)
    site = source_label(source)
    rows = (
        (
            await session.execute(
                select(Application)
                .join(Vacancy, Vacancy.id == Application.vacancy_id)
                .where(
                    Application.status.in_(OPEN_RESPONSE_STATUSES),
                    Vacancy.source == source,
                    Vacancy.archived.is_(True),
                    Vacancy.pipeline_status == "filtered_out",
                    or_(Application.queued_at.is_(None), Application.queued_at < cutoff),
                )
                .order_by(Application.id)
            )
        )
        .scalars()
        .all()
    )
    for app_row in rows:
        await cancel_response(
            session,
            app_row,
            error_text=UNCONFIRMED_ERROR if source == "hh" else UNCONFIRMED_ERROR.replace("hh.ru", site),
            why=f"вакансия закрыта на {site}, отправку так и не подтвердили",
        )
    return len(rows)


def _close(row: Vacancy, reason: str) -> None:
    row.archived = True
    row.pipeline_status = "filtered_out"
    row.pipeline_reason = reason
    row.batch_id = None
    row.last_seen_at = utcnow()
