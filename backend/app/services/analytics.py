"""Analytics of vacancy segments: what the vacancies of a given kind ask for and how the user fares there.

A segment is any slice of the vacancy base: a saved search, a criteria set, a target title from the questionnaire,
a tag (industry / tasks / stack — `services/tagging.py`), a work format or the required experience. Everything is
computed in Python over a light projection of the vacancies table plus the current evaluations and the sent
applications — no hh requests.

The projection (`Base`) costs ~1.5 s of Python on 10 000 vacancies (the target volume) and the page asks nine reports at
once, so the API shares one per `days` (the scopes are cut from it): `cached_base` builds it once — concurrent
requests wait for the same build — and rebuilds it after any committed write to the tables it reads
(`app.db.changes`); the reports computed on a base are kept with it. The building and the reports run in one
worker thread (`run_cpu`): the event loop keeps serving the rest of the panel meanwhile (it froze for ~20 s).

Reports: segment comparison, hot vacancies (fresh, few responses, good score), skills and gaps, salary, response
funnel by dimension, employers, weekly trends.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db import changes
from app.db.base import as_utc, utcnow
from app.db.models import HiddenEmployer, SavedSearch, SelectionProfile, Vacancy, VacancyEvaluation
from app.services.preferences import WORK_FORMAT_LABELS, get_preferences
from app.services.profile import primary_resume, primary_structure
from app.services.selection import effective_preferences
from app.services.stats import SentRow, _load_sent, resolve_period, salary_bucket, score_bucket
from app.services.tagging import GROUPS, STACK, load_tags, merge_tags, rule_tags
from app.services.textmatch import TermIndex, contains_term, norm, significant_words, terms_equal

SEGMENT_KINDS: dict[str, str] = {
    "search": "по поискам",
    "set": "по наборам критериев",
    "title": "по целевым должностям",
    "domain": "по отраслям",
    "tasks": "по задачам",
    "stack": "по инструментам",
    "format": "по формату работы",
    "experience": "по требуемому стажу",
}
FUNNEL_DIMENSIONS: dict[str, str] = {
    **SEGMENT_KINDS,
    "letter": "письмо / без письма",
    "accredited": "аккредитация IT",
    "test": "тестовое задание",
    "salary": "зарплатная вилка",
    "score": "оценка Claude",
}
EXPERIENCE_LABELS = {
    "noExperience": "без опыта",
    "between1And3": "1–3 года",
    "between3And6": "3–6 лет",
    "moreThan6": "более 6 лет",
}
# which vacancies the reports look at: everything the scanner saw, only those past the hard rules (default —
# stop-word sales jobs would otherwise dominate the skills and salary pictures), only those Claude evaluated
SCOPES: dict[str, str] = {"passed": "прошедшие правила", "all": "все найденные", "evaluated": "с оценкой Claude"}
# not worth a reminder in the hot list: already handled or dropped by rules / Claude
HOT_EXCLUDED = {"sent", "queued", "already_applied", "filtered_out", "not_suitable", "skipped", "hidden"}
NO_ANSWER_DAYS = 7
TOP_SKILLS = 30
TOP_LINES = 15
# title words that say nothing about the role: never suggested as searches or stop words
LEVEL_WORDS = frozenset(
    "junior middle senior lead intern trainee remote удаленно удаленный удаленная стажер стажёр младший старший "
    "ведущий главный руководитель head chief team лет опыт год года москва санкт петербург спб россия рф "
    "the and for with без для или по на из от до за не со во об при под над".split()
)
GOOD_SCORE = 50


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _pct(part: int, whole: int) -> float | None:
    return round(part * 100.0 / whole, 1) if whole else None


def _median(values: list[int]) -> int | None:
    return int(statistics.median(values)) if values else None


def _quartiles(values: list[int]) -> tuple[int | None, int | None, int | None]:
    if not values:
        return None, None, None
    if len(values) < 2:
        return values[0], values[0], values[0]
    q = statistics.quantiles(sorted(values), n=4, method="inclusive")
    return int(q[0]), int(q[1]), int(q[2])


# ---- projections ------------------------------------------------------------------------


@dataclass
class V:
    id: int
    hh_id: str
    name: str
    url: str | None
    employer_id: str | None
    employer_name: str | None
    accredited: bool | None
    trusted: bool | None
    area: str | None
    salary: int | None
    experience: str | None
    formats: list[str]
    published_at: datetime | None
    first_seen_at: datetime | None
    archived: bool
    direct: bool
    has_test: bool
    letter_required: bool
    key_skills: list[str]
    responses: int | None
    status: str
    prescore: int | None
    score: int | None
    set_id: int | None
    search_ids: list[int]
    hidden: bool
    tags: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class E:
    vacancy_id: int
    score: int
    verdict: str
    summary: str
    matched: list[str]
    gaps: list[str]
    red_flags: list[str]
    salary_fit: str | None
    seniority_fit: str | None
    tags: dict[str, list[str]]


@dataclass
class Base:
    vacancies: list[V]
    evals: dict[int, E]
    sent: list[SentRow]
    search_names: dict[int, str]
    set_names: dict[int, str]
    titles: list[str]
    my_skills: list[str]
    my_salary: int | None
    hidden_employers: set[str]
    tz: ZoneInfo
    now: datetime
    my_weights: dict[str, int] = field(default_factory=dict)  # explicit questionnaire weights (norm(name) → weight)
    stop_words: list[str] = field(default_factory=list)
    search_texts: list[str] = field(default_factory=list)  # `text` of enabled searches
    by_id: dict[int, V] = field(init=False, default_factory=dict)
    sent_by_vacancy: dict[int, list[SentRow]] = field(init=False, default_factory=dict)
    # a shared base is immutable: the reports computed on it are kept until the base itself is rebuilt
    reports: dict[tuple[Any, ...], dict[str, Any]] = field(init=False, default_factory=dict, repr=False)
    _title_keys: dict[int, list[tuple[str, str]]] | None = field(init=False, default=None, repr=False)
    _root: Base | None = field(init=False, default=None, repr=False)  # the «all» base a scoped one was cut from
    _scoped: dict[str, Base] = field(init=False, default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.by_id = {v.id: v for v in self.vacancies}
        for r in self.sent:
            self.sent_by_vacancy.setdefault(r.app.vacancy_id, []).append(r)

    def scoped(self, scope: str) -> Base:
        """This base (loaded with scope «all») narrowed to `scope`: the scopes differ only in the vacancy list — the
        evaluations, applications and names are the same, so they are not loaded again."""
        if scope == "all":
            return self
        hit = self._scoped.get(scope)
        if hit is None:
            hit = replace(self, vacancies=_in_scope(self.vacancies, self.evals, scope))
            hit._root = self._root or self
            self._scoped[scope] = hit
        return hit

    def report(self, key: tuple[Any, ...], compute: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """A report of this base, computed once (two threads asking at once both compute it; one result is kept)."""
        hit = self.reports.get(key)
        if hit is None:
            hit = compute()
            self.reports[key] = hit
        return hit

    def title_keys(self, v: V) -> list[tuple[str, str]]:
        """(key, label) of the target titles the vacancy name contains, in the order of `titles`. Built once per base
        with one TermIndex over all the names — not a regex per vacancy per title; a scoped base asks its root. A
        shared base may be asked from two report threads at once: both build the same dict, one assignment wins."""
        if self._root is not None:
            return self._root.title_keys(v)
        keys = self._title_keys
        if keys is None:
            index = TermIndex([norm(x.name) for x in self.vacancies])
            keys = {}
            for title in self.titles:
                for i in sorted(index.matching(title)):
                    keys.setdefault(self.vacancies[i].id, []).append((norm(title), title))
            self._title_keys = keys
        return keys.get(v.id, [])


EVAL_COLUMNS = (
    VacancyEvaluation.vacancy_id,
    VacancyEvaluation.score,
    VacancyEvaluation.verdict,
    VacancyEvaluation.summary,
    VacancyEvaluation.matched_json,
    VacancyEvaluation.gaps_json,
    VacancyEvaluation.red_flags_json,
    VacancyEvaluation.salary_fit,
    VacancyEvaluation.seniority_fit,
    VacancyEvaluation.tags_json,
)


_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analytics")


async def run_cpu(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Pure-Python work of the analytics off the event loop, one piece at a time. Threads do not add CPU under the
    GIL: nine report threads at once starved the loop thread and a light request of another page waited 1–2 s; with
    one worker the worst loop lag during a cold build on 10 000 vacancies is ~0.15 s and the page is ready sooner."""
    return await asyncio.get_running_loop().run_in_executor(_POOL, partial(fn, *args, **kwargs))


