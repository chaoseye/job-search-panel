"""hh.ru notification e-mails → manual journal: status and a manual sync (job `mail_sync`)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.runtime import Runtime
from app.services.jobs import JobBusy
from app.services.mail_sync import configured, mail_status, sync_mail

router = APIRouter(prefix="/mail", tags=["mail"])


@router.get("/status")
async def status(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    return await mail_status(session, runtime.settings)


@router.post("/sync")
async def sync_now(runtime: Annotated[Runtime, Depends(get_runtime)]) -> dict[str, Any]:
    """Reads new hh messages right now and returns the summary (waits for the job)."""
    if not configured(runtime.settings):
        raise HTTPException(
            status_code=400, detail="IMAP не настроен: заполните IMAP_HOST, IMAP_USER, IMAP_PASSWORD в .env"
        )
    try:
        await runtime.jobs.start("mail_sync", lambda: sync_mail(runtime))
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail="Синхронизация почты уже выполняется") from exc
    await runtime.jobs.wait("mail_sync")
    async with runtime.session_factory() as session:
        return await mail_status(session, runtime.settings)
