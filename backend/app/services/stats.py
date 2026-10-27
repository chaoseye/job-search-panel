"""Statistics computed on the server, cached until the next write to the data (60 s at most).

Definitions (F-8.10.5; the UI shows the same wording in tooltips):

* period — local calendar days [date_from, date_to] in `Settings.timezone`; `today`, `7d`, `30d`, `all`, `custom`;
* found — vacancies first seen by the scanner in the period (`vacancies.first_seen_at`);
* passed_rules — of those, vacancies that passed the hard rules (`rules_passed = 1`);
* evaluated — vacancies with a Claude evaluation created in the period (distinct vacancy);
* suitable — evaluations in the period with Claude's verdict `apply` (SUITABLE_VERDICTS);
* sent — applications with status `sent`, not dry-run, `sent_at` in the period; split by mode
  (`api` — sent by the panel, `assist` — confirmed by the user, `external` — found by sync);
* viewed — sent applications whose negotiation was viewed by the employer (`viewed_by_opponent`);
* invitations / discards — sent applications whose negotiation ever reached `invitation` / `discard`
  (`invited_at` / `discarded_at` set by sync);
* no_answer — sent more than N days ago (default 7) without any employer reaction (no invitation, no discard,
  no employer message);
* conversions — viewed/sent, invitations/sent, invitations/viewed (percent, 1 decimal);
* median_reaction_hours — median of (first employer reaction − sent_at) in hours over applications with a
  reaction (view, state change or message), 1 decimal;
* remaining_day — applications still allowed today by the panel limit (APPLY_MAX_PER_DAY);
* batches_created / batches_imported / letters_imported — packets created / imported and Claude letters
  imported in the period.
"""

from __future__ import annotations

import csv
import io
import json
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import changes
from app.db.base import as_utc, utcnow
from app.db.models import (
    Application,
    ClaudeBatch,
    CoverLetter,
    Negotiation,
    SavedSearch,
    Vacancy,
    VacancyEvaluation,
)
from app.services.counters import counters_between, sum_counters

CACHE_TTL_SEC = 60
NO_ANSWER_DAYS = 7
# Claude's «suits, apply» (the result schema says `apply`; `suitable` is kept for evaluations written by hand)
SUITABLE_VERDICTS = ("apply", "suitable")
PERIODS = ("today", "7d", "30d", "all", "custom")
SCORE_BUCKETS = ((0, 49, "0–49"), (50, 69, "50–69"), (70, 79, "70–79"), (80, 89, "80–89"), (90, 100, "90–100"))
SALARY_BUCKETS = (
    (None, 99_999, "до 100 000 ₽"),
    (100_000, 149_999, "100–150 тыс. ₽"),
    (150_000, 199_999, "150–200 тыс. ₽"),
    (200_000, 299_999, "200–300 тыс. ₽"),
    (300_000, None, "от 300 000 ₽"),
)
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
MAX_SERIES_DAYS = 120
BOM = "﻿"  # Excel opens UTF-8 CSV correctly only with a BOM
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: Any) -> Any:
    """Text that starts like a formula gets a leading apostrophe: Excel / LibreOffice would run «=HYPERLINK(…)» from a
    vacancy title or an employer name as a formula (CSV injection). Numbers stay numbers."""
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


class _CsvWriter:
    """csv.writer with «;» (Excel in the Russian locale) and escaped formula-like text cells."""

    def __init__(self, buf: io.StringIO) -> None:
        self._writer = csv.writer(buf, delimiter=";", lineterminator="\n")

    def writerow(self, row: Sequence[Any]) -> None:
        self._writer.writerow([_csv_cell(v) for v in row])


_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
CACHE_MAX_ENTRIES = 64


def clear_cache() -> None:
    _cache.clear()


def _store(key: tuple[Any, ...], result: dict[str, Any]) -> None:
    """Keeps a result; entries past the TTL (every write makes a new key) and the oldest beyond the cap go."""
    moment = time.monotonic()
    for old in [k for k, (ts, _) in _cache.items() if moment - ts >= CACHE_TTL_SEC]:
        del _cache[old]
    _cache[key] = (moment, result)
    while len(_cache) > CACHE_MAX_ENTRIES:
        del _cache[next(iter(_cache))]


