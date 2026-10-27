"""Vacancies tab: listing, detail, user actions."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.db.models import Vacancy
from app.hh.errors import HHError
from app.hh.vacancy import EXPERIENCE_RANGES
from app.runtime import Runtime
from app.services import letters as letters_svc
from app.services import vacancies as svc
from app.services.jobs import JobBusy, job_label
from app.services.scanner import reprocess_vacancy
from app.services.state import auth_public_state, get_hh_auth

router = APIRouter(prefix="/vacancies", tags=["vacancies"])


async def _get(session: AsyncSession, vacancy_id: int) -> Vacancy:
    row = await session.get(Vacancy, vacancy_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Вакансия не найдена")
    return row


@router.get("")
async def list_vacancies(
    session: Annotated[AsyncSession, Depends(get_session)],
    group: str = "candidates",
    min_score: int | None = Query(None, ge=0, le=100),
    search_id: int | None = None,
    area: str | None = None,
    work_format: str | None = None,
    salary_from: int | None = Query(None, ge=0),
    q: str | None = None,
    include_snoozed: bool = False,
    unseen_only: bool = False,
    sort: Literal["score", "published", "responses", "salary"] = "score",
    page: int = Query(0, ge=0),
    per_page: int = Query(50, ge=1, le=200),
    selection_profile_id: int | None = Query(None, ge=0, description="0 = базовая анкета"),
    experience: str | None = Query(None, description="hh experience ids через запятую: noExperience,between1And3"),
) -> dict[str, Any]:
    if group not in svc.GROUPS:
        raise HTTPException(status_code=400, detail=f"Неизвестная группа: {group}")
    experience_ids = [x for x in (experience or "").split(",") if x in EXPERIENCE_RANGES]
    if experience and not experience_ids:
        raise HTTPException(status_code=400, detail=f"Неизвестный опыт: {experience}")
    return await svc.list_vacancies(
        session,
        group=group,
        min_score=min_score,
        search_id=search_id,
        area=area,
        work_format=work_format,
        salary_from=salary_from,
        q=q,
        include_snoozed=include_snoozed,
        unseen_only=unseen_only,
        sort=sort,
        page=page,
        per_page=per_page,
        selection_profile_id=selection_profile_id,
        experience=experience_ids or None,
    )


@router.get("/summary")
async def summary(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    return await svc.status_counts(session)


@router.get("/hidden-employers")
async def hidden_employers(session: Annotated[AsyncSession, Depends(get_session)]) -> list[dict[str, Any]]:
    return await svc.list_hidden_employers(session)


@router.delete("/hidden-employers/{pk}")
async def unhide_employer(
    pk: int,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    restored = await svc.unhide_employer(session, pk, runtime.settings)
    if restored is None:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    await session.commit()
    return {"ok": True, "restored": restored}


class MarkSeenRequest(BaseModel):
    ids: list[int] | None = None


@router.post("/mark-seen")
async def mark_seen(body: MarkSeenRequest, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    n = await svc.mark_seen(session, body.ids)
    await session.commit()
    return {"marked": n}


@router.get("/{vacancy_id}")
async def detail(vacancy_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    row = await _get(session, vacancy_id)
    return await svc.vacancy_detail(session, row)


class SkipRequest(BaseModel):
    reason: str | None = Field(None, max_length=500)


@router.post("/{vacancy_id}/skip")
async def skip(
    vacancy_id: int, body: SkipRequest, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    row = await _get(session, vacancy_id)
    await svc.skip(session, row, body.reason)
    await session.commit()
    return await svc.vacancy_detail(session, row)


class SnoozeRequest(BaseModel):
    days: int = Field(3, ge=1, le=60)


@router.post("/{vacancy_id}/snooze")
async def snooze(
    vacancy_id: int, body: SnoozeRequest, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    row = await _get(session, vacancy_id)
    await svc.snooze(session, row, body.days)
    await session.commit()
    return await svc.vacancy_detail(session, row)


@router.post("/{vacancy_id}/unsnooze")
async def unsnooze(vacancy_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    row = await _get(session, vacancy_id)
    await svc.unsnooze(session, row)
    await session.commit()
    return await svc.vacancy_detail(session, row)


@router.post("/{vacancy_id}/hide")
async def hide(vacancy_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    row = await _get(session, vacancy_id)
    await svc.hide(session, row)
    await session.commit()
    return await svc.vacancy_detail(session, row)


@router.post("/{vacancy_id}/hide-employer")
async def hide_employer(vacancy_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    row = await _get(session, vacancy_id)
    affected = await svc.hide_employer(session, row)
    await session.commit()
    data = await svc.vacancy_detail(session, row)
    data["affected"] = affected
    return data


@router.post("/{vacancy_id}/restore")
async def restore(
    vacancy_id: int,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    row = await _get(session, vacancy_id)
    try:
        await svc.restore(session, row, runtime.settings)
    except svc.VacancyActionError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    await session.commit()
    return await svc.vacancy_detail(session, row)


class QuestionsRequest(BaseModel):
    text: str = Field(..., min_length=5, max_length=8000)


@router.post("/{vacancy_id}/questions")
async def employer_questions(
    vacancy_id: int,
    body: QuestionsRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Queues an `answer_questions` task: the employer's response-form questions → answers from the profile."""
    from app.services.questions import QuestionsError, answers_row, list_answers, request_answers

    row = await _get(session, vacancy_id)
    try:
        req, batch_id = await request_answers(session, runtime.settings, row, body.text)
    except QuestionsError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    await session.commit()
    return {"request": answers_row(req), "batch_id": batch_id, "answers": await list_answers(session, row.id)}


