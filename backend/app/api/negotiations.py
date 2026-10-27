"""Sync of negotiations with hh.ru and the read side (messages, events)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.db.models import Application, Negotiation
from app.runtime import Runtime
from app.services import manual_negotiation as manual
from app.services.jobs import JobBusy
from app.services.state import auth_public_state, get_hh_auth
from app.services.sync import (
    negotiation_detail,
    refresh_messages,
    reply_draft_row,
    request_reply_draft,
    run_sync,
    sync_status,
)

router = APIRouter(prefix="/negotiations", tags=["negotiations"])


@router.get("/status")
async def status(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    data = await sync_status(session)
    data["job_running"] = runtime.jobs.is_running("sync")
    data["interval_min"] = runtime.settings.sync_interval_min
    return data


@router.post("/sync")
async def start_sync(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
    full: bool = False,
) -> dict[str, Any]:
    if not auth_public_state(await get_hh_auth(session))["logged_in"]:
        raise HTTPException(status_code=400, detail="Сначала войдите в hh.ru")
    try:
        run_id = await runtime.jobs.start("sync", lambda: run_sync(runtime, full=full))
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_run_id": run_id}


async def _get(session: AsyncSession, hh_id: str) -> Negotiation:
    row = (await session.execute(select(Negotiation).where(Negotiation.hh_id == hh_id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Отклик не найден в базе панели")
    return row


@router.get("/{hh_id}")
async def detail(hh_id: str, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    row = await _get(session, hh_id)
    data = await negotiation_detail(session, row)
    app_row = await session.get(Application, row.application_id) if row.application_id else None
    data["application_id"] = app_row.id if app_row else None
    return data


class ReplyDraftRequest(BaseModel):
    user_intent: str | None = None


@router.post("/{hh_id}/reply-draft")
async def reply_draft(
    hh_id: str,
    body: ReplyDraftRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """M6 (F-8.9.5, желательно): queue a `reply_draft` task for Claude Code; the draft appears after import."""
    row = await _get(session, hh_id)
    try:
        draft, batch_id = await request_reply_draft(session, runtime.settings, row, body.user_intent)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return {"draft": reply_draft_row(draft), "batch_id": batch_id}


@router.post("/{hh_id}/refresh-messages")
async def refresh(
    hh_id: str,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    if not auth_public_state(await get_hh_auth(session))["logged_in"]:
        raise HTTPException(status_code=400, detail="Сначала войдите в hh.ru")
    row = await _get(session, hh_id)
    if manual.is_manual(row.hh_id):
        raise HTTPException(
            status_code=409, detail="Это ручной журнал: переписка записывается вами, hh.ru не опрашивается"
        )
    data = await refresh_messages(runtime, session, row)
    await session.commit()
    return data


@router.delete("/{hh_id}/messages/{message_id}")
async def delete_message(
    hh_id: str, message_id: int, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    """Removes a message recorded by hand (manual journal only)."""
    row = await _get(session, hh_id)
    try:
        await manual.delete_manual_message(session, row, message_id)
    except manual.ManualError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    await session.commit()
    return await negotiation_detail(session, row)