@dataclass
class Period:
    key: str
    date_from: date
    date_to: date  # inclusive
    tz: ZoneInfo

    # Bounds are UTC-aware: SQLite stores the naive part of a datetime as given, and the panel stores UTC,
    # so bound parameters must be UTC too (a local-aware value would compare against local midnight).
    @property
    def start(self) -> datetime:
        return datetime.combine(self.date_from, datetime.min.time(), tzinfo=self.tz).astimezone(UTC)

    @property
    def end(self) -> datetime:
        return datetime.combine(self.date_to + timedelta(days=1), datetime.min.time(), tzinfo=self.tz).astimezone(UTC)

    def contains(self, moment: datetime | None) -> bool:
        m = as_utc(moment)
        return m is not None and self.start <= m < self.end

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "date_from": self.date_from.isoformat(), "date_to": self.date_to.isoformat()}


def resolve_period(
    settings: Settings,
    key: str,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    now: datetime | None = None,
    earliest: date | None = None,
) -> Period:
    tz = ZoneInfo(settings.timezone or "UTC")
    today = (as_utc(now) or utcnow()).astimezone(tz).date()
    if key not in PERIODS:
        key = "7d"
    if key == "today":
        return Period(key, today, today, tz)
    if key == "7d":
        return Period(key, today - timedelta(days=6), today, tz)
    if key == "30d":
        return Period(key, today - timedelta(days=29), today, tz)
    if key == "custom" and date_from:
        d_from = date.fromisoformat(date_from)
        d_to = date.fromisoformat(date_to) if date_to else today
        for d in (d_from, d_to):
            # 0001-01-01 / 9999-12-31 overflow the datetime arithmetic of Period.start/end (500 before)
            if not 2000 <= d.year <= 2100:
                raise ValueError(f"дата вне диапазона 2000–2100: {d.isoformat()}")
        if d_to < d_from:
            d_from, d_to = d_to, d_from
        return Period(key, d_from, d_to, tz)
    start = earliest or (today - timedelta(days=365))
    return Period("all", min(start, today), today, tz)


# ---- helpers ------------------------------------------------------------------------------


def _pct(part: int, whole: int) -> float | None:
    return round(part * 100.0 / whole, 1) if whole else None


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def score_bucket(score: int | None) -> str:
    if score is None:
        return "нет оценки"
    for lo, hi, label in SCORE_BUCKETS:
        if lo <= score <= hi:
            return label
    return "нет оценки"


def salary_bucket(net_rur: int | None) -> str:
    if net_rur is None:
        return "не указана"
    for lo, hi, label in SALARY_BUCKETS:
        if (lo is None or net_rur >= lo) and (hi is None or net_rur <= hi):
            return label
    return "не указана"


@dataclass(frozen=True)
class SentVacancy:
    """The vacancy fields the statistics, the CSV export and the analytics read. Not the ORM row: it is ~46 KB with
    the hh payload, and loading 2 000 of them made the statistics miss the target (≤ 500 ms). Not a partially loaded
    Vacancy either — it would stay in the session's identity map and break a later full load in the same session."""

    id: int
    name: str
    employer_name: str | None
    area_name: str | None
    salary_net_rur: int | None
    work_format_names_json: str | None
    score: int | None
    prescore: int | None
    alternate_url: str | None
    source_search_ids: str | None


@dataclass(frozen=True)
class SentApp:
    """The application fields the reports read (the ORM rows cost ~0.1 s to build for 2 000 applications)."""

    id: int
    vacancy_id: int
    mode: str
    sent_at: datetime | None
    letter_text: str | None
    cover_letter_id: int | None
    hh_negotiation_id: str | None


@dataclass(frozen=True)
class SentNeg:
    """The negotiation fields the reports read (the ORM row carries hh's raw JSON)."""

    id: int
    state_id: str | None
    viewed_by_opponent: bool
    first_viewed_at: datetime | None
    first_reaction_at: datetime | None
    invited_at: datetime | None
    discarded_at: datetime | None
    last_employer_message_at: datetime | None


SENT_VACANCY_COLUMNS = tuple(getattr(Vacancy, f.name) for f in fields(SentVacancy))
SENT_APP_COLUMNS = tuple(getattr(Application, f.name) for f in fields(SentApp))
SENT_NEG_COLUMNS = tuple(getattr(Negotiation, f.name) for f in fields(SentNeg))


