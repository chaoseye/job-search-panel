"""Applications: consent, limits, queue (mode A), assist (mode B), listing."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.db.base import as_utc
from app.db.models import Application, Vacancy
from app.runtime import Runtime
from app.services import applier as svc
from app.services import manual_negotiation as manual
from app.services.jobs import JobBusy
from app.services.state import get_system_state

router = APIRouter(prefix="/applications", tags=["applications"])


def _raise(exc: svc.ApplyError) -> None:
    detail: Any = {"message": str(exc), "code": exc.code} if exc.code else str(exc)
    raise HTTPException(status_code=exc.status, detail=detail) from exc


async def _get(session: AsyncSession, app_id: int) -> Application:
    row = await session.get(Application, app_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Отклик не найден")
    return row


async def _status(runtime: Runtime, session: AsyncSession) -> dict[str, Any]:
    state = await get_system_state(session)
    limits = await limits_dict(runtime, session)
    consent = as_utc(state.consent_accepted_at)
    return {
        "apply_mode": state.apply_mode,
        "consent_accepted_at": consent.isoformat(timespec="seconds") if consent else None,
        "resume_id": await svc.resolve_resume_id(session),
        "dry_run": runtime.settings.apply_dry_run,
        "autopilot": runtime.settings.apply_autopilot,
        "autopilot_min_score": runtime.settings.apply_min_score_autopilot,
        "limits": limits,
        "job_running": runtime.jobs.is_running("apply"),
    }


async def limits_dict(runtime: Runtime, session: AsyncSession) -> dict[str, Any]:
    limits = await svc.limits_status(session, runtime.settings)
    return limits.to_dict()


@router.get("/status")
async def status(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    data = await _status(runtime, session)
    await session.commit()
    return data


@router.post("/consent")
async def consent(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    await svc.accept_consent(session)
    await session.commit()
    return await _status(runtime, session)


@router.get("")
async def list_applications(
    session: Annotated[AsyncSession, Depends(get_session)],
    runtime: Annotated[Runtime, Depends(get_runtime)],
    status: str | None = None,
    mode: str | None = None,
    stage: str | None = Query(default=None, description="sent|viewed|invitation|discard|hidden|no_answer|unread"),
    days: int = Query(default=7, ge=1, le=365, description="threshold for no_answer"),
    date_from: str | None = Query(default=None, description="YYYY-MM-DD (local)"),
    date_to: str | None = Query(default=None, description="YYYY-MM-DD (local, inclusive)"),
    page: int = Query(0, ge=0),
    per_page: int = Query(50, ge=1, le=200),
    sort: str = Query("sent", description="sent|vacancy|employer|viewed|status|updated|mode|score"),
    order: str = Query("desc", description="asc|desc"),
) -> dict[str, Any]:
    from app.services.stats import resolve_period

    d_from = d_to = None
    if date_from or date_to:
        try:
            period = resolve_period(
                runtime.settings, "custom", date_from=date_from or date_to, date_to=date_to or date_from
            )
        except ValueError as exc:  # «2026-13-45», «bogus» — was a 500
            raise HTTPException(status_code=400, detail=f"неверная дата: {exc}") from exc
        d_from, d_to = period.start, period.end
    return await svc.list_applications(
        session,
        status=status or None,
        mode=mode or None,
        stage=stage or None,
        days=days,
        date_from=d_from,
        date_to=d_to,
        page=page,
        per_page=per_page,
        sort=sort,
        order="asc" if order == "asc" else "desc",
    )


class QueueRequest(BaseModel):
    vacancy_ids: list[int] = Field(..., min_length=1, max_length=200)


@router.post("/queue")
async def queue(
    body: QueueRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    try:
        result = await svc.enqueue(session, runtime.settings, body.vacancy_ids)
    except svc.ApplyError as exc:
        _raise(exc)
    await session.commit()
    started = False
    if result["queued"]:
        try:
            await runtime.jobs.start("apply", lambda: svc.run_apply(runtime))
            started = True
        except JobBusy:
            started = False
    result["job_started"] = started
    result["status"] = await _status(runtime, session)
    return result


class CheckRequest(BaseModel):
    vacancy_ids: list[int] = Field(..., min_length=1, max_length=200)
    mode: str = "api"


@router.post("/check")
async def check(
    body: CheckRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Dry validation: which of the vacancies can be applied to right now and why not."""
    items: list[dict[str, Any]] = []
    for vid in body.vacancy_ids:
        vacancy = await session.get(Vacancy, vid)
        if vacancy is None:
            items.append({"vacancy_id": vid, "ok": False, "problems": ["вакансия не найдена"]})
            continue
        problems, _ = await svc.validate_for_apply(session, runtime.settings, vacancy, mode=body.mode)
        items.append({"vacancy_id": vid, "name": vacancy.name, "ok": not problems, "problems": problems})
    return {"items": items, "status": await _status(runtime, session)}


@router.post("/run")
async def run_now(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    dry_run: bool = Query(False, description="сухой прогон только для этого запуска (CLI `apply` без --real)"),
    max_items: int | None = Query(None, ge=1, le=1000, description="не больше N откликов за запуск"),
) -> dict[str, Any]:
    try:
        run_id = await runtime.jobs.start("apply", lambda: svc.run_apply(runtime, max_items=max_items, dry_run=dry_run))
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail="Отправка уже выполняется") from exc
    return {"job_run_id": run_id}