def _in_scope(vacancies: list[V], evals: dict[int, E], scope: str) -> list[V]:
    if scope == "passed":
        return [v for v in vacancies if v.status != "filtered_out" and not v.hidden]
    if scope == "evaluated":
        return [v for v in vacancies if v.id in evals]
    return vacancies


def _project(ev_rows: Sequence[Any], vac_rows: Sequence[Any]) -> tuple[dict[int, E], list[V]]:
    """Plain rows → the report projections of every vacancy (pure Python, runs in a worker thread). The newest
    current evaluation of a vacancy wins (`ev_rows` come ordered by id)."""
    evals: dict[int, E] = {}
    for row in ev_rows:
        evals[row.vacancy_id] = E(
            vacancy_id=row.vacancy_id,
            score=int(row.score),
            verdict=row.verdict,
            summary=row.summary or "",
            matched=_loads(row.matched_json, []),
            gaps=_loads(row.gaps_json, []),
            red_flags=_loads(row.red_flags_json, []),
            salary_fit=row.salary_fit,
            seniority_fit=row.seniority_fit,
            tags=load_tags(row.tags_json),
        )
    vacancies: list[V] = []
    for r in vac_rows:
        ev = evals.get(r.id)
        tags = merge_tags(load_tags(r.tags_json), ev.tags if ev else {})
        vacancies.append(
            V(
                id=r.id,
                hh_id=r.hh_id,
                name=r.name or "",
                url=r.alternate_url,
                employer_id=r.employer_id,
                employer_name=r.employer_name,
                accredited=r.employer_accredited_it,
                trusted=r.employer_trusted,
                area=r.area_name,
                salary=r.salary_net_rur,
                experience=r.experience_id,
                formats=[str(x) for x in _loads(r.work_format_ids_json, []) if x],
                published_at=as_utc(r.published_at),
                first_seen_at=as_utc(r.first_seen_at),
                archived=bool(r.archived),
                direct=r.type_id == "direct",
                has_test=bool(r.has_test),
                letter_required=bool(r.response_letter_required),
                key_skills=[str(x) for x in _loads(r.key_skills_json, []) if x],
                responses=r.responses_count,
                status=r.pipeline_status,
                prescore=r.prescore,
                score=ev.score if ev else r.score,
                set_id=r.selection_profile_id,
                search_ids=[int(p) for p in (r.source_search_ids or "").split(",") if p.strip().isdigit()],
                hidden=bool(r.hidden),
                tags=tags,
            )
        )
    return evals, vacancies