@dataclass
class SentRow:
    app: SentApp
    vacancy: SentVacancy | None
    neg: SentNeg | None
    sent_local: datetime

    @property
    def viewed(self) -> bool:
        return bool(self.neg is not None and (self.neg.viewed_by_opponent or self.neg.first_viewed_at))

    @property
    def invited(self) -> bool:
        return bool(self.neg is not None and (self.neg.invited_at or self.neg.state_id == "invitation"))

    @property
    def discarded(self) -> bool:
        return bool(self.neg is not None and (self.neg.discarded_at or self.neg.state_id == "discard"))

    @property
    def reacted(self) -> bool:
        return bool(self.neg is not None and (self.invited or self.discarded or self.neg.last_employer_message_at))

    def reaction_hours(self) -> float | None:
        if self.neg is None:
            return None
        reacted_at = as_utc(self.neg.first_reaction_at)
        sent_at = as_utc(self.app.sent_at)
        if reacted_at is None or sent_at is None:
            return None
        return max(0.0, (reacted_at - sent_at).total_seconds() / 3600.0)

    def outcome(self) -> str:
        if self.invited:
            return "invited"
        if self.discarded:
            return "discarded"
        if self.viewed:
            return "viewed"
        return "sent"


async def _load_sent(session: AsyncSession, period: Period) -> list[SentRow]:
    stmt = (
        select(*SENT_APP_COLUMNS, *SENT_NEG_COLUMNS, *SENT_VACANCY_COLUMNS)
        .select_from(Application)
        .join(Vacancy, Vacancy.id == Application.vacancy_id, isouter=True)
        .join(Negotiation, Negotiation.hh_id == Application.hh_negotiation_id, isouter=True)
        .where(
            Application.status == "sent",
            Application.dry_run.is_(False),
            Application.sent_at >= period.start,
            Application.sent_at < period.end,
        )
        .order_by(Application.sent_at)
    )
    rows = (await session.execute(stmt)).all()
    n_app, n_neg = len(SENT_APP_COLUMNS), len(SENT_NEG_COLUMNS)
    out: list[SentRow] = []
    for row in rows:
        app_row = SentApp(*row[:n_app])
        neg_values, vacancy_values = row[n_app : n_app + n_neg], row[n_app + n_neg :]
        sent = as_utc(app_row.sent_at)
        if sent is None:
            continue
        neg = SentNeg(*neg_values) if neg_values[0] is not None else None
        vacancy = SentVacancy(*vacancy_values) if vacancy_values[0] is not None else None
        out.append(SentRow(app_row, vacancy, neg, sent.astimezone(period.tz)))
    return out


async def _earliest_date(session: AsyncSession, tz: ZoneInfo) -> date | None:
    candidates = []
    for col in (func.min(Vacancy.first_seen_at), func.min(Application.sent_at)):
        value = (await session.execute(select(col))).scalar()
        dt = as_utc(value)
        if dt is not None:
            candidates.append(dt.astimezone(tz).date())
    return min(candidates) if candidates else None


# ---- main entry ---------------------------------------------------------------------------


