"""Morning digest of «hot» vacancies (event `hot`): fresh, few responses, good score — the ones to answer first.

Same rule as the «Горячие» block of the Analytics page (`analytics.hot_report`), sent once a day at `HOT_DIGEST_AT`
when there is something new: a vacancy already reported in the last `REPEAT_DAYS` days is not repeated, so the
digest is skipped rather than sent empty. The bot command /hot renders the current list on demand. A morning the panel
was off or asleep at HOT_DIGEST_AT gets the digest later, marked as late (scheduler.hot_digest_catch_up).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import Notification
from app.services.analytics import hot_report, load_base
from app.services.notify import emit_event
from app.services.state import get_setting, set_setting

log = structlog.get_logger("hot_digest")

SENT_SETTING = "hot_digest_sent"  # {vacancy id: iso timestamp of the digest it was in}
LAST_RUN_SETTING = "hot_digest_last_run"  # iso UTC time of the last run, with or without new vacancies
REPEAT_DAYS = 7
MAX_ITEMS = 10


async def current_hot(session: AsyncSession, settings: Settings, *, now: datetime | None = None) -> dict[str, Any]:
    base = await load_base(session, settings, scope="all", now=now)
    return hot_report(
        base,
        settings,
        max_responses=settings.hot_max_responses,
        max_age_days=settings.hot_max_age_days,
        min_score=settings.hot_min_score,
        limit=200,
    )


def hot_payload(report: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total": len(items),
        "max_responses": report["max_responses"],
        "max_age_days": report["max_age_days"],
        "min_score": report["min_score"],
        "items": [
            {
                "id": i["id"],
                "name": i["name"],
                "employer": i["employer"],
                "url": i["url"],
                "score": i["score"],
                "prescore": i["prescore"],
                "responses": i["responses"],
                "age_hours": i["age_hours"],
                "salary": i["salary"],
            }
            for i in items[:MAX_ITEMS]
        ],
    }


async def last_run(session: AsyncSession) -> datetime | None:
    """When the digest last ran (sent, or found nothing new): its own mark, or — for a base from before the mark
    existed — the last `hot` event of the feed. None: the digest never ran here."""
    mark = await get_setting(session, LAST_RUN_SETTING)
    last_event = (
        await session.execute(
            select(func.max(Notification.created_at)).where(
                Notification.channel == "ui", Notification.event_type == "hot"
            )
        )
    ).scalar()
    known = [t for t in (as_utc(_parse(mark if isinstance(mark, str) else None)), as_utc(last_event)) if t]
    return max(known) if known else None


async def hot_digest_tick(
    runtime: Any, *, force: bool = False, late: str | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Scheduler entry point (cron at HOT_DIGEST_AT) — `force=True` ignores the «already reported» memory, `late`
    («09:00») marks a digest the catch-up sends after the panel was off at that time. Every run leaves its mark
    (`last_run`), the one that found nothing new too: the catch-up must know the morning was not missed."""
    settings: Settings = runtime.settings
    now = now or utcnow()
    stamp = now.isoformat(timespec="seconds")
    async with runtime.session_factory() as session:
        report = await current_hot(session, settings, now=now)
        sent_raw = await get_setting(session, SENT_SETTING) or {}
        sent: dict[str, str] = sent_raw if isinstance(sent_raw, dict) else {}
        fresh_since = now - timedelta(days=REPEAT_DAYS)
        items = [
            i
            for i in report["items"]
            if force or (as_utc(_parse(sent.get(str(i["id"])))) or fresh_since) <= fresh_since
        ]
        await set_setting(session, LAST_RUN_SETTING, stamp)
        if not items:
            await session.commit()
            return {"skipped": "нет новых горячих вакансий", "hot_total": report["total"]}
        payload = hot_payload(report, items)
        if late:
            payload["late"] = late
        rows = await emit_event(session, settings, "hot", payload, now=now)
        for i in items:
            sent[str(i["id"])] = stamp
        # forget entries older than the repeat window so the setting does not grow forever
        sent = {k: v for k, v in sent.items() if (as_utc(_parse(v)) or now) >= fresh_since}
        await set_setting(session, SENT_SETTING, sent)
        await session.commit()
    telegram = any(r.channel == "telegram" for r in rows)
    log.info("hot_digest_emitted", items=len(items), telegram=telegram)
    return {"ok": True, "items": len(items), "telegram": telegram}


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