async def load_base(
    session: AsyncSession, settings: Settings, *, days: int = 0, scope: str = "passed", now: datetime | None = None
) -> Base:
    """Everything the reports need, loaded once; `days` > 0 keeps only vacancies first seen in the last N days,
    `scope` narrows them further (see SCOPES)."""
    if scope not in SCOPES:
        raise ValueError(f"scope должен быть одним из {', '.join(SCOPES)}")
    now = as_utc(now) or utcnow()
    tz = ZoneInfo(settings.timezone or "UTC")
    since = now - timedelta(days=days) if days > 0 else None
    cols = (
        Vacancy.id,
        Vacancy.hh_id,
        Vacancy.name,
        Vacancy.alternate_url,
        Vacancy.employer_id,
        Vacancy.employer_name,
        Vacancy.employer_accredited_it,
        Vacancy.employer_trusted,
        Vacancy.area_name,
        Vacancy.salary_net_rur,
        Vacancy.experience_id,
        Vacancy.work_format_ids_json,
        Vacancy.published_at,
        Vacancy.first_seen_at,
        Vacancy.archived,
        Vacancy.type_id,
        Vacancy.has_test,
        Vacancy.response_letter_required,
        Vacancy.key_skills_json,
        Vacancy.responses_count,
        Vacancy.pipeline_status,
        Vacancy.prescore,
        Vacancy.score,
        Vacancy.selection_profile_id,
        Vacancy.source_search_ids,
        Vacancy.hidden,
        Vacancy.tags_json,
    )
    stmt = select(*cols)
    if since is not None:
        stmt = stmt.where(Vacancy.first_seen_at >= since)
    ev_rows = (
        await session.execute(
            select(*EVAL_COLUMNS).where(VacancyEvaluation.is_current.is_(True)).order_by(VacancyEvaluation.id)
        )
    ).all()
    vac_rows = (await session.execute(stmt)).all()
    # ~1.5 s of Python on 10 000 vacancies (JSON, tags): in a worker thread, the event loop keeps serving meanwhile
    evals, vacancies = await run_cpu(_project, ev_rows, vac_rows)
    search_rows = (
        await session.execute(select(SavedSearch.id, SavedSearch.name, SavedSearch.enabled, SavedSearch.params_json))
    ).all()
    search_names = {int(r.id): str(r.name) for r in search_rows}
    search_texts = [str(_loads(r.params_json, {}).get("text") or "") for r in search_rows if r.enabled]
    set_rows = (await session.execute(select(SelectionProfile.id, SelectionProfile.name))).all()
    set_names = {int(i): str(n) for i, n in set_rows}
    prefs = await get_preferences(session)
    titles: list[str] = list(prefs.target_titles)
    for sid, _ in set_rows:
        for t in (await effective_preferences(session, int(sid))).target_titles:
            if norm(t) not in {norm(x) for x in titles}:
                titles.append(t)
    structure = primary_structure(await primary_resume(session, "hh"), await primary_resume(session, "file"))
    my_skills = [s.name for s in prefs.merged_skills(structure.skills if structure else [])]
    hidden = {str(e) for (e,) in (await session.execute(select(HiddenEmployer.employer_id))).all() if e}
    if days > 0:
        date_from = (now - timedelta(days=days)).astimezone(tz).date().isoformat()
        period = resolve_period(settings, "custom", date_from=date_from, now=now)
    else:
        period = resolve_period(settings, "all", now=now, earliest=date(2000, 1, 1))
    sent = await _load_sent(session, period)
    base = Base(
        vacancies=vacancies,
        evals=evals,
        sent=sent,
        search_names=search_names,
        set_names=set_names,
        titles=titles,
        my_skills=my_skills,
        my_salary=prefs.salary_min,
        hidden_employers=hidden,
        tz=tz,
        now=now,
        my_weights={norm(s.name): int(s.weight) for s in prefs.skills_weights},
        stop_words=list(prefs.stop_words_title),
        search_texts=[s for s in search_texts if s],
    )
    return base.scoped(scope)


# ---- the shared base --------------------------------------------------------------------

# the reports measure ages from base.now («published in the last 3 days»): a base is rebuilt at least this often
BASE_MAX_AGE_SEC = 300
BASE_CACHE_SIZE = 3  # `days` values kept; a base of 10 000 vacancies holds ~30 MB


@dataclass
class _CachedBase:
    base: Base
    generation: int
    built: float


_bases: dict[tuple[Any, ...], _CachedBase] = {}
_building: dict[tuple[Any, ...], asyncio.Task[Base]] = {}


def clear_base_cache() -> None:
    _bases.clear()


async def cached_base(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, *, days: int = 0, scope: str = "passed"
) -> Base:
    """The base for (days, scope), shared by the reports: one «all» base per `days`, the scopes cut from it,
    reused until a committed write to the tables it reads (`app.db.changes`) or BASE_MAX_AGE_SEC. Concurrent
    requests wait for one build; it runs in its own task and session, so a request that goes away does not cancel
    it for the others. A request that comes after a write never gets a base built before it: builds are keyed by
    the data generation too."""
    if scope not in SCOPES:
        raise ValueError(f"scope должен быть одним из {', '.join(SCOPES)}")
    key = (settings.db_url, settings.timezone, days)
    generation = changes.generation()
    hit = _bases.get(key)
    if hit is not None and hit.generation == generation and time.monotonic() - hit.built < BASE_MAX_AGE_SEC:
        return hit.base.scoped(scope)
    build_key = (*key, generation)
    task = _building.get(build_key)
    if task is None:
        task = asyncio.create_task(_build_base(session_factory, settings, key, generation, days))
        # nobody may be left to await a failed build (all requests went away): mark its error as seen
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        _building[build_key] = task
    return (await asyncio.shield(task)).scoped(scope)