async def compute_stats(
    session: AsyncSession,
    settings: Settings,
    *,
    period_key: str = "7d",
    date_from: str | None = None,
    date_to: str | None = None,
    no_answer_days: int = NO_ANSWER_DAYS,
    now: datetime | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    now = as_utc(now) or utcnow()
    tz = ZoneInfo(settings.timezone or "UTC")
    # the database is part of the key (tests run many), the data generation too: a write the user just made shows
    # at once, the TTL only covers the «today»/«7d» windows moving with the clock
    key = (settings.db_url, period_key, date_from, date_to, no_answer_days, settings.timezone, changes.generation())
    if use_cache and key in _cache:
        ts, cached = _cache[key]
        if time.monotonic() - ts < CACHE_TTL_SEC:
            return cached
    earliest = await _earliest_date(session, tz) if period_key == "all" else None
    period = resolve_period(settings, period_key, date_from=date_from, date_to=date_to, now=now, earliest=earliest)

    # --- funnel top: vacancies and evaluations
    vac_rows = (
        await session.execute(
            select(Vacancy.id, Vacancy.first_seen_at, Vacancy.rules_passed, Vacancy.pipeline_status).where(
                Vacancy.first_seen_at >= period.start, Vacancy.first_seen_at < period.end
            )
        )
    ).all()
    found = len(vac_rows)
    passed_rules = sum(1 for r in vac_rows if r.rules_passed is True)
    ev_rows = (
        await session.execute(
            select(VacancyEvaluation.vacancy_id, VacancyEvaluation.verdict, VacancyEvaluation.score).where(
                VacancyEvaluation.created_at >= period.start, VacancyEvaluation.created_at < period.end
            )
        )
    ).all()
    evaluated = len({r.vacancy_id for r in ev_rows})
    suitable = len({r.vacancy_id for r in ev_rows if r.verdict in SUITABLE_VERDICTS})
    letters_ready = int(
        (
            await session.execute(
                select(func.count(func.distinct(CoverLetter.vacancy_id))).where(
                    CoverLetter.created_at >= period.start, CoverLetter.created_at < period.end
                )
            )
        ).scalar()
        or 0
    )

    # --- applications cohort
    sent_rows = await _load_sent(session, period)
    sent = len(sent_rows)
    by_mode = Counter(r.app.mode for r in sent_rows)
    viewed = sum(1 for r in sent_rows if r.viewed)
    invitations = sum(1 for r in sent_rows if r.invited)
    discards = sum(1 for r in sent_rows if r.discarded)
    threshold = now - timedelta(days=no_answer_days)
    no_answer = 0
    for r in sent_rows:
        sent_at = as_utc(r.app.sent_at)
        if not r.reacted and sent_at is not None and sent_at < threshold:
            no_answer += 1
    reaction_hours = [h for h in (r.reaction_hours() for r in sent_rows) if h is not None]
    median_reaction = round(statistics.median(reaction_hours), 1) if reaction_hours else None

    # --- packets / letters / counters
    batches_created = int(
        (
            await session.execute(
                select(func.count(ClaudeBatch.id)).where(
                    ClaudeBatch.created_at >= period.start, ClaudeBatch.created_at < period.end
                )
            )
        ).scalar()
        or 0
    )
    batches_imported = int(
        (
            await session.execute(
                select(func.count(ClaudeBatch.id)).where(
                    ClaudeBatch.imported_at >= period.start, ClaudeBatch.imported_at < period.end
                )
            )
        ).scalar()
        or 0
    )
    letters_imported = int(
        (
            await session.execute(
                select(func.count(CoverLetter.id)).where(
                    CoverLetter.origin == "claude",
                    CoverLetter.created_at >= period.start,
                    CoverLetter.created_at < period.end,
                )
            )
        ).scalar()
        or 0
    )
    counters = await counters_between(session, period.date_from.isoformat(), period.date_to.isoformat())
    awaiting = int(
        (
            await session.execute(select(func.count(Vacancy.id)).where(Vacancy.pipeline_status == "awaiting_claude"))
        ).scalar()
        or 0
    )

    # --- remaining day limit
    from app.services.applier import limits_status

    limits = await limits_status(session, settings, now=now)

    # --- series per day
    series = _daily_series(period, vac_rows, ev_rows, sent_rows, tz)

    # --- score buckets of sent applications
    buckets: dict[str, dict[str, int]] = {label: {"sent": 0, "invited": 0} for _, _, label in SCORE_BUCKETS}
    buckets["нет оценки"] = {"sent": 0, "invited": 0}
    for r in sent_rows:
        label = score_bucket(r.vacancy.score if r.vacancy is not None else None)
        buckets[label]["sent"] += 1
        if r.invited:
            buckets[label]["invited"] += 1
    score_chart = [
        {"bucket": label, "sent": v["sent"], "invited": v["invited"], "invited_pct": _pct(v["invited"], v["sent"])}
        for label, v in buckets.items()
        if v["sent"] or label != "нет оценки"
    ]

    # --- breakdowns
    search_names = {sid: name for sid, name in (await session.execute(select(SavedSearch.id, SavedSearch.name))).all()}
    breakdowns = {
        "by_search": _breakdown(sent_rows, lambda r: _search_labels(r.vacancy, search_names)),
        "by_employer": _breakdown(
            sent_rows, lambda r: [r.vacancy.employer_name or "—"] if r.vacancy else ["—"], limit=15
        ),
        "by_salary": _breakdown(
            sent_rows, lambda r: [salary_bucket(r.vacancy.salary_net_rur if r.vacancy else None)], order=_salary_order
        ),
        "by_work_format": _breakdown(
            sent_rows, lambda r: (_loads(r.vacancy.work_format_names_json, []) if r.vacancy else []) or ["не указан"]
        ),
        "by_area": _breakdown(sent_rows, lambda r: [r.vacancy.area_name or "—"] if r.vacancy else ["—"], limit=15),
        "by_weekday_hour": _weekday_hour(sent_rows),
    }

    result = {
        "period": period.to_dict(),
        "generated_at": now.isoformat(timespec="seconds"),
        "no_answer_days": no_answer_days,
        "metrics": {
            "found": found,
            "passed_rules": passed_rules,
            "evaluated": evaluated,
            "suitable": suitable,
            "letters_ready": letters_ready,
            "sent": sent,
            "sent_api": int(by_mode.get("api", 0)),
            "sent_assist": int(by_mode.get("assist", 0)),
            "sent_external": int(by_mode.get("external", 0)),
            "viewed": viewed,
            "invitations": invitations,
            "discards": discards,
            "no_answer": no_answer,
            "conv_sent_viewed": _pct(viewed, sent),
            "conv_sent_invited": _pct(invitations, sent),
            "conv_viewed_invited": _pct(invitations, viewed),
            "median_reaction_hours": median_reaction,
            "remaining_day": limits.remaining_day,
            "max_per_day": limits.max_per_day,
            "batches_created": batches_created,
            "batches_imported": batches_imported,
            "letters_imported": letters_imported,
            "awaiting_claude": awaiting,
            "hh_requests": sum_counters(counters)["hh_requests"],
        },
        "funnel": [
            {"stage": "found", "label": "найдено", "value": found},
            {"stage": "passed_rules", "label": "прошли правила", "value": passed_rules},
            {"stage": "evaluated", "label": "разобрано Claude", "value": evaluated},
            {"stage": "suitable", "label": "подходят", "value": suitable},
            {"stage": "letters", "label": "письмо готово", "value": letters_ready},
            {"stage": "sent", "label": "отправлено", "value": sent},
            {"stage": "viewed", "label": "просмотрено", "value": viewed},
            {"stage": "invitations", "label": "приглашения", "value": invitations},
        ],
        "series": series,
        "score_buckets": score_chart,
        "breakdowns": breakdowns,
        "counters": counters,
    }
    _store(key, result)
    return result


def _daily_series(
    period: Period, vac_rows: Sequence[Any], ev_rows: Sequence[Any], sent_rows: list[SentRow], tz: ZoneInfo
) -> list[dict[str, Any]]:
    days: dict[str, dict[str, int]] = {}
    total_days = (period.date_to - period.date_from).days + 1
    start = period.date_from if total_days <= MAX_SERIES_DAYS else period.date_to - timedelta(days=MAX_SERIES_DAYS - 1)
    d = start
    while d <= period.date_to:
        days[d.isoformat()] = {"found": 0, "suitable": 0, "sent": 0, "viewed": 0, "invited": 0, "discarded": 0}
        d += timedelta(days=1)
    for r in vac_rows:
        dt = as_utc(r.first_seen_at)
        key = dt.astimezone(tz).date().isoformat() if dt else None
        if key in days:
            days[key]["found"] += 1
    suitable_ids = {r.vacancy_id for r in ev_rows if r.verdict in SUITABLE_VERDICTS}
    for r in vac_rows:
        if r.id in suitable_ids:
            dt = as_utc(r.first_seen_at)
            key = dt.astimezone(tz).date().isoformat() if dt else None
            if key in days:
                days[key]["suitable"] += 1
    for r in sent_rows:
        key = r.sent_local.date().isoformat()
        if key not in days:
            continue
        days[key]["sent"] += 1
        outcome = r.outcome()
        if outcome == "invited":
            days[key]["invited"] += 1
        elif outcome == "discarded":
            days[key]["discarded"] += 1
        elif outcome == "viewed":
            days[key]["viewed"] += 1
    return [{"date": k, **v} for k, v in days.items()]


def _search_labels(vacancy: SentVacancy | None, names: dict[int, str]) -> list[str]:
    if vacancy is None:
        return ["—"]
    ids = [int(p) for p in (vacancy.source_search_ids or "").split(",") if p.strip().isdigit()]
    labels = [names.get(i, f"поиск #{i}") for i in ids]
    return labels or ["вне поисков"]


def _salary_order(label: str) -> int:
    order = [lbl for _, _, lbl in SALARY_BUCKETS] + ["не указана"]
    return order.index(label) if label in order else len(order)


def _breakdown(
    rows: list[SentRow], key_fn: Any, *, limit: int | None = None, order: Any = None
) -> list[dict[str, Any]]:
    acc: dict[str, dict[str, int]] = defaultdict(lambda: {"sent": 0, "viewed": 0, "invited": 0, "discarded": 0})
    for r in rows:
        for label in key_fn(r):
            cell = acc[str(label)]
            cell["sent"] += 1
            if r.viewed:
                cell["viewed"] += 1
            if r.invited:
                cell["invited"] += 1
            if r.discarded:
                cell["discarded"] += 1
    items: list[dict[str, Any]] = [
        {"label": label, **v, "invited_pct": _pct(v["invited"], v["sent"]), "viewed_pct": _pct(v["viewed"], v["sent"])}
        for label, v in acc.items()
    ]
    if order is not None:
        items.sort(key=lambda i: order(i["label"]))
    else:
        items.sort(key=lambda i: (-int(i["sent"]), str(i["label"])))
    return items[:limit] if limit else items


def _weekday_hour(rows: list[SentRow]) -> dict[str, Any]:
    cells: Counter[tuple[int, int]] = Counter()
    invited: Counter[tuple[int, int]] = Counter()
    for r in rows:
        key = (r.sent_local.weekday(), r.sent_local.hour)
        cells[key] += 1
        if r.invited:
            invited[key] += 1
    by_weekday = [
        {
            "weekday": i,
            "label": WEEKDAYS[i],
            "sent": sum(c for (w, _), c in cells.items() if w == i),
            "invited": sum(c for (w, _), c in invited.items() if w == i),
        }
        for i in range(7)
    ]
    by_hour = [
        {
            "hour": h,
            "sent": sum(c for (_, hh), c in cells.items() if hh == h),
            "invited": sum(c for (_, hh), c in invited.items() if hh == h),
        }
        for h in range(24)
    ]
    return {
        "cells": [
            {"weekday": w, "hour": h, "sent": c, "invited": invited.get((w, h), 0)}
            for (w, h), c in sorted(cells.items())
        ],
        "by_weekday": by_weekday,
        "by_hour": by_hour,
    }


# ---- CSV export --------------------------------------------------------------------------


async def export_applications_csv(
    session: AsyncSession, settings: Settings, *, period_key: str, date_from: str | None, date_to: str | None
) -> str:
    tz = ZoneInfo(settings.timezone or "UTC")
    earliest = await _earliest_date(session, tz) if period_key == "all" else None
    period = resolve_period(settings, period_key, date_from=date_from, date_to=date_to, earliest=earliest)
    rows = await _load_sent(session, period)
    search_names = {sid: name for sid, name in (await session.execute(select(SavedSearch.id, SavedSearch.name))).all()}
    buf = io.StringIO()
    writer = _CsvWriter(buf)
    writer.writerow(
        [
            "sent_at",
            "vacancy",
            "employer",
            "area",
            "salary_net_rur",
            "work_format",
            "mode",
            "status",
            "negotiation_state",
            "viewed_at",
            "invited_at",
            "discarded_at",
            "reaction_hours",
            "score",
            "prescore",
            "letter_chars",
            "searches",
            "hh_url",
            "negotiation_id",
        ]
    )

    def local(dt: datetime | None) -> str:
        d = as_utc(dt)
        return d.astimezone(tz).strftime("%Y-%m-%d %H:%M") if d else ""

    for r in rows:
        v, n = r.vacancy, r.neg
        writer.writerow(
            [
                local(r.app.sent_at),
                v.name if v else "",
                (v.employer_name or "") if v else "",
                (v.area_name or "") if v else "",
                v.salary_net_rur if v and v.salary_net_rur is not None else "",
                ", ".join(_loads(v.work_format_names_json, [])) if v else "",
                r.app.mode,
                r.outcome(),
                (n.state_id or "") if n else "",
                local(n.first_viewed_at) if n else "",
                local(n.invited_at) if n else "",
                local(n.discarded_at) if n else "",
                f"{r.reaction_hours():.1f}" if r.reaction_hours() is not None else "",
                v.score if v and v.score is not None else "",
                v.prescore if v and v.prescore is not None else "",
                len(r.app.letter_text or ""),
                "; ".join(_search_labels(v, search_names)),
                (v.alternate_url or "") if v else "",
                r.app.hh_negotiation_id or "",
            ]
        )
    return BOM + buf.getvalue()


async def export_aggregates_csv(
    session: AsyncSession, settings: Settings, *, period_key: str, date_from: str | None, date_to: str | None
) -> str:
    stats = await compute_stats(
        session, settings, period_key=period_key, date_from=date_from, date_to=date_to, use_cache=False
    )
    buf = io.StringIO()
    writer = _CsvWriter(buf)
    writer.writerow(["metric", "value"])
    writer.writerow(["period", f"{stats['period']['date_from']}..{stats['period']['date_to']}"])
    for key, value in stats["metrics"].items():
        writer.writerow([key, "" if value is None else value])
    writer.writerow([])
    writer.writerow(["date", "found", "suitable", "sent", "viewed", "invited", "discarded"])
    for day in stats["series"]:
        writer.writerow(
            [day["date"], day["found"], day["suitable"], day["sent"], day["viewed"], day["invited"], day["discarded"]]
        )
    writer.writerow([])
    writer.writerow(["score_bucket", "sent", "invited", "invited_pct"])
    for b in stats["score_buckets"]:
        writer.writerow([b["bucket"], b["sent"], b["invited"], "" if b["invited_pct"] is None else b["invited_pct"]])
    for name, rows in stats["breakdowns"].items():
        if name == "by_weekday_hour":
            continue
        writer.writerow([])
        writer.writerow([name, "sent", "viewed", "invited", "discarded"])
        for r in rows:
            writer.writerow([r["label"], r["sent"], r["viewed"], r["invited"], r["discarded"]])
    return BOM + buf.getvalue()


async def daily_summary_payload(
    session: AsyncSession, settings: Settings, *, now: datetime | None = None
) -> dict[str, Any]:
    """Numbers for the evening Telegram summary."""
    stats = await compute_stats(session, settings, period_key="today", now=now, use_cache=False)
    m = stats["metrics"]
    all_time = await compute_stats(session, settings, period_key="all", now=now, use_cache=False)
    journal = await journal_reminder(session, now=now)
    return {
        "date": stats["period"]["date_to"],
        "found": m["found"],
        "suitable": m["suitable"],
        "sent": m["sent"],
        "viewed": m["viewed"],
        "invitations": m["invitations"],
        "awaiting": m["awaiting_claude"],
        "no_answer": all_time["metrics"]["no_answer"],
        "journal": journal["items"],
        "journal_total": journal["total"],
        "journal_days": NO_ANSWER_DAYS,
    }


async def journal_reminder(
    session: AsyncSession, *, now: datetime | None = None, days: int = NO_ANSWER_DAYS, limit: int = 10
) -> dict[str, Any]:
    """Applications the user should look at on hh.ru when the panel cannot sync them: sent at least `days` ago
    with no record at all, or whose (manual) record was not updated for `days`. Empty when hh sync works."""
    from app.services.state import get_system_state, read_capabilities

    caps = read_capabilities(await get_system_state(session))
    if caps.get("negotiations_list") != "unavailable":
        return {"items": [], "total": 0}
    moment = now or utcnow()
    threshold = moment - timedelta(days=days)
    # columns, not ORM rows: a vacancy row is ~46 KB with the hh payload, only its name and employer are needed
    stmt = (
        select(
            Application.id,
            Application.vacancy_id,
            Application.sent_at,
            Vacancy.id.label("vacancy_found"),
            Vacancy.name,
            Vacancy.employer_name,
            Negotiation.id.label("negotiation_found"),
            Negotiation.state_id,
            Negotiation.hidden,
            Negotiation.hh_updated_at,
            Negotiation.hh_created_at,
        )
        .select_from(Application)
        .join(Vacancy, Vacancy.id == Application.vacancy_id, isouter=True)
        .join(Negotiation, Negotiation.hh_id == Application.hh_negotiation_id, isouter=True)
        .where(Application.status == "sent", Application.dry_run.is_(False), Application.sent_at <= threshold)
        .order_by(Application.sent_at)
    )
    items: list[dict[str, Any]] = []
    for r in (await session.execute(stmt)).all():
        if r.negotiation_found is not None:
            if r.state_id in ("invitation", "discard") or r.hidden:
                continue  # the outcome is known
            last = as_utc(r.hh_updated_at) or as_utc(r.hh_created_at)
            if last is not None and last > threshold:
                continue  # touched recently
        sent = as_utc(r.sent_at)
        items.append(
            {
                "application_id": r.id,
                "name": r.name if r.vacancy_found is not None else f"#{r.vacancy_id}",
                "employer": r.employer_name if r.vacancy_found is not None else None,
                "days": int((moment - sent).days) if sent else None,
            }
        )
    return {"items": items[:limit], "total": len(items)}
