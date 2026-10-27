"""«Панель запущена» / «Панель остановлена» in the journal: how long the panel stood still is
seen in the «Журнал» tab instead of being guessed from «interrupted (process restarted)» job runs.

`.\\run.ps1 stop` kills the process, so a stop record is not guaranteed: the start record says whether the previous run
ended cleanly and when the panel was last active (the newest journal record or job run before this start).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import AuditLog, JobRun
from app.services.audit import audit

AREA = "system"
STARTED = "Панель запущена"
STOPPED = "Панель остановлена"


def _duration(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days} дн. {hours} ч"
    if hours:
        return f"{hours} ч {mins} мин"
    return f"{mins} мин"


async def record_start(session: AsyncSession, settings: Settings, version: str, *, now: datetime | None = None) -> str:
    """Writes the start record and returns its text."""
    now = now or utcnow()
    previous = (
        await session.execute(
            select(AuditLog.message).where(AuditLog.area == AREA).order_by(desc(AuditLog.id)).limit(1)
        )
    ).scalar_one_or_none()
    last_journal = (await session.execute(select(func.max(AuditLog.ts)))).scalar()
    # started_at, not finished_at: Runtime.create has just closed the runs the killed process left «running» with
    # finished_at = now (mark_stale_runs); the scan tick starts a run every few minutes while the panel is alive
    last_run = (await session.execute(select(func.max(JobRun.started_at)))).scalar()
    moments = [m for m in (as_utc(last_journal), as_utc(last_run)) if m is not None]
    text = f"{STARTED} (версия {version})"
    if moments:
        last = max(moments)
        local = last.astimezone(ZoneInfo(settings.timezone)).strftime("%d.%m %H:%M")
        pause = _duration((now - last).total_seconds())
        activity = f"последняя активность {local} (перерыв {pause})"
        if previous is not None and previous.startswith(STOPPED):
            text += f"; до этого остановлена штатно, {activity}"
        elif previous is not None:
            text += f"; предыдущий запуск завершился без штатной остановки, {activity}"
        else:
            text += f"; {activity}"
    await audit(session, AREA, text)
    return text


async def record_stop(session: AsyncSession) -> None:
    await audit(session, AREA, STOPPED)
