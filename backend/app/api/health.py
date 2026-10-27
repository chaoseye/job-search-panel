from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import desc, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.api.deps import get_runtime, get_session
from app.db.base import as_utc
from app.db.models import AuditLog, ClaudeBatch, JobRun, Notification
from app.runtime import Runtime
from app.services.state import auth_public_state, get_hh_auth, get_system_state

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(
    request: Request,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await session.execute(text("SELECT 1"))
    auth = auth_public_state(await get_hh_auth(session))
    state = await get_system_state(session)
    scheduler = getattr(request.app.state, "scheduler", None)
    jobs = [j.id for j in scheduler.get_jobs()] if scheduler else []
    last_runs = (await session.execute(select(JobRun).order_by(desc(JobRun.started_at)).limit(5))).scalars().all()
    from sqlalchemy import func

    queued_tg = int(
        (
            await session.execute(
                select(func.count(Notification.id)).where(
                    Notification.channel == "telegram", Notification.status == "queued"
                )
            )
        ).scalar()
        or 0
    )
    pending_batches = int(
        (await session.execute(select(func.count(ClaudeBatch.id)).where(ClaudeBatch.status == "pending"))).scalar() or 0
    )
    return {
        "telegram_queue": queued_tg,
        "pending_batches": pending_batches,
        "running_jobs": runtime.jobs.running_jobs(),
        "ok": True,
        "version": __version__,
        "db": "ok",
        "auth": auth,
        "paused_reason": state.paused_reason,
        "apply_mode": state.apply_mode,
        "scheduler_jobs": jobs,
        "hh_requests_limiter_waits": runtime.limiter.waits,
        "last_jobs": [
            {
                "job": r.job,
                "status": r.status,
                "started_at": (as_utc(r.started_at) or r.started_at).isoformat(),
                "error": r.error,
            }
            for r in last_runs
        ],
    }


@router.get("/audit")
async def audit_tail(session: Annotated[AsyncSession, Depends(get_session)], limit: int = 50) -> list[dict[str, Any]]:
    rows = (await session.execute(select(AuditLog).order_by(desc(AuditLog.id)).limit(min(limit, 500)))).scalars().all()
    return [
        {
            "id": r.id,
            "ts": (as_utc(r.ts) or r.ts).isoformat(),
            "level": r.level,
            "area": r.area,
            "message": r.message,
            "context": r.context_json,
        }
        for r in rows
    ]
