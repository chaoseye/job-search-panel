"""The board «Воронка»: the vacancy pipeline as columns —
what waits for the user's decision, a response opened on hh.ru that waits for the confirmation, sent, viewed by the
employer, answered — with the counts, the first cards of each column and the 30-day funnel above them; a column opens
as a list (`column_items`). Everything comes from what the panel already keeps: vacancy statuses, applications and
negotiations (synced from hh.ru or the manual journal). Nothing new is stored, the actions are the usual endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Select, and_, case, desc, exists, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import Application, Negotiation, Vacancy, VacancyEvaluation
from app.services.applier import application_row, local_day_bounds
from app.services.letters import current_letters_map
from app.services.selection import profile_names
from app.services.stats import _load_sent, resolve_period
from app.services.vacancies import _clarify_condition, current_verdicts, search_names, vacancy_row

COLUMNS: tuple[tuple[str, str], ...] = (
    ("new", "Новые"),
    ("confirm", "Ждут подтверждения"),
    ("sent", "Отправлено"),
    ("viewed", "Смотрели"),
    ("answer", "Ответ"),
)
COLUMN_KEYS: tuple[str, ...] = tuple(key for key, _ in COLUMNS)
# the bands of «Новые»: Claude's verdict (`hi` suitable, `mid` maybe — the importer's thresholds), the evaluated ones
# whose letter still asks the user something (`clarify`, across hi and mid), rejected by Claude (`low`, hidden behind a
# tile), not evaluated yet (`wait`), and vacancies that want a step on hh.ru while no response is open for them
# (`manual`: a required test in the API mode, Claude failed three times)
BANDS: tuple[str, ...] = ("hi", "mid", "clarify", "low", "wait", "manual")
DECIDE_BANDS = ("hi", "mid")  # what «Новые» counts: evaluated, worth a decision
WAIT_STATUSES = ("prescored", "awaiting_claude")
# a response on its way: the hh.ru page is open and waits for «Да, отправлено», or the API queue holds it
OPEN_APPLICATION = ("needs_manual", "queued", "sending")
CONFIRM_STATUSES = ("needs_manual", "queued")
# cards a column shows on the board before «Ещё N»; «Новые» shows its summary and the two best ones
PREVIEW = {"new": 2, "confirm": 4, "sent": 4, "viewed": 4, "answer": 4}
FUNNEL_PERIOD = "30d"


def _iso(dt: datetime | None) -> str | None:
    d = as_utc(dt)
    return d.isoformat(timespec="seconds") if d else None


def _visible(now: datetime) -> Any:
    """Not hidden and not put off: the default of the «Вакансии» list too."""
    return and_(Vacancy.hidden.is_(False), or_(Vacancy.snoozed_until.is_(None), Vacancy.snoozed_until <= now))


def _open_application_exists() -> Any:
    return exists().where(Application.vacancy_id == Vacancy.id, Application.status.in_(OPEN_APPLICATION))


def band_condition(band: str) -> Any:
    if band == "hi":
        return Vacancy.pipeline_status.in_(("suitable", "letter_ready"))
    if band == "mid":
        return Vacancy.pipeline_status == "maybe"
    if band == "clarify":
        return and_(or_(band_condition("hi"), band_condition("mid")), _clarify_condition())
    if band == "low":
        return and_(Vacancy.pipeline_status == "not_suitable", Vacancy.score.isnot(None))
    if band == "wait":
        return Vacancy.pipeline_status.in_(WAIT_STATUSES)
    if band == "manual":
        return and_(Vacancy.pipeline_status == "needs_manual", not_(_open_application_exists()))
    raise ValueError(f"unknown band: {band}")


# ---- the response stages (synced negotiations or the manual journal) --------------------------------------------


def _invited() -> Any:
    return or_(func.coalesce(Negotiation.state_id, "") == "invitation", Negotiation.invited_at.isnot(None))


def _discarded() -> Any:
    return or_(func.coalesce(Negotiation.state_id, "") == "discard", Negotiation.discarded_at.isnot(None))


def _answered() -> Any:
    """Invitation, rejection or a message from the employer. The negotiation is outer-joined: every part is a plain
    true/false even without one (a NULL would make NOT(...) drop the row)."""
    return or_(_invited(), _discarded(), Negotiation.last_employer_message_at.isnot(None))


def _viewed() -> Any:
    return or_(func.coalesce(Negotiation.viewed_by_opponent, False).is_(True), Negotiation.first_viewed_at.isnot(None))


def _stage_condition(key: str) -> Any:
    if key == "answer":
        return _answered()
    if key == "viewed":
        return and_(_viewed(), not_(_answered()))
    return and_(not_(_viewed()), not_(_answered()))  # sent


def _reaction_at() -> Any:
    return func.coalesce(
        Negotiation.first_reaction_at,
        Negotiation.invited_at,
        Negotiation.discarded_at,
        Negotiation.last_employer_message_at,
    )


def _sent_application_ids() -> Select[Any]:
    """The sent application of each vacancy (the newest, should there ever be two)."""
    return (
        select(func.max(Application.id))
        .join(Vacancy, Vacancy.id == Application.vacancy_id)
        .where(Application.status == "sent")
        .group_by(Application.vacancy_id)
    )


def _confirm_application_ids() -> Select[Any]:
    """The open application of each vacancy that waits at the response step. The vacancy's status says the step is
    still current: an assist page left open for a vacancy hh.ru has since closed (freshness → `filtered_out`) or the
    user skipped does not belong here."""
    return (
        select(func.max(Application.id))
        .join(Vacancy, Vacancy.id == Application.vacancy_id)
        .where(
            Application.status.in_(OPEN_APPLICATION),
            Vacancy.pipeline_status.in_(CONFIRM_STATUSES),
            Vacancy.hidden.is_(False),
        )
        .group_by(Application.vacancy_id)
    )


def _outcome(neg: Negotiation | None) -> str | None:
    if neg is None:
        return None
    if neg.state_id == "invitation" or neg.invited_at is not None:
        return "invitation"
    if neg.state_id == "discard" or neg.discarded_at is not None:
        return "discard"
    if neg.last_employer_message_at is not None:
        return "message"
    return None


def _stage_at(key: str, app_row: Application, neg: Negotiation | None) -> str | None:
    if key == "confirm":
        return _iso(app_row.queued_at or app_row.created_at)
    if key == "viewed" and neg is not None:
        return _iso(neg.first_viewed_at) or _iso(app_row.sent_at)
    if key == "answer" and neg is not None:
        return (
            _iso(neg.first_reaction_at)
            or _iso(neg.invited_at)
            or _iso(neg.discarded_at)
            or _iso(neg.last_employer_message_at)
            or _iso(app_row.sent_at)
        )
    return _iso(app_row.sent_at)


# ---- rows -------------------------------------------------------------------------------------------------------


async def _vacancy_rows(session: AsyncSession, ids: list[int]) -> dict[int, dict[str, Any]]:
    """The «Вакансии» rows of these vacancies (letter state and Claude's verdict included), by id."""
    if not ids:
        return {}
    rows = (await session.execute(select(Vacancy).where(Vacancy.id.in_(ids)))).scalars().all()
    names = await search_names(session)
    pnames = await profile_names(session)
    letters = await current_letters_map(session, ids)
    verdicts = await current_verdicts(session, ids)
    return {r.id: vacancy_row(r, names, letters.get(r.id), verdicts.get(r.id), pnames) for r in rows}


async def _new_items(session: AsyncSession, cond: Any, *, offset: int, limit: int) -> list[dict[str, Any]]:
    # the ids of the page first, then the rows: the same order as the «Вакансии» list sorted by the score
    ids = list(
        (
            await session.execute(
                select(Vacancy.id)
                .where(cond)
                .order_by(
                    func.coalesce(Vacancy.score, Vacancy.prescore, -1).desc(),
                    desc(Vacancy.published_at),
                    desc(Vacancy.id),
                )
                .offset(offset)
                .limit(limit)
            )
        ).scalars()
    )
    rows = await _vacancy_rows(session, ids)
    return [
        {"vacancy": rows[i], "application": None, "stage_at": rows[i]["first_seen_at"], "outcome": None}
        for i in ids
        if i in rows
    ]


async def _application_items(
    session: AsyncSession, key: str, *, offset: int, limit: int
) -> tuple[list[dict[str, Any]], int]:
    """A column of responses (confirm / sent / viewed / answer): the vacancy row, the application row with its
    negotiation, when the card came to the column and what the employer answered."""
    base = (
        select(Application.id)
        .select_from(Application)
        .join(Vacancy, Vacancy.id == Application.vacancy_id)
        .join(Negotiation, Negotiation.hh_id == Application.hh_negotiation_id, isouter=True)
    )
    if key == "confirm":
        stmt = base.where(Application.id.in_(_confirm_application_ids()))
        order: list[Any] = [desc(func.coalesce(Application.queued_at, Application.created_at)), desc(Application.id)]
    else:
        stmt = base.where(Application.id.in_(_sent_application_ids()), _stage_condition(key))
        if key == "viewed":
            order = [Negotiation.first_viewed_at.desc().nulls_last(), desc(Application.sent_at), desc(Application.id)]
        elif key == "answer":
            order = [_reaction_at().desc().nulls_last(), desc(Application.sent_at), desc(Application.id)]
        else:
            order = [Application.sent_at.desc().nulls_last(), desc(Application.id)]
    total = int((await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0)
    ids = list((await session.execute(stmt.order_by(*order).offset(offset).limit(limit))).scalars())
    found = (
        await session.execute(
            select(Application, Vacancy, Negotiation)
            .join(Vacancy, Vacancy.id == Application.vacancy_id)
            .join(Negotiation, Negotiation.hh_id == Application.hh_negotiation_id, isouter=True)
            .where(Application.id.in_(ids))
        )
    ).all()
    by_id = {a.id: (a, v, n) for a, v, n in found}
    vacancy_rows = await _vacancy_rows(session, [v.id for _a, v, _n in found])
    items = []
    for i in ids:
        if i not in by_id:
            continue
        app_row, vacancy, neg = by_id[i]
        items.append(
            {
                "vacancy": vacancy_rows[vacancy.id],
                "application": application_row(app_row, vacancy, neg),
                "stage_at": _stage_at(key, app_row, neg),
                "outcome": _outcome(neg) if key == "answer" else None,
            }
        )
    return items, total


# ---- the board and a column -------------------------------------------------------------------------------------


async def new_counts(session: AsyncSession, settings: Settings, now: datetime | None = None) -> dict[str, int]:
    """«Новые» in numbers: the bands, how many of the evaluated ones came today (local day), how many letters still
    ask the user something, how many are put off."""
    now = as_utc(now) or utcnow()
    today, _ = local_day_bounds(settings, now)
    decide = or_(band_condition("hi"), band_condition("mid"))

    def n(cond: Any) -> Any:
        return func.coalesce(func.sum(case((cond, 1), else_=0)), 0)

    visible = _visible(now)
    row = (
        await session.execute(
            select(
                n(and_(visible, band_condition("hi"))),
                n(and_(visible, band_condition("mid"))),
                n(and_(visible, band_condition("low"))),
                n(and_(visible, band_condition("wait"))),
                n(and_(visible, band_condition("manual"))),
                n(and_(visible, decide, Vacancy.first_seen_at >= today)),
                n(and_(visible, band_condition("clarify"))),
                n(and_(Vacancy.hidden.is_(False), decide, Vacancy.snoozed_until > now)),
            ).where(
                Vacancy.pipeline_status.in_(
                    ("suitable", "letter_ready", "maybe", "not_suitable", *WAIT_STATUSES, "needs_manual")
                )
            )
        )
    ).one()
    keys = ("hi", "mid", "low", "wait", "manual", "fresh_today", "clarify", "snoozed")
    return {k: int(v) for k, v in zip(keys, row, strict=True)}


async def funnel(session: AsyncSession, settings: Settings, now: datetime | None = None) -> list[dict[str, Any]]:
    """The 30-day funnel over the board: found (watch-only searches left out — they never enter the pipeline), scored
    by Claude from the «maybe» threshold, sent, viewed, invited. Sent/viewed/invited are the dashboard's (stats.py)."""
    period = resolve_period(settings, FUNNEL_PERIOD, now=now)
    found = int(
        (
            await session.execute(
                select(func.count(Vacancy.id)).where(
                    Vacancy.first_seen_at >= period.start,
                    Vacancy.first_seen_at < period.end,
                    Vacancy.pipeline_status != "watched",
                )
            )
        ).scalar()
        or 0
    )
    good = int(
        (
            await session.execute(
                select(func.count(func.distinct(VacancyEvaluation.vacancy_id))).where(
                    VacancyEvaluation.created_at >= period.start,
                    VacancyEvaluation.created_at < period.end,
                    VacancyEvaluation.score >= settings.score_threshold_maybe,
                )
            )
        ).scalar()
        or 0
    )
    sent_rows = await _load_sent(session, period)
    return [
        {"key": "found", "label": "найдено за 30 дней", "value": found, "hint": "без поисков-наблюдений"},
        {"key": "good", "label": f"оценка от {settings.score_threshold_maybe}", "value": good, "hint": None},
        {"key": "sent", "label": "отправлено", "value": len(sent_rows), "hint": None},
        {"key": "viewed", "label": "смотрели", "value": sum(1 for r in sent_rows if r.viewed), "hint": None},
        {"key": "invited", "label": "приглашений", "value": sum(1 for r in sent_rows if r.invited), "hint": None},
    ]


async def column_items(
    session: AsyncSession,
    settings: Settings,
    key: str,
    *,
    band: str | None = None,
    offset: int = 0,
    limit: int = 60,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One column as a list (the open column of the board); «Новые» takes a band (`hi` by default)."""
    now = as_utc(now) or utcnow()
    if key not in COLUMN_KEYS:
        raise ValueError(f"unknown column: {key}")
    if key == "new":
        band = band or "hi"
        if band not in BANDS:
            raise ValueError(f"unknown band: {band}")
        cond = and_(_visible(now), band_condition(band))
        total = int((await session.execute(select(func.count(Vacancy.id)).where(cond))).scalar() or 0)
        items = await _new_items(session, cond, offset=offset, limit=limit)
    else:
        band = None
        items, total = await _application_items(session, key, offset=offset, limit=limit)
    return {
        "source": "vacancies",
        "key": key,
        "band": band,
        "total": total,
        "offset": offset,
        "items": items,
    }


async def board_view(session: AsyncSession, settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    now = as_utc(now) or utcnow()
    counts = await new_counts(session, settings, now)
    columns: list[dict[str, Any]] = []
    for key, title in COLUMNS:
        if key == "new":
            decide = and_(_visible(now), or_(band_condition("hi"), band_condition("mid")))
            items = await _new_items(session, decide, offset=0, limit=PREVIEW[key])
            count = counts["hi"] + counts["mid"]
        else:
            items, count = await _application_items(session, key, offset=0, limit=PREVIEW[key])
        columns.append({"key": key, "title": title, "count": count, "items": items})
    return {
        "source": "vacancies",
        "thresholds": {"suitable": settings.score_threshold_suitable, "maybe": settings.score_threshold_maybe},
        "funnel": await funnel(session, settings, now),
        "new": counts,
        "columns": columns,
    }