async def _build_base(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    key: tuple[Any, ...],
    generation: int,
    days: int,
) -> Base:
    try:
        async with session_factory() as session:
            base = await load_base(session, settings, days=days, scope="all")
        current = _bases.get(key)
        if current is None or current.generation <= generation:  # a build started after a newer write may be done
            _bases.pop(key, None)
            _bases[key] = _CachedBase(base, generation, time.monotonic())
            while len(_bases) > BASE_CACHE_SIZE:
                _bases.pop(next(iter(_bases)))
        return base
    finally:
        _building.pop((*key, generation), None)


# ---- segments ---------------------------------------------------------------------------


def segment_keys(v: V, by: str, base: Base) -> list[tuple[str, str]]:
    """(key, label) pairs of the segments a vacancy belongs to for the given kind (may be several)."""
    if by == "search":
        keys = [(f"s{sid}", base.search_names.get(sid, f"поиск #{sid}")) for sid in v.search_ids]
        return keys or [("none", "вне поисков")]
    if by == "set":
        label = base.set_names.get(v.set_id, "Анкета") if v.set_id else "Анкета"
        return [(f"p{v.set_id or 0}", label)]
    if by == "title":
        return base.title_keys(v) or [("other", "другие названия")]
    if by in GROUPS:
        return [(label, label) for label in v.tags.get(by, [])] or [("none", "без метки")]
    if by == "format":
        return [(f, WORK_FORMAT_LABELS.get(f, f)) for f in v.formats] or [("none", "не указан")]
    if by == "experience":
        return [(v.experience or "none", EXPERIENCE_LABELS.get(v.experience or "", "не указан"))]
    raise ValueError(f"unknown segment kind {by}")


def select_segment(base: Base, by: str | None, key: str | None) -> list[V]:
    if not by or not key or by not in SEGMENT_KINDS:
        return list(base.vacancies)
    return [v for v in base.vacancies if any(k == key for k, _ in segment_keys(v, by, base))]


def _reactions(rows: list[SentRow]) -> dict[str, int]:
    return {
        "sent": len(rows),
        "viewed": sum(1 for r in rows if r.viewed),
        "invited": sum(1 for r in rows if r.invited),
        "discarded": sum(1 for r in rows if r.discarded),
        "reacted": sum(1 for r in rows if r.reacted),
    }


def segment_metrics(vs: list[V], base: Base) -> dict[str, Any]:
    found = len(vs)
    evals = [base.evals[v.id] for v in vs if v.id in base.evals]
    evaluated = len(evals)
    ge50 = sum(1 for e in evals if e.score >= 50)
    ge70 = sum(1 for e in evals if e.score >= 70)
    salaries = [v.salary for v in vs if v.salary]
    p25, median, p75 = _quartiles(salaries)
    responses = [v.responses for v in vs if v.responses is not None]
    exp = Counter(EXPERIENCE_LABELS.get(v.experience or "", "не указан") for v in vs)
    sent_rows = [r for v in vs for r in base.sent_by_vacancy.get(v.id, [])]
    reactions = _reactions(sent_rows)
    return {
        "found": found,
        "active": sum(1 for v in vs if not v.archived),
        "evaluated": evaluated,
        "ge50": ge50,
        "ge70": ge70,
        "ge50_pct": _pct(ge50, evaluated),
        "ge70_pct": _pct(ge70, evaluated),
        "awaiting": sum(1 for v in vs if v.status == "awaiting_claude"),
        "salary_n": len(salaries),
        "salary_p25": p25,
        "salary_median": median,
        "salary_p75": p75,
        "no_salary_pct": _pct(found - len(salaries), found),
        "remote_pct": _pct(sum(1 for v in vs if "REMOTE" in v.formats), found),
        "hybrid_pct": _pct(sum(1 for v in vs if "HYBRID" in v.formats), found),
        "office_pct": _pct(sum(1 for v in vs if "ON_SITE" in v.formats), found),
        "test_pct": _pct(sum(1 for v in vs if v.has_test), found),
        "direct_pct": _pct(sum(1 for v in vs if v.direct), found),
        "letter_required_pct": _pct(sum(1 for v in vs if v.letter_required), found),
        "accredited_pct": _pct(sum(1 for v in vs if v.accredited), found),
        "experience": dict(exp.most_common()),
        "responses_median": _median(responses),
        "low_competition_pct": _pct(sum(1 for x in responses if x < 50), len(responses)),
        **reactions,
        "invited_pct": _pct(reactions["invited"], reactions["sent"]),
        "reacted_pct": _pct(reactions["reacted"], reactions["sent"]),
    }


def segments_report(base: Base, by: str) -> dict[str, Any]:
    if by not in SEGMENT_KINDS:
        raise ValueError(f"by должен быть одним из {', '.join(SEGMENT_KINDS)}")
    groups: dict[str, list[V]] = defaultdict(list)
    labels: dict[str, str] = {}
    for v in base.vacancies:
        for key, label in segment_keys(v, by, base):
            groups[key].append(v)
            labels[key] = label
    items = [{"key": key, "label": labels[key], **segment_metrics(vs, base)} for key, vs in groups.items()]
    items.sort(key=lambda i: (-(i["ge50"] or 0), -(i["found"] or 0), str(i["label"])))
    return {
        "by": by,
        "label": SEGMENT_KINDS[by],
        "total": {"key": "all", "label": "все вакансии", **segment_metrics(base.vacancies, base)},
        "items": items,
    }


# ---- hot vacancies ----------------------------------------------------------------------


