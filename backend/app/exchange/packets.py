"""Building packets for /hh-batch."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import (
    Application,
    ClaudeBatch,
    CoverLetter,
    InterviewPrep,
    LetterRevisionRequest,
    Message,
    Negotiation,
    QuestionAnswers,
    ReplyDraft,
    ResumeAdvice,
    Studio,
    StudioLetterRequest,
    Vacancy,
    VacancyEvaluation,
    WsTender,
    WsTenderReplyRequest,
)
from app.exchange.guidelines_md import ensure_guidelines
from app.exchange.schemas import PROTOCOL_VERSION, Packet, PacketTask, PacketVacancy
from app.services.audit import audit
from app.services.preferences import get_preferences
from app.services.profile import active_profile
from app.services.relocate import local_path
from app.services.rules import looks_like_agency

log = structlog.get_logger("exchange.packets")


class PacketError(Exception):
    pass


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def pending_dir(settings: Settings) -> Path:
    return settings.exchange_dir / "pending"


def done_dir(settings: Settings) -> Path:
    return settings.exchange_dir / "done"


def imported_dir(settings: Settings) -> Path:
    return settings.exchange_dir / "imported"


def rejected_dir(settings: Settings) -> Path:
    return settings.exchange_dir / "rejected"


def new_batch_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d_%H%M")
    return f"{stamp}_{secrets.token_hex(2)}"


def packet_vacancy(row: Vacancy, max_chars: int) -> PacketVacancy:
    description = (row.description_text or "").strip()
    if len(description) > max_chars:
        description = description[:max_chars].rstrip() + "\n…[обрезано]"
    if not description:
        parts = [p for p in (row.raw_short_json,) if p]
        short = _loads(parts[0], {}) if parts else {}
        snippet = short.get("snippet") if isinstance(short, dict) else None
        if isinstance(snippet, dict):
            description = "\n".join(
                s for s in (snippet.get("requirement"), snippet.get("responsibility")) if isinstance(s, str) and s
            )
    published = as_utc(row.published_at)
    return PacketVacancy(
        id=row.hh_id,
        name=row.name,
        employer={
            "name": row.employer_name,
            "accredited_it": row.employer_accredited_it,
            "agency": looks_like_agency(row.employer_name),
        },
        area=row.area_name,
        work_format=_loads(row.work_format_ids_json, []),
        employment_form=row.employment_form_id,
        experience=row.experience_id,
        experience_name=row.experience_name,
        salary=(
            {"from": row.salary_from, "to": row.salary_to, "currency": row.salary_currency, "gross": row.salary_gross}
            if (row.salary_from or row.salary_to)
            else None
        ),
        key_skills=_loads(row.key_skills_json, []),
        professional_roles=_loads(row.professional_roles_json, []),
        responses_count=row.responses_count,
        published_at=published.isoformat(timespec="seconds") if published else None,
        alternate_url=row.alternate_url,
        response_letter_required=row.response_letter_required,
        has_test=row.has_test,
        description=description,
    )


async def user_notes(session: AsyncSession, limit: int = 5) -> list[str]:
    rows = (
        await session.execute(
            select(VacancyEvaluation, Vacancy.name)
            .join(Vacancy, Vacancy.id == VacancyEvaluation.vacancy_id)
            .where(VacancyEvaluation.user_feedback == "wrong", VacancyEvaluation.user_comment.is_not(None))
            .order_by(desc(VacancyEvaluation.updated_at))
            .limit(limit)
        )
    ).all()
    notes: list[str] = []
    for ev, name in rows:
        comment = (ev.user_comment or "").strip()
        if comment:
            notes.append(f"Оценка «{name}» ({ev.score}, {ev.verdict}) была неверной: {comment}")
    return notes


async def _assign_profile_files(
    session: AsyncSession,
    settings: Settings,
    work: list[tuple[PacketTask, Vacancy | None, PacketRequest | None]],
) -> None:
    """Tasks of vacancies whose criteria set has its own resume point Claude at profile_set<id>.md."""
    from app.services.selection import set_profile_file, set_resumes

    sets = await set_resumes(session)
    if not sets:
        return
    folder = settings.exchange_dir
    for task, vacancy, req in work:
        row = vacancy
        vacancy_id = getattr(req, "vacancy_id", None)
        if row is None and vacancy_id is not None:
            row = await session.get(Vacancy, vacancy_id)
        if row is None or row.selection_profile_id not in sets:
            continue
        name = set_profile_file(row.selection_profile_id)
        if (folder / name).exists():
            task.profile_file = name


async def _candidates(session: AsyncSession, vacancy_ids: list[int] | None) -> list[Vacancy]:
    stmt = select(Vacancy).where(Vacancy.hidden.is_(False))
    if vacancy_ids:
        stmt = stmt.where(Vacancy.id.in_(vacancy_ids))
    else:
        stmt = stmt.where(
            Vacancy.pipeline_status == "prescored",
            or_(Vacancy.snoozed_until.is_(None), Vacancy.snoozed_until <= utcnow()),
        )
    stmt = stmt.order_by(desc(Vacancy.prescore), desc(Vacancy.published_at), desc(Vacancy.id))
    return list((await session.execute(stmt)).scalars().all())


async def _revision_tasks(session: AsyncSession, max_chars: int) -> list[tuple[PacketTask, LetterRevisionRequest]]:
    rows = (
        (
            await session.execute(
                select(LetterRevisionRequest)
                .where(LetterRevisionRequest.status == "queued")
                .order_by(LetterRevisionRequest.id)
            )
        )
        .scalars()
        .all()
    )
    tasks: list[tuple[PacketTask, LetterRevisionRequest]] = []
    for req in rows:
        vacancy = await session.get(Vacancy, req.vacancy_id)
        if vacancy is None:
            req.status = "cancelled"
            continue
        letter = await session.get(CoverLetter, req.previous_letter_id) if req.previous_letter_id else None
        if letter is None:
            letter = (
                await session.execute(
                    select(CoverLetter)
                    .where(CoverLetter.vacancy_id == vacancy.id, CoverLetter.is_current.is_(True))
                    .limit(1)
                )
            ).scalar_one_or_none()
        if letter is None:
            req.status = "cancelled"
            continue
        evaluation = (
            await session.execute(
                select(VacancyEvaluation)
                .where(VacancyEvaluation.vacancy_id == vacancy.id, VacancyEvaluation.is_current.is_(True))
                .limit(1)
            )
        ).scalar_one_or_none()
        tasks.append(
            (
                PacketTask(
                    type="letter_revision",
                    vacancy=packet_vacancy(vacancy, max_chars),
                    previous_letter=letter.text,
                    user_comment=req.user_comment,
                    evaluation=(
                        {
                            "score": evaluation.score,
                            "verdict": evaluation.verdict,
                            "summary": evaluation.summary,
                            "matched": _loads(evaluation.matched_json, []),
                            "gaps": _loads(evaluation.gaps_json, []),
                        }
                        if evaluation
                        else None
                    ),
                ),
                req,
            )
        )
    return tasks


THREAD_MESSAGES = 10
# requests that ride along in a packet next to the vacancy tasks (each is keyed by a vacancy id or a stub id)
PacketRequest = (
    LetterRevisionRequest
    | ReplyDraft
    | InterviewPrep
    | ResumeAdvice
    | QuestionAnswers
    | StudioLetterRequest
    | WsTenderReplyRequest
)


async def _reply_tasks(session: AsyncSession, max_chars: int) -> list[tuple[PacketTask, ReplyDraft]]:
    """M6: queued reply drafts → `reply_draft` tasks with the last messages of the negotiation."""
    rows = (
        (await session.execute(select(ReplyDraft).where(ReplyDraft.status == "queued").order_by(ReplyDraft.id)))
        .scalars()
        .all()
    )
    tasks: list[tuple[PacketTask, ReplyDraft]] = []
    for req in rows:
        vacancy = await session.get(Vacancy, req.vacancy_id)
        negotiation = await session.get(Negotiation, req.negotiation_id)
        if vacancy is None or negotiation is None:
            req.status = "cancelled"
            req.error = "вакансия или отклик удалены"
            continue
        messages = (
            (
                await session.execute(
                    select(Message)
                    .where(Message.negotiation_id == negotiation.id)
                    .order_by(desc(Message.hh_created_at), desc(Message.id))
                    .limit(THREAD_MESSAGES)
                )
            )
            .scalars()
            .all()
        )
        thread = [
            {
                "author": m.author_type,
                "name": m.author_name,
                "text": (m.text or "")[:2000],
                "at": (at.isoformat(timespec="minutes") if (at := as_utc(m.hh_created_at)) else None),
            }
            for m in reversed(messages)
        ]
        evaluation = (
            await session.execute(
                select(VacancyEvaluation)
                .where(VacancyEvaluation.vacancy_id == vacancy.id, VacancyEvaluation.is_current.is_(True))
                .limit(1)
            )
        ).scalar_one_or_none()
        tasks.append(
            (
                PacketTask(
                    type="reply_draft",
                    vacancy=packet_vacancy(vacancy, max_chars),
                    want_letter=False,
                    thread=thread,
                    user_intent=req.user_intent,
                    evaluation=(
                        {"score": evaluation.score, "verdict": evaluation.verdict, "summary": evaluation.summary}
                        if evaluation
                        else None
                    ),
                ),
                req,
            )
        )
    return tasks


async def _prep_tasks(session: AsyncSession, max_chars: int) -> list[tuple[PacketTask, InterviewPrep]]:
    """Queued interview preparations → `interview_prep` tasks with the letter sent and the journal thread."""
    rows = (
        (
            await session.execute(
                select(InterviewPrep).where(InterviewPrep.status == "queued").order_by(InterviewPrep.id)
            )
        )
        .scalars()
        .all()
    )
    tasks: list[tuple[PacketTask, InterviewPrep]] = []
    for req in rows:
        vacancy = await session.get(Vacancy, req.vacancy_id)
        app_row = await session.get(Application, req.application_id)
        if vacancy is None or app_row is None:
            req.status = "cancelled"
            req.error = "вакансия или отклик удалены"
            continue
        thread: list[dict[str, Any]] = []
        if req.negotiation_id is not None or app_row.hh_negotiation_id:
            negotiation = (
                await session.get(Negotiation, req.negotiation_id)
                if req.negotiation_id is not None
                else (
                    await session.execute(select(Negotiation).where(Negotiation.hh_id == app_row.hh_negotiation_id))
                ).scalar_one_or_none()
            )
            if negotiation is not None:
                messages = (
                    (
                        await session.execute(
                            select(Message)
                            .where(Message.negotiation_id == negotiation.id)
                            .order_by(desc(Message.hh_created_at), desc(Message.id))
                            .limit(THREAD_MESSAGES)
                        )
                    )
                    .scalars()
                    .all()
                )
                thread = [
                    {
                        "author": m.author_type,
                        "name": m.author_name,
                        "text": (m.text or "")[:2000],
                        "at": (at.isoformat(timespec="minutes") if (at := as_utc(m.hh_created_at)) else None),
                    }
                    for m in reversed(messages)
                ]
        evaluation = (
            await session.execute(
                select(VacancyEvaluation)
                .where(VacancyEvaluation.vacancy_id == vacancy.id, VacancyEvaluation.is_current.is_(True))
                .limit(1)
            )
        ).scalar_one_or_none()
        tasks.append(
            (
                PacketTask(
                    type="interview_prep",
                    vacancy=packet_vacancy(vacancy, max_chars),
                    want_letter=False,
                    thread=thread,
                    letter_sent=(app_row.letter_text or "")[:4000] or None,
                    user_notes=req.user_notes,
                    evaluation=(
                        {
                            "score": evaluation.score,
                            "verdict": evaluation.verdict,
                            "summary": evaluation.summary,
                            "matched": _loads(evaluation.matched_json, []),
                            "gaps": _loads(evaluation.gaps_json, []),
                        }
                        if evaluation
                        else None
                    ),
                ),
                req,
            )
        )
    return tasks


async def _question_tasks(session: AsyncSession, max_chars: int) -> list[tuple[PacketTask, QuestionAnswers]]:
    """Queued employer questions → `answer_questions` tasks with the vacancy, the current letter and the evaluation."""
    from app.services.letters import current_letter

    rows = (
        (
            await session.execute(
                select(QuestionAnswers).where(QuestionAnswers.status == "queued").order_by(QuestionAnswers.id)
            )
        )
        .scalars()
        .all()
    )
    tasks: list[tuple[PacketTask, QuestionAnswers]] = []
    for req in rows:
        vacancy = await session.get(Vacancy, req.vacancy_id)
        if vacancy is None:
            req.status = "cancelled"
            req.error = "вакансия удалена"
            continue
        letter = await current_letter(session, vacancy.id)
        evaluation = (
            await session.execute(
                select(VacancyEvaluation)
                .where(VacancyEvaluation.vacancy_id == vacancy.id, VacancyEvaluation.is_current.is_(True))
                .limit(1)
            )
        ).scalar_one_or_none()
        tasks.append(
            (
                PacketTask(
                    type="answer_questions",
                    vacancy=packet_vacancy(vacancy, max_chars),
                    want_letter=False,
                    questions=_loads(req.questions_json, []) or [req.questions_text],
                    current_letter=(letter.text[:4000] if letter is not None else None),
                    evaluation=(
                        {"score": evaluation.score, "verdict": evaluation.verdict, "summary": evaluation.summary}
                        if evaluation
                        else None
                    ),
                ),
                req,
            )
        )
    return tasks


async def _advice_tasks(session: AsyncSession) -> list[tuple[PacketTask, ResumeAdvice]]:
    """Queued resume advice → `resume_advice` tasks (a stub vacancy `advice-<id>` keys the result)."""
    rows = (
        (await session.execute(select(ResumeAdvice).where(ResumeAdvice.status == "queued").order_by(ResumeAdvice.id)))
        .scalars()
        .all()
    )
    tasks: list[tuple[PacketTask, ResumeAdvice]] = []
    for req in rows:
        demand = _loads(req.demand_json, {}) or {}
        tasks.append(
            (
                PacketTask(
                    type="resume_advice",
                    vacancy=PacketVacancy(
                        id=f"advice-{req.id}",
                        name=f"Правки резюме: {req.segment_label}",
                        employer={"name": "—"},
                        description="Это не вакансия: задача про правки резюме под сегмент, см. segment и demand.",
                    ),
                    want_letter=False,
                    segment={
                        "by": req.segment_by,
                        "key": req.segment_key,
                        "label": req.segment_label,
                        "found": demand.get("found"),
                        "evaluated": demand.get("evaluated"),
                        "good": demand.get("good"),
                    },
                    demand={k: v for k, v in demand.items() if k not in ("found", "evaluated", "good")},
                    user_notes=req.user_notes,
                ),
                req,
            )
        )
    return tasks


async def _studio_tasks(session: AsyncSession, max_chars: int) -> list[tuple[PacketTask, StudioLetterRequest]]:
    """Queued studio letters → `studio_letter` tasks (a stub vacancy `studio-<request id>` keys the result)."""
    from app.services.studios import KIND_LABELS, LETTER_STYLES, packet_studio, relevant_vacancies, studio_settings

    rows = (
        (
            await session.execute(
                select(StudioLetterRequest)
                .where(StudioLetterRequest.status == "queued")
                .order_by(StudioLetterRequest.id)
            )
        )
        .scalars()
        .all()
    )
    settings = await studio_settings(session)
    tasks: list[tuple[PacketTask, StudioLetterRequest]] = []
    for req in rows:
        studio = await session.get(Studio, req.studio_id)
        if studio is None:
            req.status, req.error = "cancelled", "студия удалена"
            continue
        info = packet_studio(studio, await relevant_vacancies(session, studio), settings, req.kind)
        about = "\n".join(x for x in (studio.description or "", studio.site_summary or "") if x)
        tasks.append(
            (
                PacketTask(
                    type="studio_letter",
                    vacancy=PacketVacancy(
                        id=f"studio-{req.id}",
                        name=f"Письмо студии: {studio.name}",
                        employer={"name": studio.name},
                        area=studio.area,
                        alternate_url=studio.site_url,
                        description=about[:max_chars],
                        professional_roles=[KIND_LABELS.get(studio.kind, studio.kind)],
                    ),
                    want_letter=False,
                    studio=info,
                    letter_kind=req.kind,
                    styles=list(LETTER_STYLES),
                    user_notes=req.user_notes,
                ),
                req,
            )
        )
    return tasks


async def _tender_tasks(session: AsyncSession, max_chars: int) -> list[tuple[PacketTask, WsTenderReplyRequest]]:
    """Queued answers to workspace.ru tenders → `tender_reply` tasks (a stub vacancy `tender-<request id>`)."""
    from app.services.ws_tenders import packet_tender, tender_settings

    rows = (
        (
            await session.execute(
                select(WsTenderReplyRequest)
                .where(WsTenderReplyRequest.status == "queued")
                .order_by(WsTenderReplyRequest.id)
            )
        )
        .scalars()
        .all()
    )
    settings = await tender_settings(session)
    tasks: list[tuple[PacketTask, WsTenderReplyRequest]] = []
    for req in rows:
        tender = await session.get(WsTender, req.tender_id)
        if tender is None:
            req.status, req.error = "cancelled", "тендер удалён"
            continue
        tasks.append(
            (
                PacketTask(
                    type="tender_reply",
                    vacancy=PacketVacancy(
                        id=f"tender-{req.id}",
                        name=f"Тендер: {tender.title}"[:500],
                        employer={"name": "заказчик на workspace.ru"},
                        area=tender.city,
                        alternate_url=tender.url,
                        description=(tender.description or "")[:max_chars],
                    ),
                    want_letter=False,
                    tender=packet_tender(tender, settings),
                    user_notes=req.user_notes,
                ),
                req,
            )
        )
    return tasks


async def build_batches(
    session: AsyncSession,
    settings: Settings,
    *,
    vacancy_ids: list[int] | None = None,
    include_revisions: bool = True,
) -> list[ClaudeBatch]:
    """Creates 0..N packets. Vacancies move to awaiting_claude; revision requests to sent_in_batch."""
    profile = await active_profile(session)
    if profile is None:
        raise PacketError("Сначала соберите профиль (вкладка «Профиль»): без profile.md разбор невозможен")
    ensure_guidelines(settings)
    prefs = await get_preferences(session)
    notes = await user_notes(session)
    letter_defaults = {
        "min_chars": prefs.letter_style.min_chars,
        "max_chars": prefs.letter_style.max_chars,
        "tone": prefs.letter_style.tone,
        "greeting": prefs.letter_style.greeting,
        "must_mention": prefs.letter_style.must_mention,
        "avoid": prefs.letter_style.avoid,
        "language": "auto",
    }

    work: list[tuple[PacketTask, Vacancy | None, PacketRequest | None]] = []
    seen_vacancies: set[int] = set()
    for vacancy in await _candidates(session, vacancy_ids):
        task = PacketTask(
            type="evaluate_and_letter",
            vacancy=packet_vacancy(vacancy, settings.batch_desc_max_chars),
            prescore={"value": vacancy.prescore, "notes": _loads(vacancy.prescore_notes_json, [])},
            want_letter=True,
        )
        work.append((task, vacancy, None))
        seen_vacancies.add(vacancy.id)
    if include_revisions:
        for task, req in await _revision_tasks(session, settings.batch_desc_max_chars):
            if req.vacancy_id in seen_vacancies:
                continue  # a fresh evaluate_and_letter already produces a new letter
            work.append((task, None, req))
            seen_vacancies.add(req.vacancy_id)
        for task, draft in await _reply_tasks(session, settings.batch_desc_max_chars):
            if draft.vacancy_id in seen_vacancies:
                continue  # results are keyed by vacancy_id — one task per vacancy per packet
            work.append((task, None, draft))
            seen_vacancies.add(draft.vacancy_id)
        for task, prep in await _prep_tasks(session, settings.batch_desc_max_chars):
            if prep.vacancy_id in seen_vacancies:
                continue
            work.append((task, None, prep))
            seen_vacancies.add(prep.vacancy_id)
        for task, question in await _question_tasks(session, settings.batch_desc_max_chars):
            if question.vacancy_id in seen_vacancies:
                continue  # one result per vacancy per packet: the answers wait for the next one
            work.append((task, None, question))
            seen_vacancies.add(question.vacancy_id)
        for task, advice in await _advice_tasks(session):
            work.append((task, None, advice))
        for task, studio_req in await _studio_tasks(session, settings.batch_desc_max_chars):
            work.append((task, None, studio_req))
        for task, tender_req in await _tender_tasks(session, settings.batch_desc_max_chars):
            work.append((task, None, tender_req))
    if not work:
        return []
    await _assign_profile_files(session, settings, work)

    pending = pending_dir(settings)
    pending.mkdir(parents=True, exist_ok=True)
    size = max(1, settings.batch_size)
    created: list[ClaudeBatch] = []
    for start in range(0, len(work), size):
        chunk = work[start : start + size]
        batch_id = new_batch_id()
        while (pending / f"batch_{batch_id}.json").exists():
            batch_id = new_batch_id()
        packet = Packet(
            protocol_version=PROTOCOL_VERSION,
            batch_id=batch_id,
            created_at=utcnow().isoformat(timespec="seconds"),
            profile_version=profile.version,
            user_notes=notes,
            letter_defaults=letter_defaults,
            tasks=[t for t, _, _ in chunk],
        )
        path = pending / f"batch_{batch_id}.json"
        path.write_text(json.dumps(packet.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        row = ClaudeBatch(
            batch_id=batch_id,
            packet_path=str(path),
            status="pending",
            tasks_count=len(chunk),
            profile_version=profile.version,
            protocol_version=PROTOCOL_VERSION,
        )
        session.add(row)
        for _, vacancy, req in chunk:  # type: ignore[assignment]
            if vacancy is not None:
                vacancy.pipeline_status = "awaiting_claude"
                vacancy.batch_id = batch_id
                vacancy.pipeline_reason = None
            if req is not None:
                req.status = "sent_in_batch"
                req.batch_id = batch_id
        await session.flush()
        await audit(session, "batches", f"Сформирован пакет {batch_id}: задач {len(chunk)}", batch_id=batch_id)
        log.info("batch_created", batch_id=batch_id, tasks=len(chunk))
        created.append(row)
    if created:
        from app.services.counters import bump_counter
        from app.services.notify import emit_event

        await bump_counter(session, settings, "batches_created", len(created))
        await emit_event(
            session,
            settings,
            "batch_ready",
            {"batch_id": created[0].batch_id, "batches": len(created), "tasks": sum(b.tasks_count for b in created)},
        )
    return created


async def cancel_batch(session: AsyncSession, settings: Settings, batch: ClaudeBatch) -> int:
    """Returns pending vacancies to `prescored`, revision requests to `queued`, removes the packet file."""
    if batch.status != "pending":
        raise PacketError("Отменить можно только пакет в статусе pending")
    rows = (await session.execute(select(Vacancy).where(Vacancy.batch_id == batch.batch_id))).scalars().all()
    for v in rows:
        if v.pipeline_status == "awaiting_claude":
            v.pipeline_status = "prescored"
        v.batch_id = None
    reqs = (
        (await session.execute(select(LetterRevisionRequest).where(LetterRevisionRequest.batch_id == batch.batch_id)))
        .scalars()
        .all()
    )
    for r in reqs:
        if r.status == "sent_in_batch":
            r.status = "queued"
            r.batch_id = None
    for draft in (await session.execute(select(ReplyDraft).where(ReplyDraft.batch_id == batch.batch_id))).scalars():
        if draft.status == "sent_in_batch":
            draft.status, draft.batch_id = "queued", None
    for prep in (
        await session.execute(select(InterviewPrep).where(InterviewPrep.batch_id == batch.batch_id))
    ).scalars():
        if prep.status == "sent_in_batch":
            prep.status, prep.batch_id = "queued", None
    for adv in (await session.execute(select(ResumeAdvice).where(ResumeAdvice.batch_id == batch.batch_id))).scalars():
        if adv.status == "sent_in_batch":
            adv.status, adv.batch_id = "queued", None
    for qa in (
        await session.execute(select(QuestionAnswers).where(QuestionAnswers.batch_id == batch.batch_id))
    ).scalars():
        if qa.status == "sent_in_batch":
            qa.status, qa.batch_id = "queued", None
    for sl in (
        await session.execute(select(StudioLetterRequest).where(StudioLetterRequest.batch_id == batch.batch_id))
    ).scalars():
        if sl.status == "sent_in_batch":
            sl.status, sl.batch_id = "queued", None
    for tr in (
        await session.execute(select(WsTenderReplyRequest).where(WsTenderReplyRequest.batch_id == batch.batch_id))
    ).scalars():
        if tr.status == "sent_in_batch":
            tr.status, tr.batch_id = "queued", None
    # a packet left in pending/ would still be taken by the background claude -p (the stored path may be another
    # machine's: services/relocate.py)
    packet_file = local_path(batch.packet_path, pending_dir(settings))
    if packet_file is not None:
        packet_file.unlink(missing_ok=True)
    batch.status = "cancelled"
    await session.flush()
    await audit(session, "batches", f"Пакет {batch.batch_id} отменён", batch_id=batch.batch_id)
    return len(rows)


async def cancel_pending_batches(session: AsyncSession, settings: Settings) -> int:
    """Cancels every pending packet; returns how many were cancelled."""
    rows = (
        (await session.execute(select(ClaudeBatch).where(ClaudeBatch.status == "pending").order_by(ClaudeBatch.id)))
        .scalars()
        .all()
    )
    cancelled = 0
    for row in rows:
        try:
            await cancel_batch(session, settings, row)
        except PacketError:
            continue
        cancelled += 1
    return cancelled


def batch_row(batch: ClaudeBatch) -> dict[str, Any]:
    created = as_utc(batch.created_at)
    imported = as_utc(batch.imported_at)
    return {
        "id": batch.id,
        "batch_id": batch.batch_id,
        "status": batch.status,
        "tasks_count": batch.tasks_count,
        "results_count": batch.results_count,
        "profile_version": batch.profile_version,
        "packet_path": batch.packet_path,
        "packet_exists": Path(batch.packet_path).exists(),
        "result_path": batch.result_path,
        "created_at": created.isoformat(timespec="seconds") if created else None,
        "imported_at": imported.isoformat(timespec="seconds") if imported else None,
        "error": batch.error,
        "summary": _loads(batch.summary_json, None),
    }
