"""Tenders of workspace.ru: the watched list, reading it now (job `ws_tenders_sync`), answers through Claude, statuses
(services/ws_tenders.py)."""

from __future__ import annotations

from typing import Annotated, Any, Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.db.models import WsTender
from app.runtime import Runtime
from app.services import ws_tenders as svc
from app.services.jobs import JobBusy

router = APIRouter(prefix="/tenders", tags=["tenders"])

Group = Literal["relevant", "open", "drafted", "responded", "closed", "all"]
Section = Literal[
    "web-development",
    "crm",
    "apps-development",
    "identity",
    "copywriting",
    "videoproduction",
    "marketing",
    "smm",
    "seo",
    "context",
]


async def _get(session: AsyncSession, tender_id: int) -> WsTender:
    row = await session.get(WsTender, tender_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Тендер не найден")
    return row


def _raise(exc: svc.TenderError) -> NoReturn:
    raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.get("")
async def list_tenders(
    session: Annotated[AsyncSession, Depends(get_session)],
    runtime: Annotated[Runtime, Depends(get_runtime)],
    group: Group = "relevant",
    section: Section | None = None,
    q: str | None = Query(None, max_length=200),
    sort: Literal["fit", "published", "deadline", "budget"] = "fit",
    page: int = Query(0, ge=0, le=10_000),
    per_page: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    return await svc.list_tenders(
        session, runtime.settings, group=group, section=section, q=q, sort=sort, page=page, per_page=per_page
    )


@router.get("/summary")
async def summary(
    session: Annotated[AsyncSession, Depends(get_session)], runtime: Annotated[Runtime, Depends(get_runtime)]
) -> dict[str, Any]:
    """Menu badge: suitable open tenders not looked at yet."""
    return {"new_relevant": await svc.new_relevant_count(session, runtime.settings)}


@router.get("/settings")
async def get_settings(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    data = await svc.tender_settings(session)
    data["available_sections"] = [{"key": k, "label": v} for k, v in svc.SECTIONS.items()]
    return data


class TenderSettingsUpdate(BaseModel):
    sections: list[Section] | None = Field(None, max_length=10)
    min_score: int | None = Field(None, ge=0, le=100)
    rates: str | None = Field(None, max_length=3000)


@router.put("/settings")
async def put_settings(
    body: TenderSettingsUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    try:
        data = await svc.save_tender_settings(session, body.model_dump(exclude_none=True))
    except svc.TenderError as exc:
        _raise(exc)
    await svc.rescore_all(session, runtime.settings)  # the watched sections and the threshold decide «suitable»
    await session.commit()
    data["available_sections"] = [{"key": k, "label": v} for k, v in svc.SECTIONS.items()]
    return data


@router.post("/sync")
async def sync(runtime: Annotated[Runtime, Depends(get_runtime)]) -> dict[str, Any]:
    """Job `ws_tenders_sync` in the background; its result is in the «Журнал» tab and in /api/jobs."""
    try:
        run_id = await runtime.jobs.start(svc.JOB, lambda: svc.sync_tenders(runtime))
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_run_id": run_id}


class RepliesRequest(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=50)
    notes: str | None = Field(None, max_length=2000)


@router.post("/replies")
async def request_replies(
    body: RepliesRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """A `tender_reply` task per tender in the next packet for /hh-batch (built right away when possible)."""
    queued, skipped, batch_id = await svc.request_replies(session, runtime.settings, body.ids, body.notes)
    await session.commit()
    return {"queued": queued, "skipped": skipped, "batch_id": batch_id}


@router.get("/{tender_id}")
async def detail(
    tender_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    return await svc.tender_detail(session, runtime.settings, await _get(session, tender_id))


class TenderUpdate(BaseModel):
    notes: str | None = Field(None, max_length=2000)


@router.put("/{tender_id}")
async def update(
    tender_id: int,
    body: TenderUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    row = await _get(session, tender_id)
    await svc.update_notes(session, row, body.notes)
    await session.commit()
    return await svc.tender_detail(session, runtime.settings, row)


class ReplyEdit(BaseModel):
    text: str = Field(..., min_length=20, max_length=10_000)


@router.put("/{tender_id}/reply")
async def edit_reply(
    tender_id: int,
    body: ReplyEdit,
    session: Annotated[AsyncSession, Depends(get_session)],
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    row = await _get(session, tender_id)
    try:
        await svc.save_reply(session, row, body.text)
    except svc.TenderError as exc:
        _raise(exc)
    await session.commit()
    return await svc.tender_detail(session, runtime.settings, row)


@router.post("/{tender_id}/{action}")
async def action(
    tender_id: int,
    action: Literal["interesting", "skip", "hide", "restore", "responded", "replied", "declined"],
    session: Annotated[AsyncSession, Depends(get_session)],
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    row = await _get(session, tender_id)
    try:
        await svc.set_status(session, row, action)
    except svc.TenderError as exc:
        _raise(exc)
    await session.commit()
    return await svc.tender_detail(session, runtime.settings, row)