def hot_report(
    base: Base,
    settings: Settings,
    *,
    max_responses: int = 50,
    max_age_days: int = 3,
    min_score: int = 50,
    limit: int = 50,
) -> dict[str, Any]:
    """Fresh vacancies with few responses that already look good (Claude score, or the prescore when not yet
    evaluated) and are not handled yet — the list to answer first."""
    since = base.now - timedelta(days=max_age_days)
    items: list[dict[str, Any]] = []
    for v in base.vacancies:
        if v.archived or v.hidden or v.status in HOT_EXCLUDED or v.employer_id in base.hidden_employers:
            continue
        if v.responses is None or v.responses > max_responses:
            continue
        if v.published_at is None or v.published_at < since:
            continue
        ev = base.evals.get(v.id)
        if ev is not None:
            if ev.score < min_score:
                continue
        elif (v.prescore or 0) < settings.prescore_min_for_claude:
            continue
        items.append(
            {
                "id": v.id,
                "hh_id": v.hh_id,
                "name": v.name,
                "employer": v.employer_name,
                "url": v.url,
                "score": ev.score if ev else None,
                "verdict": ev.verdict if ev else None,
                "prescore": v.prescore,
                "responses": v.responses,
                "published_at": v.published_at.isoformat(timespec="minutes"),
                "age_hours": round((base.now - v.published_at).total_seconds() / 3600, 1),
                "salary": v.salary,
                "formats": [WORK_FORMAT_LABELS.get(f, f) for f in v.formats],
                "status": v.status,
                "has_test": v.has_test,
            }
        )
    items.sort(key=lambda i: (-(i["score"] if i["score"] is not None else -1), -(i["prescore"] or 0), i["responses"]))
    return {
        "max_responses": max_responses,
        "max_age_days": max_age_days,
        "min_score": min_score,
        "total": len(items),
        "items": items[:limit],
    }


# ---- skills and gaps --------------------------------------------------------------------


def _in_my_skills(name: str, my_skills: list[str]) -> bool:
    n = norm(name)
    return any(terms_equal(name, s) or contains_term(n, s) or contains_term(norm(s), name) for s in my_skills)


def _unused_skills(my_skills: list[str], demanded: list[str]) -> list[str]:
    """My skills no vacancy asks for: no demanded key `d` (normalized) with `terms_equal(s, d)` or
    `contains_term(d, s)` — through a TermIndex and a set of stems instead of every pair (dozens × thousands)."""
    index = TermIndex(demanded)
    exact = set(demanded)
    # terms_equal: one is the other plus 1–3 characters, the shorter one at least 4 characters long
    shorter_of_demanded = {d[:-cut] for d in demanded for cut in (1, 2, 3) if len(d) - cut >= 4}
    out: list[str] = []
    for s in my_skills:
        ns = norm(s)
        equal = bool(ns) and (
            ns in exact
            or ns in shorter_of_demanded
            or any(ns[:-cut] in exact for cut in (1, 2, 3) if len(ns) - cut >= 4)
        )
        if not equal and not index.matching(s):
            out.append(s)
    return out


def _top_lines(lines: list[str], limit: int = TOP_LINES) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    display: dict[str, str] = {}
    for line in lines:
        key = norm(line).rstrip(".;, ")
        if not key:
            continue
        counter[key] += 1
        display.setdefault(key, line.strip().rstrip(".;, "))
    return [{"text": display[k], "count": c} for k, c in counter.most_common(limit)]


def skills_report(vs: list[V], base: Base) -> dict[str, Any]:
    found = len(vs)
    counter: Counter[str] = Counter()
    display: dict[str, str] = {}
    for v in vs:
        seen: set[str] = set()
        for s in v.key_skills:
            key = norm(s)
            if key and key not in seen:
                seen.add(key)
                counter[key] += 1
                display.setdefault(key, s)
    skills = [
        {
            "name": display[k],
            "count": c,
            "share": _pct(c, found),
            "in_resume": _in_my_skills(display[k], base.my_skills),
        }
        for k, c in counter.most_common(TOP_SKILLS)
    ]
    missing = [s for s in skills if not s["in_resume"]][:TOP_LINES]
    unused = _unused_skills(base.my_skills, list(counter))
    evals = [base.evals[v.id] for v in vs if v.id in base.evals]
    gap_lines = [g for e in evals for g in e.gaps]
    flag_lines = [f for e in evals for f in e.red_flags]
    # what to look for in the gap lines: the segment's key skills by name, the stack labels by their keywords;
    # thousands of skills × thousands of lines — one TermIndex instead of a regex per pair (was ~10 s on 10 000)
    vocab: dict[str, tuple[str, ...]] = {display[k]: (display[k],) for k in counter if len(k) >= 3}
    for label, keywords in STACK.items():
        vocab.setdefault(label, keywords)
    gap_index = TermIndex([norm(g) for g in gap_lines])
    gap_skills: Counter[str] = Counter()
    for label, keywords in vocab.items():
        lines: set[int] = set()
        for kw in keywords:
            lines |= gap_index.matching(kw)
        if lines:
            gap_skills[label] += len(lines)
    return {
        "found": found,
        "with_skills": sum(1 for v in vs if v.key_skills),
        "evaluated": len(evals),
        "skills": skills,
        "missing": missing,
        "unused": unused[:TOP_LINES],
        "gaps": _top_lines(gap_lines),
        "gap_skills": [{"name": n, "count": c} for n, c in gap_skills.most_common(TOP_LINES)],
        "red_flags": _top_lines(flag_lines, 10),
        "matched": _top_lines([m for e in evals for m in e.matched], 10),
    }


# ---- salary -----------------------------------------------------------------------------


def _salary_stats(vs: list[V]) -> dict[str, Any]:
    values = [v.salary for v in vs if v.salary]
    p25, median, p75 = _quartiles(values)
    return {"n": len(values), "of": len(vs), "p25": p25, "median": median, "p75": p75}