@router.get("/{vacancy_id}/questions")
async def employer_questions_list(
    vacancy_id: int, session: Annotated[AsyncSession, Depends(get_session)]
) -> list[dict[str, Any]]:
    from app.services.questions import list_answers

    return await list_answers(session, vacancy_id)


@router.post("/{vacancy_id}/to-batch")
async def to_batch(vacancy_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    row = await _get(session, vacancy_id)
    try:
        await svc.to_batch(session, row)
    except svc.VacancyActionError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    await session.commit()
    return await svc.vacancy_detail(session, row)


class FeedbackRequest(BaseModel):
    feedback: Literal["ok", "wrong"]
    comment: str | None = Field(None, max_length=1000)


@router.post("/{vacancy_id}/feedback")
async def feedback(
    vacancy_id: int, body: FeedbackRequest, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    row = await _get(session, vacancy_id)
    if not await svc.set_feedback(session, row, body.feedback, body.comment):
        raise HTTPException(status_code=409, detail="У вакансии ещё нет оценки Claude")
    await session.commit()
    return await svc.vacancy_detail(session, row)


class LetterRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=20000)


@router.put("/{vacancy_id}/letter")
async def save_letter(
    vacancy_id: int,
    body: LetterRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    row = await _get(session, vacancy_id)
    try:
        await letters_svc.save_edit(session, row, body.text, check_style=runtime.settings.humanizer_check)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return await svc.vacancy_detail(session, row)


class LetterCheckRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=20000)


@router.post("/letter-check")
async def letter_check(body: LetterCheckRequest) -> dict[str, Any]:
    """Run the humanizer-ru scanner on a letter draft (Russian only); the text is not stored."""
    from app.services import humanizer

    if not humanizer.available():
        raise HTTPException(status_code=409, detail="Скилл humanizer-ru не установлен в .claude/skills")
    if not humanizer.is_russian(body.text):
        return {"skipped": "не русский текст", "result": None, "warnings": []}
    result = await humanizer.scan_text(body.text)
    if result is None:
        raise HTTPException(
            status_code=503, detail="Сканер не запустился: нужны пакеты razdel и pymorphy3 в backend/.venv"
        )
    return {"result": result, "warnings": humanizer.warnings_for(result)}


class RevisionRequestBody(BaseModel):
    comment: str = Field(..., min_length=1, max_length=1000)


@router.post("/{vacancy_id}/letter/revision")
async def request_revision(
    vacancy_id: int, body: RevisionRequestBody, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    row = await _get(session, vacancy_id)
    try:
        await letters_svc.request_revision(session, row, body.comment)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return await svc.vacancy_detail(session, row)


@router.post("/{vacancy_id}/letter/template")
async def letter_template(vacancy_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    row = await _get(session, vacancy_id)
    await letters_svc.apply_template(session, row)
    await session.commit()
    return await svc.vacancy_detail(session, row)


@router.post("/{vacancy_id}/letter/{letter_id}/restore")
async def restore_letter(
    vacancy_id: int, letter_id: int, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    row = await _get(session, vacancy_id)
    try:
        await letters_svc.restore_version(session, row, letter_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return await svc.vacancy_detail(session, row)


class AddByLinksRequest(BaseModel):
    text: str = Field(..., min_length=5, max_length=20000, description="ссылки или id вакансий через пробел/строку")
    selection_profile_id: int | None = None
    create_packet: bool = True


@router.post("/add")
async def add_by_links(
    body: AddByLinksRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Adds vacancies by hh.ru links: fetches the cards, puts them into the pipeline past the rules and builds
    a packet for /hh-batch."""
    from app.services.manual_add import add_vacancies

    if not auth_public_state(await get_hh_auth(session))["logged_in"]:
        raise HTTPException(status_code=400, detail="Сначала войдите в hh.ru")
    running = runtime.jobs.running_exclusive()
    if running is not None:
        raise HTTPException(
            status_code=409, detail=f"Сейчас выполняется задача «{job_label(running)}» — попробуйте через минуту"
        )
    outcome = await add_vacancies(
        session,
        runtime.settings,
        runtime.hh_client(),
        body.text,
        profile_id=body.selection_profile_id,
        create_packet=body.create_packet,
    )
    await session.commit()
    return {"added": outcome.added, "errors": outcome.errors, "batch_ids": outcome.batch_ids}


class ReprocessAllRequest(BaseModel):
    statuses: list[str] | None = None
    refetch: bool = False
    cancel_pending: bool = False  # cancel packets waiting for /hh-batch and re-evaluate their vacancies too


@router.post("/reprocess-all")
async def reprocess_all_now(
    body: ReprocessAllRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Re-run rules and prescore for all vacancies in the given statuses with the current criteria (job `reprocess`)."""
    from app.services.scanner import REPROCESS_STATUSES, reprocess_all

    if not auth_public_state(await get_hh_auth(session))["logged_in"]:
        raise HTTPException(status_code=400, detail="Сначала войдите в hh.ru")
    statuses = tuple(s for s in (body.statuses or list(REPROCESS_STATUSES)) if s in REPROCESS_STATUSES)
    if not statuses:
        raise HTTPException(status_code=400, detail="Нет допустимых статусов для переоценки")
    try:
        run_id = await runtime.jobs.start(
            "reprocess",
            lambda: reprocess_all(runtime, statuses=statuses, refetch=body.refetch, cancel_pending=body.cancel_pending),
        )
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_run_id": run_id, "statuses": list(statuses)}


@router.post("/{vacancy_id}/reprocess")
async def reprocess(
    vacancy_id: int,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
    refetch: bool = True,
) -> dict[str, Any]:
    row = await _get(session, vacancy_id)
    try:
        await reprocess_vacancy(session, runtime.hh_client(), runtime.settings, row, refetch=refetch)
    except HHError as exc:
        await session.commit()
        raise HTTPException(status_code=502, detail=f"hh.ru: {exc.summary()}") from exc
    await session.commit()
    return await svc.vacancy_detail(session, row)
