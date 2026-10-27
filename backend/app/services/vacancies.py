"""Vacancy listing, detail and user actions for the «Новые вакансии» tab."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import (
    Application,
    AuditLog,
    ClaudeBatch,
    CoverLetter,
    HiddenEmployer,
    SavedSearch,
    Vacancy,
    VacancyEvaluation,
)
from app.services.audit import about_vacancy, audit
from app.services.letters import (
    current_letter,
    current_letters_map,
    letter_row,
    letter_status,
    letter_versions,
    pending_revision,
)
from app.services.preferences import get_preferences
from app.services.profile import active_profile
from app.services.questions import list_answers
from app.services.tagging import load_tags, merge_tags
from app.services.textmatch import terms_equal

CANDIDATE_STATUSES = ["prescored", "awaiting_claude", "evaluated", "suitable", "maybe", "letter_ready"]
GROUPS: dict[str, list[str] | None] = {
    "candidates": CANDIDATE_STATUSES,
    "suitable": ["suitable", "letter_ready"],
    "maybe": ["maybe"],
    "prescored": ["prescored", "awaiting_claude"],
    "awaiting_claude": ["awaiting_claude"],
    "evaluated": ["suitable", "maybe", "letter_ready", "evaluated"],
    "needs_manual": ["needs_manual"],
    "not_suitable": ["not_suitable"],
    "filtered_out": ["filtered_out"],
    "skipped": ["skipped"],
    "hidden": ["hidden"],
    "already_applied": ["already_applied"],
    "queued": ["queued"],
    "sent": ["sent"],
    "new": ["new"],
    "watched": ["watched"],
    "all": None,
    # letters with [[УТОЧНИТЬ]] markers or open clarifications that can still go out (_clarify_condition)
    "needs_clarification": None,
}
# where a letter can still be sent from: the candidates, the apply queue, a vacancy waiting for a manual step on hh.
# A sent, skipped or rejected vacancy keeps its letter, but the question in it no longer waits for the user.
CLARIFY_STATUSES = [*CANDIDATE_STATUSES, "queued", "needs_manual"]


def _clarify_condition() -> Any:
    """Vacancies whose current letter still asks the user something and can still be sent (CLARIFY_STATUSES)."""
    letters = (
        select(CoverLetter.vacancy_id)
        .where(
            CoverLetter.is_current.is_(True),
            or_(
                CoverLetter.text.like("%[[УТОЧНИТЬ%"),
                CoverLetter.text.like("%[[ уточнить%"),
                and_(
                    CoverLetter.needs_clarification_json.isnot(None),
                    CoverLetter.needs_clarification_json.notin_(("[]", "null", "")),
                ),
            ),
        )
        .scalar_subquery()
    )
    return and_(Vacancy.pipeline_status.in_(CLARIFY_STATUSES), Vacancy.id.in_(letters))


STATUS_LABELS = {
    "new": "не обработана",
    "filtered_out": "отфильтрована правилами",
    "prescored": "кандидат (ждёт разбора)",
    "awaiting_claude": "в пакете для Claude",
    "evaluated": "разобрана",
    "suitable": "подходит",
    "maybe": "возможно",
    "not_suitable": "не подходит",
    "letter_ready": "письмо готово",
    "queued": "в очереди на отклик",
    "sent": "отклик отправлен",
    "needs_manual": "нужно действие на hh.ru",
    "skipped": "пропущена",
    "hidden": "скрыта",
    "already_applied": "уже есть отклик",
    "watched": "наблюдение (без разбора)",
}


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def _iso(dt: datetime | None) -> str | None:
    d = as_utc(dt)
    return d.isoformat(timespec="seconds") if d else None


def _search_ids(row: Vacancy) -> list[int]:
    return [int(x) for x in (row.source_search_ids or "").split(",") if x.strip().isdigit()]


def vacancy_row(
    row: Vacancy,
    search_names: dict[int, str],
    letter: Any = None,
    verdict: str | None = None,
    profile_names: dict[int, str] | None = None,
) -> dict[str, Any]:
    flags = _loads(row.rules_flags_json, [])
    pnames = profile_names or {}
    return {
        "id": row.id,
        "hh_id": row.hh_id,
        "name": row.name,
        "employer": {
            "id": row.employer_id,
            "name": row.employer_name,
            "accredited_it": row.employer_accredited_it,
            "trusted": row.employer_trusted,
        },
        "area_name": row.area_name,
        "address": row.address,
        "metro": _loads(row.metro_json, []),
        "salary": {
            "from": row.salary_from,
            "to": row.salary_to,
            "currency": row.salary_currency,
            "gross": row.salary_gross,
            "net_estimate": row.salary_net_estimate,
            "net_rur": row.salary_net_rur,
        },
        "experience_name": row.experience_name,
        "employment_form_name": row.employment_form_name,
        "work_formats": _loads(row.work_format_names_json, []),
        "work_format_ids": _loads(row.work_format_ids_json, []),
        "published_at": _iso(row.published_at),
        "first_seen_at": _iso(row.first_seen_at),
        "responses_count": row.responses_count,
        "duplicate_of_id": row.duplicate_of_id,
        "sources": [search_names.get(i, f"#{i}") for i in _search_ids(row)],
        "flags": flags,
        "pipeline_status": row.pipeline_status,
        "status_label": STATUS_LABELS.get(row.pipeline_status, row.pipeline_status),
        "pipeline_reason": row.pipeline_reason,
        "prescore": row.prescore,
        "score": row.score,
        "verdict": verdict,
        "letter_status": letter_status(letter),
        "batch_id": row.batch_id,
        "selection_profile_id": row.selection_profile_id,
        "selection_profile_name": pnames.get(row.selection_profile_id) if row.selection_profile_id else None,
        "alternate_url": row.alternate_url,
        "apply_alternate_url": row.apply_alternate_url,
        "is_new": row.seen_by_user_at is None,
        "snoozed_until": _iso(row.snoozed_until),
        "hidden": row.hidden,
        "user_override": row.user_override,
        "archived": row.archived,
        "has_full": bool(row.raw_full_json),
    }


async def search_names(session: AsyncSession) -> dict[int, str]:
    rows = (await session.execute(select(SavedSearch.id, SavedSearch.name))).all()
    return {int(i): str(n) for i, n in rows}


# ---- listing ----------------------------------------------------------------


def _apply_filters(
    stmt: Select[Any],
    *,
    group: str,
    min_score: int | None,
    search_id: int | None,
    area: str | None,
    work_format: str | None,
    salary_from: int | None,
    q: str | None,
    include_snoozed: bool,
    unseen_only: bool,
    selection_profile_id: int | None = None,
    experience: list[str] | None = None,
) -> Select[Any]:
    statuses = GROUPS.get(group, CANDIDATE_STATUSES)
    if selection_profile_id is not None:
        # 0 = base questionnaire (NULL), otherwise the named set
        stmt = stmt.where(
            Vacancy.selection_profile_id.is_(None)
            if selection_profile_id == 0
            else Vacancy.selection_profile_id == selection_profile_id
        )
    if statuses is not None:
        stmt = stmt.where(Vacancy.pipeline_status.in_(statuses))
    if group == "needs_clarification":
        stmt = stmt.where(_clarify_condition())
    if group not in ("hidden", "all"):
        stmt = stmt.where(Vacancy.hidden.is_(False))
    if not include_snoozed:
        stmt = stmt.where(or_(Vacancy.snoozed_until.is_(None), Vacancy.snoozed_until <= utcnow()))
    if min_score is not None:
        stmt = stmt.where(func.coalesce(Vacancy.score, Vacancy.prescore, 0) >= min_score)
    if search_id is not None:
        stmt = stmt.where(Vacancy.source_search_ids.like(f"%,{search_id},%"))
    # typed text (the region here, q below): icontains is lower(x) LIKE lower(y) with the Unicode lower() of
    # db/session.unicode_lower, so «москва» finds «Москва»; autoescape keeps «%» and «_» of the input literal
    area = (area or "").strip()
    if area:
        stmt = stmt.where(Vacancy.area_name.icontains(area, autoescape=True))
    if work_format:
        stmt = stmt.where(Vacancy.work_format_ids_json.contains(f'"{work_format}"', autoescape=True))
    if experience:
        # hh experience ids (noExperience, between1And3, …); a vacancy without the field is left out
        stmt = stmt.where(Vacancy.experience_id.in_(experience))
    if salary_from is not None:
        stmt = stmt.where(Vacancy.salary_net_rur >= salary_from)
    q = (q or "").strip()
    if q:
        stmt = stmt.where(
            or_(Vacancy.name.icontains(q, autoescape=True), Vacancy.employer_name.icontains(q, autoescape=True))
        )
    if unseen_only:
        stmt = stmt.where(Vacancy.seen_by_user_at.is_(None))
    return stmt


async def list_vacancies(
    session: AsyncSession,
    *,
    group: str = "candidates",
    min_score: int | None = None,
    search_id: int | None = None,
    area: str | None = None,
    work_format: str | None = None,
    salary_from: int | None = None,
    q: str | None = None,
    include_snoozed: bool = False,
    unseen_only: bool = False,
    sort: str = "score",
    page: int = 0,
    per_page: int = 50,
    selection_profile_id: int | None = None,
    experience: list[str] | None = None,
) -> dict[str, Any]:
    filters = dict(
        group=group,
        min_score=min_score,
        search_id=search_id,
        area=area,
        work_format=work_format,
        salary_from=salary_from,
        q=q,
        include_snoozed=include_snoozed,
        unseen_only=unseen_only,
        selection_profile_id=selection_profile_id,
        experience=experience,
    )
    count_stmt = _apply_filters(select(func.count(Vacancy.id)), **filters)  # type: ignore[arg-type]
    total = int((await session.execute(count_stmt)).scalar() or 0)

    # the ids of the page first, then those rows: sorting whole rows made SQLite carry every ~46 KB payload through
    # the sorter (500 ms on 10 000 vacancies); the ids come from the covering index ix_vacancies_listing
    stmt = _apply_filters(select(Vacancy.id), **filters)  # type: ignore[arg-type]
    if sort == "published":
        stmt = stmt.order_by(desc(Vacancy.published_at), desc(Vacancy.id))
    elif sort == "responses":
        stmt = stmt.order_by(Vacancy.responses_count.asc().nulls_last(), desc(Vacancy.published_at), desc(Vacancy.id))
    elif sort == "salary":
        stmt = stmt.order_by(Vacancy.salary_net_rur.desc().nulls_last(), desc(Vacancy.published_at), desc(Vacancy.id))
    else:
        stmt = stmt.order_by(
            func.coalesce(Vacancy.score, Vacancy.prescore, -1).desc(), desc(Vacancy.published_at), desc(Vacancy.id)
        )
    page_ids = list((await session.execute(stmt.offset(page * per_page).limit(per_page))).scalars())
    by_id = {r.id: r for r in (await session.execute(select(Vacancy).where(Vacancy.id.in_(page_ids)))).scalars()}
    rows = [by_id[i] for i in page_ids if i in by_id]
    names = await search_names(session)
    from app.services.selection import profile_names as _profile_names

    pnames = await _profile_names(session)
    ids = [r.id for r in rows]
    letters = await current_letters_map(session, ids)
    verdicts = await current_verdicts(session, ids)
    return {
        "items": [vacancy_row(r, names, letters.get(r.id), verdicts.get(r.id), pnames) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "counts": await status_counts(session),
    }


async def current_verdicts(session: AsyncSession, ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    rows = await session.execute(
        select(VacancyEvaluation.vacancy_id, VacancyEvaluation.verdict).where(
            VacancyEvaluation.vacancy_id.in_(ids), VacancyEvaluation.is_current.is_(True)
        )
    )
    return {int(v): str(verdict) for v, verdict in rows.all()}


async def current_evaluation(session: AsyncSession, vacancy_id: int) -> VacancyEvaluation | None:
    return (
        await session.execute(
            select(VacancyEvaluation)
            .where(VacancyEvaluation.vacancy_id == vacancy_id, VacancyEvaluation.is_current.is_(True))
            .order_by(desc(VacancyEvaluation.id))
            .limit(1)
        )
    ).scalar_one_or_none()


def evaluation_row(ev: VacancyEvaluation, active_version: int | None) -> dict[str, Any]:
    created = as_utc(ev.created_at)
    return {
        "id": ev.id,
        "batch_id": ev.batch_id,
        "score": ev.score,
        "verdict": ev.verdict,
        "summary": ev.summary,
        "matched": _loads(ev.matched_json, []),
        "gaps": _loads(ev.gaps_json, []),
        "red_flags": _loads(ev.red_flags_json, []),
        "salary_fit": ev.salary_fit,
        "seniority_fit": ev.seniority_fit,
        "language": ev.language,
        "tags": load_tags(ev.tags_json),
        "profile_version": ev.profile_version,
        "stale_profile": bool(active_version is not None and ev.profile_version not in (None, active_version)),
        "user_feedback": ev.user_feedback,
        "user_comment": ev.user_comment,
        "created_at": created.isoformat(timespec="seconds") if created else None,
    }


async def status_counts(session: AsyncSession) -> dict[str, int]:
    """Counters of the tabs and the menu. One pass over the covering index ix_vacancies_listing (status, hidden,
    snoozed_until, seen_by_user_at, …) — the table itself is not read; the letters needing clarification come from
    cover_letters."""
    now = utcnow()
    visible = and_(Vacancy.hidden.is_(False), or_(Vacancy.snoozed_until.is_(None), Vacancy.snoozed_until <= now))

    def n(cond: Any) -> Any:
        return func.coalesce(func.sum(case((cond, 1), else_=0)), 0)

    rows = (
        await session.execute(
            select(
                Vacancy.pipeline_status,
                func.count(),
                n(visible),
                n(Vacancy.hidden.is_(True)),
                n(Vacancy.snoozed_until > now),
                n(and_(visible, Vacancy.seen_by_user_at.is_(None))),
            ).group_by(Vacancy.pipeline_status)
        )
    ).all()
    by_status = {str(status): int(vis) for status, _total, vis, _h, _s, _u in rows if vis}
    counts: dict[str, int] = {"by_status": by_status}  # type: ignore[dict-item]
    for name, statuses in GROUPS.items():
        if statuses is None:
            continue
        counts[name] = sum(by_status.get(s, 0) for s in statuses)
    counts["hidden"] = sum(int(r[3]) for r in rows)
    counts["snoozed"] = sum(int(r[4]) for r in rows)
    counts["unseen_candidates"] = sum(int(r[5]) for r in rows if r[0] in CANDIDATE_STATUSES)
    counts["needs_clarification"] = int(
        (await session.execute(select(func.count(Vacancy.id)).where(visible, _clarify_condition()))).scalar() or 0
    )
    counts["total"] = sum(int(r[1]) for r in rows)
    return counts


# ---- detail -----------------------------------------------------------------


async def vacancy_detail(session: AsyncSession, row: Vacancy) -> dict[str, Any]:
    names = await search_names(session)
    from app.services.selection import profile_names as _profile_names

    letter = await current_letter(session, row.id)
    evaluation = await current_evaluation(session, row.id)
    data = vacancy_row(row, names, letter, evaluation.verdict if evaluation else None, await _profile_names(session))
    profile = await active_profile(session)
    revision = await pending_revision(session, row.id)
    prefs = await get_preferences(session)
    profile_terms = [s.name for s in prefs.skills_weights]
    key_skills = _loads(row.key_skills_json, [])
    history = (
        (await session.execute(select(AuditLog).where(about_vacancy(row)).order_by(desc(AuditLog.id)).limit(30)))
        .scalars()
        .all()
    )
    data.update(
        {
            "description_html": row.description_html,
            "description_text": row.description_text,
            "key_skills": [{"name": k, "matched": any(terms_equal(k, t) for t in profile_terms)} for k in key_skills],
            "professional_roles": _loads(row.professional_roles_json, []),
            "tags": merge_tags(load_tags(row.tags_json), load_tags(evaluation.tags_json) if evaluation else {}),
            "rules_reasons": _loads(row.rules_reasons_json, []),
            "prescore_notes": _loads(row.prescore_notes_json, []),
            "relations": _loads(row.relations_json, []),
            "response_letter_required": row.response_letter_required,
            "has_test": row.has_test,
            "test_required": row.test_required,
            "type_id": row.type_id,
            "response_url": row.response_url,
            "full_fetched_at": _iso(row.full_fetched_at),
            "skip_reason": row.skip_reason,
            "question_answers": await list_answers(session, row.id),
            "evaluation": evaluation_row(evaluation, profile.version if profile else None) if evaluation else None,
            "letter": letter_row(letter) if letter else None,
            "letters": [
                {
                    "id": lt.id,
                    "version": lt.version,
                    "origin": lt.origin,
                    "char_count": lt.char_count,
                    "is_current": lt.is_current,
                    "created_at": _iso(lt.created_at),
                }
                for lt in await letter_versions(session, row.id)
            ],
            "revision_request": (
                {
                    "id": revision.id,
                    "status": revision.status,
                    "comment": revision.user_comment,
                    "batch_id": revision.batch_id,
                }
                if revision
                else None
            ),
            "history": [{"ts": _iso(h.ts), "level": h.level, "area": h.area, "message": h.message} for h in history],
        }
    )
    return data


# ---- actions ----------------------------------------------------------------


class VacancyActionError(Exception):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


EMPLOYER_HIDDEN_REASON = "работодатель скрыт в панели"
# an application in one of these statuses means the response is sent or on its way (vacancy status = the value)
APPLICATION_VACANCY_STATUS = {"sent": "sent", "queued": "queued", "sending": "queued", "needs_manual": "needs_manual"}


def verdict_status(score: int, settings: Settings | None) -> str:
    """Pipeline status for a Claude score (the same thresholds as the importer)."""
    suitable = settings.score_threshold_suitable if settings is not None else 70
    maybe = settings.score_threshold_maybe if settings is not None else 50
    if score >= suitable:
        return "suitable"
    if score >= maybe:
        return "maybe"
    return "not_suitable"


async def open_application(session: AsyncSession, vacancy_id: int) -> Application | None:
    """The newest application of the vacancy that is sent or on its way (queued, sending, assist page open)."""
    return (
        await session.execute(
            select(Application)
            .where(Application.vacancy_id == vacancy_id, Application.status.in_(tuple(APPLICATION_VACANCY_STATUS)))
            .order_by(desc(Application.id))
            .limit(1)
        )
    ).scalar_one_or_none()


async def revived_status(session: AsyncSession, row: Vacancy, settings: Settings | None, *, fallback: str) -> str:
    """Where a skipped/hidden vacancy comes back to: its application's stage if there is one; `awaiting_claude` while
    the packet it was hidden in still waits for /hh-batch (a second packet would evaluate it twice); else the current
    Claude verdict (a restore must not send an evaluated vacancy to Claude again); else `fallback`."""
    app_row = await open_application(session, row.id)
    if app_row is not None:
        return APPLICATION_VACANCY_STATUS[app_row.status]
    if row.batch_id:
        batch_status = (
            await session.execute(select(ClaudeBatch.status).where(ClaudeBatch.batch_id == row.batch_id))
        ).scalar_one_or_none()
        if batch_status == "pending":
            return "awaiting_claude"
        row.batch_id = None
    ev = await current_evaluation(session, row.id)
    if ev is not None:
        return verdict_status(int(ev.score), settings)
    return fallback


async def _cancel_queued(session: AsyncSession, row: Vacancy, why: str) -> None:
    """Skipping or hiding a vacancy withdraws its application that the API queue has not sent yet (mode api)."""
    queued = (
        await session.execute(
            select(Application).where(Application.vacancy_id == row.id, Application.status == "queued")
        )
    ).scalars()
    for app_row in queued:
        app_row.status = "cancelled"
        app_row.error_text = f"отменён: вакансия {why}"
        await audit(
            session,
            "apply",
            f"Отклик #{app_row.id} отменён: вакансия {why}",
            vacancy_id=row.id,
            application_id=app_row.id,
        )


async def _resolve_bot_message(session: AsyncSession, row: Vacancy, resolution: str) -> None:
    from app.services.tg_letters import mark_resolved

    await mark_resolved(session, row.id, resolution)


async def skip(session: AsyncSession, row: Vacancy, reason: str | None) -> None:
    await _cancel_queued(session, row, "пропущена")
    row.pipeline_status = "skipped"
    row.skip_reason = (reason or "").strip() or None
    row.pipeline_reason = f"пропущена пользователем{': ' + row.skip_reason if row.skip_reason else ''}"
    await audit(session, "vacancies", f"Пропущена «{row.name}»", vacancy_id=row.id, reason=row.skip_reason)
    await _resolve_bot_message(session, row, "skipped")


async def snooze(session: AsyncSession, row: Vacancy, days: int) -> None:
    days = max(1, min(int(days), 60))
    row.snoozed_until = utcnow() + timedelta(days=days)
    await audit(session, "vacancies", f"Отложена «{row.name}» на {days} дн.", vacancy_id=row.id)
    await _resolve_bot_message(session, row, "snoozed")


async def unsnooze(session: AsyncSession, row: Vacancy) -> None:
    row.snoozed_until = None
    await audit(session, "vacancies", f"Возвращена из отложенных «{row.name}»", vacancy_id=row.id)


async def hide(session: AsyncSession, row: Vacancy) -> None:
    await _cancel_queued(session, row, "скрыта")
    row.hidden = True
    row.pipeline_status = "hidden"
    row.pipeline_reason = "скрыта пользователем"
    await audit(session, "vacancies", f"Скрыта «{row.name}»", vacancy_id=row.id)
    await _resolve_bot_message(session, row, "hidden")


ALREADY_ON_HH = "already_applied"


async def restore(session: AsyncSession, row: Vacancy, settings: Settings | None = None) -> None:
    """Brings a filtered/skipped/hidden vacancy back and protects it from the rules. A vacancy Claude has already
    evaluated returns to its verdict (not to `prescored`, which would re-pack it for Claude and write a second
    letter), one with an application — to the application's stage. One hh.ru already has a response to (the
    relation, the negotiations sync) stays: a candidate there would only get a letter nobody can send."""
    if row.pipeline_status == ALREADY_ON_HH:
        raise VacancyActionError("На hh.ru по вакансии уже есть отклик — вернуть её в кандидаты нельзя")
    fallback = "prescored" if row.prescore is not None else "new"
    row.hidden = False
    row.snoozed_until = None
    row.user_override = True
    row.duplicate_of_id = None  # the user decided it is not a duplicate (or wants it anyway)
    row.claude_attempts = 0
    row.pipeline_status = await revived_status(session, row, settings, fallback=fallback)
    row.pipeline_reason = "оставлена пользователем"
    await audit(session, "vacancies", f"Возвращена в кандидаты «{row.name}»", vacancy_id=row.id)


async def hide_employer(session: AsyncSession, row: Vacancy) -> int:
    if row.employer_id:
        existing = (
            await session.execute(select(HiddenEmployer).where(HiddenEmployer.employer_id == row.employer_id))
        ).scalar_one_or_none()
        if existing is None:
            session.add(HiddenEmployer(employer_id=row.employer_id, employer_name=row.employer_name))
        cond = Vacancy.employer_id == row.employer_id
    else:
        session.add(HiddenEmployer(employer_id=None, employer_name=row.employer_name))
        cond = Vacancy.employer_name == row.employer_name
    affected = (
        (
            await session.execute(
                select(Vacancy).where(
                    cond, Vacancy.pipeline_status.in_(CANDIDATE_STATUSES + ["new", "needs_manual", "queued"])
                )
            )
        )
        .scalars()
        .all()
    )
    for v in affected:
        await _cancel_queued(session, v, "скрыта вместе с работодателем")
        v.hidden = True
        v.pipeline_status = "hidden"
        v.pipeline_reason = EMPLOYER_HIDDEN_REASON
    if row.pipeline_status != "hidden":
        await _cancel_queued(session, row, "скрыта вместе с работодателем")
        row.hidden = True
        row.pipeline_status = "hidden"
        row.pipeline_reason = EMPLOYER_HIDDEN_REASON
    await session.flush()
    await audit(
        session, "vacancies", f"Скрыт работодатель «{row.employer_name}» ({len(affected)} вакансий)", vacancy_id=row.id
    )
    return len(affected)


async def list_hidden_employers(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (await session.execute(select(HiddenEmployer).order_by(desc(HiddenEmployer.id)))).scalars().all()
    return [
        {"id": r.id, "employer_id": r.employer_id, "employer_name": r.employer_name, "created_at": _iso(r.created_at)}
        for r in rows
    ]


async def unhide_employer(session: AsyncSession, pk: int, settings: Settings | None = None) -> int | None:
    """Removes the employer from the hidden list and brings back the vacancies hidden together with it (not those
    the user hid one by one) through revived_status: evaluated ones to their verdict, pre-scored candidates to
    `prescored`, never processed ones to `new` (the next scan runs the rules on them). Returns the number of vacancies
    brought back, None when the record is unknown."""
    row = await session.get(HiddenEmployer, pk)
    if row is None:
        return None
    cond = Vacancy.employer_id == row.employer_id if row.employer_id else Vacancy.employer_name == row.employer_name
    hidden = (
        (
            await session.execute(
                select(Vacancy).where(
                    cond, Vacancy.pipeline_status == "hidden", Vacancy.pipeline_reason == EMPLOYER_HIDDEN_REASON
                )
            )
        )
        .scalars()
        .all()
    )
    for v in hidden:
        v.hidden = False
        v.pipeline_status = await revived_status(
            session, v, settings, fallback="prescored" if v.prescore is not None else "new"
        )
        v.pipeline_reason = "работодатель снова виден"
    await session.delete(row)
    await audit(session, "vacancies", f"Работодатель «{row.employer_name}» снова виден ({len(hidden)} вакансий)")
    return len(hidden)


async def to_batch(session: AsyncSession, row: Vacancy) -> None:
    """Queue for the next Claude packet: re-evaluation or a manual send of a filtered/low-scored vacancy."""
    if row.pipeline_status == "awaiting_claude":
        return
    app_row = await open_application(session, row.id)
    if app_row is not None:
        stage = {"sent": "отправлен", "needs_manual": "открыт на hh.ru и ждёт подтверждения"}.get(
            app_row.status, "в очереди на отправку"
        )
        raise VacancyActionError(f"По вакансии уже есть отклик ({stage}) — новый разбор и письмо не нужны")
    if row.pipeline_status == ALREADY_ON_HH:
        raise VacancyActionError("На hh.ru по вакансии уже есть отклик — новый разбор и письмо не нужны")
    if row.pipeline_status in ("filtered_out", "not_suitable", "skipped", "hidden", "needs_manual"):
        row.user_override = True
    row.hidden = False
    row.claude_attempts = 0
    row.pipeline_status = "prescored"
    row.pipeline_reason = "отправлена в разбор пользователем"
    row.batch_id = None
    await audit(session, "vacancies", f"В пакет для Claude: «{row.name}»", vacancy_id=row.id)


async def set_feedback(session: AsyncSession, row: Vacancy, feedback: str, comment: str | None) -> bool:
    ev = await current_evaluation(session, row.id)
    if ev is None:
        return False
    ev.user_feedback = feedback
    ev.user_comment = (comment or "").strip() or None
    await session.flush()
    label = "верна" if feedback == "ok" else "неверна"
    message = f"Оценка «{row.name}» отмечена как {label}"
    await audit(session, "vacancies", message, vacancy_id=row.id, comment=ev.user_comment)
    return True


async def mark_seen(session: AsyncSession, ids: list[int] | None) -> int:
    stmt = select(Vacancy).where(Vacancy.seen_by_user_at.is_(None))
    if ids:
        stmt = stmt.where(Vacancy.id.in_(ids))
    else:
        stmt = stmt.where(Vacancy.pipeline_status.in_(CANDIDATE_STATUSES))
    rows = (await session.execute(stmt)).scalars().all()
    now = utcnow()
    for r in rows:
        r.seen_by_user_at = now
    return len(rows)