def salary_report(vs: list[V], base: Base) -> dict[str, Any]:
    overall = _salary_stats(vs)
    by_format = []
    for fid, label in WORK_FORMAT_LABELS.items():
        sub = [v for v in vs if fid in v.formats]
        if sub:
            by_format.append({"label": label, **_salary_stats(sub)})
    by_experience = [
        {"label": label, **_salary_stats([v for v in vs if v.experience == eid])}
        for eid, label in EXPERIENCE_LABELS.items()
        if any(v.experience == eid for v in vs)
    ]
    bands: list[tuple[str, Callable[[int | None], bool]]] = [
        ("оценка ≥ 70", lambda s: s is not None and s >= 70),
        ("оценка 50–69", lambda s: s is not None and 50 <= s < 70),
        ("оценка < 50", lambda s: s is not None and s < 50),
        ("без оценки", lambda s: s is None),
    ]
    by_score = []
    for label, pred in bands:
        sub = [v for v in vs if pred(v.score)]
        if sub:
            by_score.append({"label": label, **_salary_stats(sub)})
    values = [v.salary for v in vs if v.salary]
    mine = base.my_salary
    at_or_above = sum(1 for s in values if mine is not None and s >= mine)
    evals = [base.evals[v.id] for v in vs if v.id in base.evals]
    top_paid = sorted((v for v in vs if v.salary), key=lambda v: -(v.salary or 0))[:7]
    return {
        "overall": overall,
        "by_format": by_format,
        "by_experience": by_experience,
        "by_score": by_score,
        "buckets": dict(Counter(salary_bucket(v.salary) for v in vs).most_common()),
        "mine": {
            "expected": mine,
            "at_or_above": at_or_above,
            "at_or_above_pct": _pct(at_or_above, len(values)) if mine is not None else None,
        },
        "salary_fit": dict(Counter(e.salary_fit or "unknown" for e in evals).most_common()),
        "seniority_fit": dict(Counter(e.seniority_fit or "unknown" for e in evals).most_common()),
        "top_paid": [
            {
                "id": v.id,
                "name": v.name,
                "employer": v.employer_name,
                "salary": v.salary,
                "score": v.score,
                "url": v.url,
            }
            for v in top_paid
        ],
    }


# ---- funnel -----------------------------------------------------------------------------


def _funnel_keys(r: SentRow, by: str, base: Base) -> list[str]:
    v = base.by_id.get(r.app.vacancy_id)
    if by in SEGMENT_KINDS:
        return [label for _, label in segment_keys(v, by, base)] if v else ["—"]
    if by == "letter":
        return ["с письмом" if (r.app.letter_text or r.app.cover_letter_id) else "без письма"]
    if by == "accredited":
        return ["аккредитованный IT" if v and v.accredited else "не аккредитован"]
    if by == "test":
        return ["с тестовым" if v and v.has_test else "без тестового"]
    if by == "salary":
        return [salary_bucket(v.salary if v else None)]
    if by == "score":
        return [score_bucket(v.score if v else None)]
    raise ValueError(f"unknown funnel dimension {by}")


def _funnel_cell(rows: list[SentRow], now: datetime) -> dict[str, Any]:
    reactions = _reactions(rows)
    threshold = now - timedelta(days=NO_ANSWER_DAYS)
    no_answer = sum(1 for r in rows if not r.reacted and (as_utc(r.app.sent_at) or now) < threshold)
    return {
        **reactions,
        "no_answer": no_answer,
        "pending": reactions["sent"] - reactions["reacted"] - no_answer,
        "viewed_pct": _pct(reactions["viewed"], reactions["sent"]),
        "invited_pct": _pct(reactions["invited"], reactions["sent"]),
        "reacted_pct": _pct(reactions["reacted"], reactions["sent"]),
    }


def funnel_report(base: Base, by: str) -> dict[str, Any]:
    if by not in FUNNEL_DIMENSIONS:
        raise ValueError(f"by должен быть одним из {', '.join(FUNNEL_DIMENSIONS)}")
    groups: dict[str, list[SentRow]] = defaultdict(list)
    for r in base.sent:
        for label in _funnel_keys(r, by, base):
            groups[label].append(r)
    rows = [{"label": label, **_funnel_cell(rs, base.now)} for label, rs in groups.items()]
    rows.sort(key=lambda i: (-int(i["sent"]), str(i["label"])))
    return {
        "by": by,
        "label": FUNNEL_DIMENSIONS[by],
        "overall": _funnel_cell(base.sent, base.now),
        "rows": rows,
        "enough_data": len(base.sent) >= 30,
    }


# ---- employers --------------------------------------------------------------------------