@router.post("/{app_id}/cancel")
async def cancel(app_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    row = await _get(session, app_id)
    try:
        await svc.cancel_application(session, row)
    except svc.ApplyError as exc:
        _raise(exc)
    await session.commit()
    return svc.application_row(row, await session.get(Vacancy, row.vacancy_id))


@router.post("/{app_id}/retry")
async def retry(
    app_id: int,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    row = await _get(session, app_id)
    try:
        await svc.retry_application(session, row)
    except svc.ApplyError as exc:
        _raise(exc)
    await session.commit()
    try:
        await runtime.jobs.start("apply", lambda: svc.run_apply(runtime))
    except JobBusy:
        pass
    return svc.application_row(row, await session.get(Vacancy, row.vacancy_id))


@router.post("/{app_id}/withdraw")
async def withdraw(
    app_id: int,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    row = await _get(session, app_id)
    try:
        await svc.withdraw(session, runtime.hh_client(), row)
    except svc.ApplyError as exc:
        await session.commit()
        _raise(exc)
    await session.commit()
    return svc.application_row(row, await session.get(Vacancy, row.vacancy_id))


# ---- assist mode ------------------------------------------------------------


class AssistOpenRequest(BaseModel):
    vacancy_id: int


class ManualUpdateRequest(BaseModel):
    """What the user saw on hh.ru for a sent application (no sync available)."""

    state: Literal["response", "invitation", "discard"] | None = None
    viewed: bool | None = None
    text: str | None = Field(default=None, max_length=20000)
    author: Literal["employer", "applicant"] = "employer"
    occurred_at: datetime | None = None


@router.post("/{app_id}/manual-update")
async def manual_update(
    app_id: int,
    body: ManualUpdateRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Records an employer response (stage, view, message) for an application the panel cannot sync."""
    from app.services.interview_prep import request_interview_prep
    from app.services.sync import negotiation_detail

    row = await _get(session, app_id)
    try:
        neg = await manual.record_update(
            session,
            row,
            state=body.state,
            viewed=body.viewed,
            text=body.text,
            author=body.author,
            occurred_at=body.occurred_at,
        )
    except manual.ManualError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    if body.state == "invitation" and runtime.settings.interview_prep_auto:
        await request_interview_prep(session, runtime.settings, row, notes=None, auto=True)
    await session.commit()
    vacancy = await session.get(Vacancy, row.vacancy_id)
    return {"application": svc.application_row(row, vacancy, neg), "detail": await negotiation_detail(session, neg)}


class PrepRequest(BaseModel):
    notes: str | None = Field(None, max_length=2000)


@router.post("/{app_id}/interview-prep")
async def interview_prep(
    app_id: int,
    body: PrepRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Queues an `interview_prep` task for Claude Code; the sheet appears in the application panel after import."""
    from app.services.interview_prep import PrepError, list_preps, prep_row, request_interview_prep

    app_row = await session.get(Application, app_id)
    if app_row is None:
        raise HTTPException(status_code=404, detail="Отклик не найден")
    try:
        prep, batch_id = await request_interview_prep(session, runtime.settings, app_row, notes=body.notes)
    except PrepError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    await session.commit()
    return {"prep": prep_row(prep) if prep else None, "batch_id": batch_id, "preps": await list_preps(session, app_id)}


@router.get("/{app_id}/interview-preps")
async def interview_preps(app_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> list[dict[str, Any]]:
    from app.services.interview_prep import list_preps

    return await list_preps(session, app_id)


@router.delete("/{app_id}/manual-journal")
async def delete_manual_journal(app_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    """Drops everything recorded by hand for this application (the application itself stays)."""
    row = await _get(session, app_id)
    try:
        removed = await manual.delete_manual_journal(session, row)
    except manual.ManualError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    await session.commit()
    vacancy = await session.get(Vacancy, row.vacancy_id)
    return {"removed_messages": removed, "application": svc.application_row(row, vacancy, None)}


@router.post("/assist/open")
async def assist_open(
    body: AssistOpenRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    vacancy = await session.get(Vacancy, body.vacancy_id)
    if vacancy is None:
        raise HTTPException(status_code=404, detail="Вакансия не найдена")
    try:
        row = await svc.assist_open(session, runtime.settings, vacancy)
    except svc.ApplyError as exc:
        _raise(exc)
    await session.commit()
    data = svc.application_row(row, vacancy)
    data["apply_url"] = vacancy.apply_alternate_url or vacancy.alternate_url
    return data


class ConfirmRequest(BaseModel):
    sent: bool


@router.post("/{app_id}/confirm")
async def confirm(
    app_id: int,
    body: ConfirmRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    row = await _get(session, app_id)
    try:
        await svc.assist_confirm(session, row, sent=body.sent, settings=runtime.settings)
    except svc.ApplyError as exc:
        _raise(exc)
    await session.commit()
    return svc.application_row(row, await session.get(Vacancy, row.vacancy_id))