def employers_report(vs: list[V], base: Base, *, limit: int = 30) -> dict[str, Any]:
    groups: dict[str, list[V]] = defaultdict(list)
    for v in vs:
        groups[v.employer_id or f"name:{norm(v.employer_name)}"].append(v)
    items: list[dict[str, Any]] = []
    for group in groups.values():
        first = group[0]
        responses = [v.responses for v in group if v.responses is not None]
        scores = [v.score for v in group if v.score is not None]
        sent_rows = [r for v in group for r in base.sent_by_vacancy.get(v.id, [])]
        firsts = [v.first_seen_at for v in group if v.first_seen_at]
        lasts = [v.published_at for v in group if v.published_at]
        republished = sum(
            1
            for v in group
            if v.published_at and v.first_seen_at and v.published_at > v.first_seen_at + timedelta(days=1)
        )
        items.append(
            {
                "employer_id": first.employer_id,
                "name": first.employer_name or "—",
                "url": f"https://hh.ru/employer/{first.employer_id}" if first.employer_id else None,
                "vacancies": len(group),
                "active": sum(1 for v in group if not v.archived),
                "accredited": first.accredited,
                "trusted": first.trusted,
                "hidden": bool(first.employer_id and first.employer_id in base.hidden_employers),
                "first_seen": min(firsts).isoformat(timespec="minutes") if firsts else None,
                "last_published": max(lasts).isoformat(timespec="minutes") if lasts else None,
                "republished": republished,
                "responses_median": _median(responses),
                "best_score": max(scores) if scores else None,
                **_reactions(sent_rows),
                "items": [
                    {
                        "id": v.id,
                        "name": v.name,
                        "url": v.url,
                        "score": v.score,
                        "prescore": v.prescore,
                        "status": v.status,
                        "salary": v.salary,
                        "responses": v.responses,
                        "published_at": v.published_at.isoformat(timespec="minutes") if v.published_at else None,
                        "archived": v.archived,
                    }
                    for v in sorted(group, key=lambda x: -(x.score if x.score is not None else -1))
                ],
            }
        )
    items.sort(key=lambda i: (-int(i["vacancies"]), -(i["best_score"] if i["best_score"] is not None else -1)))
    return {"total": len(items), "items": items[:limit]}


# ---- trends -----------------------------------------------------------------------------


def _week_start(moment: datetime, tz: ZoneInfo) -> date:
    local = moment.astimezone(tz).date()
    return local - timedelta(days=local.weekday())


def trends_report(vs: list[V], base: Base, *, weeks: int = 12) -> dict[str, Any]:
    this_week = _week_start(base.now, base.tz)
    starts = [this_week - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
    buckets: dict[date, list[V]] = {d: [] for d in starts}
    for v in vs:
        if v.first_seen_at is None:
            continue
        w = _week_start(v.first_seen_at, base.tz)
        if w in buckets:
            buckets[w].append(v)
    sent_weeks: Counter[date] = Counter()
    ids = {v.id for v in vs}
    for r in base.sent:
        if r.app.vacancy_id in ids:
            sent_weeks[r.sent_local.date() - timedelta(days=r.sent_local.weekday())] += 1
    stack_total: Counter[str] = Counter(label for v in vs for label in v.tags.get("stack", []))
    top_tools = [label for label, _ in stack_total.most_common(8)]
    rows = []
    for d in starts:
        group = buckets[d]
        evals = [base.evals[v.id] for v in group if v.id in base.evals]
        rows.append(
            {
                "week": d.isoformat(),
                "new": len(group),
                "evaluated": len(evals),
                "ge50": sum(1 for e in evals if e.score >= 50),
                "sent": sent_weeks.get(d, 0),
                "salary_median": _median([v.salary for v in group if v.salary]),
                "responses_median": _median([v.responses for v in group if v.responses is not None]),
                "tools": {t: sum(1 for v in group if t in v.tags.get("stack", [])) for t in top_tools},
            }
        )
    # drop the empty leading weeks so a young base does not show months of zeros
    while len(rows) > 1 and rows[0]["new"] == 0 and rows[0]["sent"] == 0:
        rows.pop(0)
    return {"weeks": rows, "tools": top_tools, "tags": tags_summary(vs)}


def tags_summary(vs: list[V]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for g in GROUPS:
        counter = Counter(label for v in vs for label in v.tags.get(g, []))
        out[g] = [{"name": n, "count": c, "share": _pct(c, len(vs))} for n, c in counter.most_common(12)]
    return out


# ---- borderline -------------------------------------------------------------------------


def borderline_report(
    base: Base,
    settings: Settings,
    *,
    prescore_margin: int = 5,
    score_margin: int = 5,
    limit: int = 60,
) -> dict[str, Any]:
    """Vacancies that just missed a cut: prescore a few points under PRESCORE_MIN_FOR_CLAUDE (never evaluated)
    or a Claude score a few points under the «maybe» threshold — worth a quick look by eye and a manual send."""
    p_min = settings.prescore_min_for_claude
    s_min = settings.score_threshold_maybe
    items: list[dict[str, Any]] = []
    for v in base.vacancies:
        if v.archived or v.hidden or v.status in ("sent", "queued", "already_applied", "awaiting_claude"):
            continue
        ev = base.evals.get(v.id)
        if ev is not None:
            if s_min - score_margin <= ev.score < s_min:
                items.append({"kind": "score", "value": ev.score, "threshold": s_min, **_border_item(v, ev)})
        elif (
            v.prescore is not None
            and v.status not in ("filtered_out",)
            and p_min - prescore_margin <= v.prescore < p_min
        ):
            items.append({"kind": "prescore", "value": v.prescore, "threshold": p_min, **_border_item(v, None)})
    # closest to the threshold first, then the least crowded
    items.sort(key=lambda i: (i["threshold"] - i["value"], i["responses"] if i["responses"] is not None else 10**6))
    return {
        "prescore_min": p_min,
        "score_min": s_min,
        "prescore_margin": prescore_margin,
        "score_margin": score_margin,
        "total": len(items),
        "items": items[:limit],
    }


def _border_item(v: V, ev: E | None) -> dict[str, Any]:
    return {
        "id": v.id,
        "hh_id": v.hh_id,
        "name": v.name,
        "employer": v.employer_name,
        "url": v.url,
        "status": v.status,
        "salary": v.salary,
        "responses": v.responses,
        "published_at": v.published_at.isoformat(timespec="minutes") if v.published_at else None,
        "formats": [WORK_FORMAT_LABELS.get(f, f) for f in v.formats],
        "summary": (ev.summary if ev is not None else None),
        "gaps": (ev.gaps[:3] if ev is not None else []),
    }


# ---- maintenance -------------------------------------------------------------------------


async def retag_all(session: AsyncSession) -> int:
    """Recomputes the rule tags of every vacancy that has a description (after a vocabulary change)."""
    rows = (
        await session.execute(
            select(Vacancy.id, Vacancy.name, Vacancy.key_skills_json, Vacancy.description_text).where(
                Vacancy.description_text.is_not(None)
            )
        )
    ).all()
    n = 0
    for r in rows:
        tags = rule_tags(r.name, _loads(r.key_skills_json, []), r.description_text)
        row = await session.get(Vacancy, r.id)
        if row is not None:
            row.tags_json = _json(tags)
            n += 1
    await session.flush()
    return n


# ---- recommendations --------------------------------------------------------------------


def name_terms(name: str) -> list[str]:
    """Search-worthy terms of a vacancy title: single words and adjacent pairs, level/format words dropped."""
    words = [w for w in significant_words(name, min_len=2) if w not in LEVEL_WORDS and not w.isdigit()]
    terms = list(dict.fromkeys(words))
    for a, b in zip(words, words[1:], strict=False):
        pair = f"{a} {b}"
        if pair not in terms:
            terms.append(pair)
    return terms


def _covered(term: str, base: Base) -> bool:
    """Already searched for (the term is a whole phrase of an enabled search text) or exactly a target title."""
    t = norm(term).replace("-", " ")
    for text in base.search_texts:
        n = norm(text).replace("-", " ").replace('"', " ").replace("(", " ").replace(")", " ")
        if f" {t} " in f" {n} ":
            return True
    return any(norm(title).replace("-", " ") == t for title in base.titles)


def recommendations_report(base: Base) -> dict[str, Any]:
    evaluated = [v for v in base.vacancies if v.id in base.evals]
    good = [v for v in evaluated if base.evals[v.id].score >= GOOD_SCORE]
    good_ids = {v.id for v in good}

    # title terms → how often they end up with a score ≥ 50 among the evaluated vacancies
    n_by: Counter[str] = Counter()
    hits_by: Counter[str] = Counter()
    for v in evaluated:
        for term in name_terms(v.name):
            n_by[term] += 1
            if v.id in good_ids:
                hits_by[term] += 1
    potential: Counter[str] = Counter()
    for v in base.vacancies:
        for term in name_terms(v.name):
            potential[term] += 1
    stop_norm = {norm(s) for s in base.stop_words}
    searches: list[dict[str, Any]] = []
    stop_words: list[dict[str, Any]] = []
    for term, n in n_by.items():
        rate = hits_by[term] / n
        if n >= 3 and rate >= 0.6 and not _covered(term, base) and term not in stop_norm:
            query = f'"{term}"' if " " in term else term
            searches.append(
                {
                    "term": term,
                    "query": query,
                    "evaluated": n,
                    "good": hits_by[term],
                    "rate": round(rate * 100, 1),
                    "in_base": potential[term],
                }
            )
        if n >= 4 and rate <= 0.15 and " " not in term and term not in stop_norm and term not in LEVEL_WORDS:
            stop_words.append({"term": term, "evaluated": n, "good": hits_by[term], "rate": round(rate * 100, 1)})
    searches.sort(key=lambda i: (-i["rate"], -i["evaluated"], i["term"]))
    stop_words.sort(key=lambda i: (i["rate"], -i["evaluated"], i["term"]))

    # key skills demanded by the good vacancies
    in_good: Counter[str] = Counter()
    in_all: Counter[str] = Counter()
    display: dict[str, str] = {}
    for v in base.vacancies:
        seen: set[str] = set()
        for s in v.key_skills:
            key = norm(s)
            if not key or key in seen:
                continue
            seen.add(key)
            display.setdefault(key, s)
            in_all[key] += 1
            if v.id in good_ids:
                in_good[key] += 1
    weights: list[dict[str, Any]] = []
    resume: list[dict[str, Any]] = []
    for key, cnt in in_good.most_common():
        if cnt < 2:
            break
        name = display[key]
        if _in_my_skills(name, base.my_skills):
            current = next((w for k, w in base.my_weights.items() if terms_equal(k, key)), None)
            if current is None or current < 5:
                weights.append({"name": name, "in_good": cnt, "in_all": in_all[key], "weight": current, "suggested": 5})
        else:
            resume.append({"name": name, "in_good": cnt, "in_all": in_all[key]})
    gaps = skills_report(good, base)["gap_skills"] if good else []

    # where the odds are: segments with the best share of good scores and the least competition
    focus: list[dict[str, Any]] = []
    for by in ("title", "search", "tasks"):
        items = [
            i for i in segments_report(base, by)["items"] if i["evaluated"] >= 3 and i["key"] not in ("other", "none")
        ]
        items.sort(
            key=lambda i: (-(i["ge50_pct"] or 0), i["responses_median"] if i["responses_median"] is not None else 10**6)
        )
        for i in items[:3]:
            focus.append(
                {
                    "by": by,
                    "by_label": SEGMENT_KINDS[by],
                    "key": i["key"],
                    "label": i["label"],
                    "found": i["found"],
                    "evaluated": i["evaluated"],
                    "ge50_pct": i["ge50_pct"],
                    "responses_median": i["responses_median"],
                    "salary_median": i["salary_median"],
                }
            )
    return {
        "evaluated": len(evaluated),
        "good": len(good),
        "enough_data": len(evaluated) >= 20,
        "searches": searches[:10],
        "stop_words": stop_words[:10],
        "weights": weights[:10],
        "resume": resume[:12],
        "gaps": gaps[:10],
        "focus": focus,
    }
