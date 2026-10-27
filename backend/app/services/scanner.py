"""Vacancy scanner: incremental search, dedup, rules, full fetch, pre-score."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import as_utc, utcnow
from app.db.models import HiddenEmployer, Resume, SavedSearch, Vacancy
from app.db.session import release_writes
from app.hh.client import HHClient
from app.hh.errors import HHAuthError, HHCaptchaRequired, HHError, HHNotFound
from app.hh.resume import NormalizedResume
from app.hh.vacancy import VacancyData, merge_full, normalize_vacancy, storable_full, to_rur
from app.services.audit import audit
from app.services.dictionaries import currency_rates, dictionaries
from app.services.jobs import FAILED_KEY
from app.services.preferences import Preferences
from app.services.prescore import prescore
from app.services.rules import RuleResult, RulesContext, apply_rules
from app.services.searches import MAX_DEPTH, PAGE_SIZE, SearchParams, build_query
from app.services.state import get_setting, get_system_state, read_capabilities, set_pause
from app.services.tagging import rule_tags
from app.services.textmatch import norm

if TYPE_CHECKING:  # pragma: no cover
    from app.runtime import Runtime

log = structlog.get_logger("scanner")

ACTIVE_STATUSES = {
    "new",
    "prescored",
    "awaiting_claude",
    "evaluated",
    "suitable",
    "maybe",
    "letter_ready",
    "needs_manual",
}
MAX_FULL_FETCH_ERRORS = 3
FULL_REFRESH_AFTER = timedelta(days=3)


@dataclass
class ScanContext:
    prefs: Preferences
    rules: RulesContext
    weights: list[Any]
    candidate_months: int | None
    prescore_min: int
    stats: dict[str, Any] = field(default_factory=dict)
    # named selection-criteria set this context was built for (None = base questionnaire)
    profile_id: int | None = None
    # lazily built contexts of other sets, shared by all searches of one run
    variants: dict[int | None, ScanContext] = field(default_factory=dict)
    settings: Any = None
    # «Ассистент»: the user applies on hh.ru by hand anyway, so vacancies with a test or an external form are
    # evaluated by Claude like any other candidate (API mode parks them in `needs_manual`)
    manual_to_claude: bool = False


# ---- helpers ----------------------------------------------------------------


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def vacancy_data_from_row(row: Vacancy) -> VacancyData:
    short_raw = _loads(row.raw_short_json, None)
    full_raw = _loads(row.raw_full_json, None)
    short = normalize_vacancy(short_raw, full=False) if isinstance(short_raw, dict) else None
    full = normalize_vacancy(full_raw, full=True) if isinstance(full_raw, dict) else None
    if short and full:
        return merge_full(short, full)
    if full:
        return full
    if short:
        return short
    return VacancyData(hh_id=row.hh_id, name=row.name)


def apply_data_to_row(row: Vacancy, v: VacancyData, rates: dict[str, float]) -> None:
    row.name = v.name or row.name
    row.employer_id = v.employer_id
    row.employer_name = v.employer_name
    row.employer_accredited_it = v.employer_accredited_it
    row.employer_trusted = v.employer_trusted
    row.area_id = v.area_id
    row.area_name = v.area_name
    row.salary_from = v.salary_from
    row.salary_to = v.salary_to
    row.salary_currency = v.salary_currency
    row.salary_gross = v.salary_gross
    row.salary_net_estimate = v.salary_net_estimate()
    row.salary_net_rur = to_rur(row.salary_net_estimate, v.salary_currency, rates)
    row.experience_id = v.experience_id
    row.experience_name = v.experience_name
    row.employment_form_id = v.employment_form_id
    row.employment_form_name = v.employment_form_name
    row.work_format_ids_json = _json(v.work_format_ids)
    row.work_format_names_json = _json(v.work_format_names)
    row.schedule_id = v.schedule_id
    row.address = v.address
    row.metro_json = _json(v.metro_stations)
    row.published_at = v.published_at
    row.archived = v.archived
    row.type_id = v.type_id
    row.response_letter_required = v.response_letter_required
    row.has_test = v.has_test
    if v.test_required is not None:
        row.test_required = v.test_required
    row.response_url = v.response_url
    row.alternate_url = v.alternate_url
    row.apply_alternate_url = v.apply_alternate_url
    if v.key_skills:
        row.key_skills_json = _json(v.key_skills)
    if v.professional_roles:
        row.professional_roles_json = _json(v.professional_roles)
    if v.description_html is not None:
        row.description_html = v.description_html
        row.description_text = v.description_text
        row.tags_json = _json(rule_tags(row.name, _loads(row.key_skills_json, []), v.description_text))
    row.relations_json = _json(v.relations)
    if v.responses_count is not None:
        row.responses_count = v.responses_count


def _set_result(row: Vacancy, result: RuleResult) -> None:
    row.rules_passed = result.passed
    row.rules_reasons_json = _json(result.reasons)
    row.rules_flags_json = _json(result.flags)
    if not result.passed and result.status:
        row.pipeline_status = result.status
        row.pipeline_reason = "; ".join(result.reasons)
        if result.status == "hidden":
            row.hidden = True


async def build_context(
    session: AsyncSession, client: HHClient, settings: Any, *, profile_id: int | None = None
) -> ScanContext:
    from app.services.selection import effective_preferences

    prefs = await effective_preferences(session, profile_id)
    hidden = (await session.execute(select(HiddenEmployer))).scalars().all()
    state = await get_system_state(session)
    policy = settings.direct_policy
    if policy not in ("skip", "keep"):
        policy = "skip" if state.apply_mode == "api" else "keep"
    try:
        dicts = await dictionaries(session, client, settings.dictionaries_ttl_days)
        rates = currency_rates(dicts)
    except Exception as exc:  # noqa: BLE001 - dictionaries are optional for scanning
        log.warning("dictionaries_unavailable", error=str(exc))
        rates = {"RUR": 1.0, "RUB": 1.0}
    rules = RulesContext(
        prefs=prefs,
        hidden_employer_ids={h.employer_id for h in hidden if h.employer_id},
        hidden_employer_names={norm(h.employer_name) for h in hidden if h.employer_name},
        direct_policy=policy,
        currency_rates=rates,
    )
    # the set's own resume if it has one; otherwise the hh resume, otherwise the primary uploaded file
    resume: Resume | None = None
    if profile_id is not None:
        from app.db.models import SelectionProfile

        set_row = await session.get(SelectionProfile, profile_id)
        if set_row is not None and set_row.resume_id:
            resume = await session.get(Resume, set_row.resume_id)
    if resume is None:
        resume = (
            await session.execute(select(Resume).where(Resume.source == "hh", Resume.is_primary.is_(True)).limit(1))
        ).scalar_one_or_none()
    if resume is None:
        resume = (
            await session.execute(select(Resume).where(Resume.source == "file", Resume.is_primary.is_(True)).limit(1))
        ).scalar_one_or_none()
    if resume is not None and resume.source == "file" and not resume.structured_json:
        from app.services.profile import ensure_file_structure

        ensure_file_structure(resume)
    resume_skills: list[str] = []
    months: int | None = None
    if resume is not None and resume.structured_json:
        try:
            norm_resume = NormalizedResume.model_validate_json(resume.structured_json)
            resume_skills = norm_resume.skills
            months = norm_resume.total_experience_months
        except ValueError:
            pass
    if prefs.own_experience_months is not None:
        months = prefs.own_experience_months  # the questionnaire wins over the resume
    ctx = ScanContext(
        prefs=prefs,
        rules=rules,
        weights=prefs.merged_skills(resume_skills),
        candidate_months=months,
        prescore_min=settings.prescore_min_for_claude,
        profile_id=profile_id,
        settings=settings,
        manual_to_claude=state.apply_mode != "api",
    )
    ctx.variants[profile_id] = ctx
    return ctx


async def context_for(
    session: AsyncSession, client: HHClient, settings: Any, ctx: ScanContext, profile_id: int | None
) -> ScanContext:
    """Context for a criteria set, reusing the run's cache (dictionaries/hidden employers are re-read cheaply)."""
    if profile_id in ctx.variants:
        return ctx.variants[profile_id]
    variant = await build_context(session, client, settings, profile_id=profile_id)
    variant.stats = ctx.stats
    variant.variants = ctx.variants
    ctx.variants[profile_id] = variant
    return variant


# ---- upsert -----------------------------------------------------------------


async def upsert_short(
    session: AsyncSession, item: dict[str, Any], search_id: int, ctx: ScanContext
) -> tuple[Vacancy, bool]:
    v = normalize_vacancy(item, full=False)
    row = (await session.execute(select(Vacancy).where(Vacancy.hh_id == v.hh_id))).scalar_one_or_none()
    now = utcnow()
    created = row is None
    if row is None:
        row = Vacancy(
            hh_id=v.hh_id,
            first_seen_at=now,
            pipeline_status="new",
            source_search_ids=",",
            selection_profile_id=ctx.profile_id,
        )
        session.add(row)
    apply_data_to_row(row, v, ctx.rules.currency_rates)
    row.raw_short_json = _json(item)
    row.last_seen_at = now
    tag = f",{search_id},"
    if f",{search_id}," not in row.source_search_ids:
        row.source_search_ids = (row.source_search_ids or ",") + tag.lstrip(",")
    if not created and row.pipeline_status in ACTIVE_STATUSES and not row.user_override:
        # re-found vacancy: react to archive/relations changes
        changed = ("got_response", "got_invitation", "got_rejection", "blacklisted")
        if v.archived or any(r in v.relations for r in changed):
            result = apply_rules(v, ctx.rules)
            if not result.passed:
                _set_result(row, result)
    await session.flush()
    return row, created


# ---- scanning one search ----------------------------------------------------


async def scan_search(session: AsyncSession, client: HHClient, search: SavedSearch, ctx: ScanContext) -> dict[str, Any]:
    params = SearchParams.model_validate_json(search.params_json or "{}")
    stats: dict[str, Any] = {"search_id": search.id, "name": search.name, "requests": 0, "items": 0, "new": 0}

    path = "/vacancies"
    if search.source == "similar_to_resume":
        caps = read_capabilities(await get_system_state(session))
        if caps.get("resume_similar") == "unavailable":
            raise RuntimeError("подходящие вакансии под резюме недоступны приложению (по данным зонда)")
        resume = (
            await session.execute(select(Resume).where(Resume.source == "hh", Resume.is_primary.is_(True)).limit(1))
        ).scalar_one_or_none()
        resume_id = (resume.hh_resume_id if resume else None) or await get_setting(session, "manual_resume_id")
        if not resume_id:
            raise RuntimeError("для источника «под резюме» нужен resume_id (Профиль)")
        path = f"/resumes/{resume_id}/similar_vacancies"

    last_seen = as_utc(search.last_seen_published_at)
    window: dict[str, Any] = (
        {"date_from": last_seen - timedelta(hours=2)}
        if last_seen
        else {"period": params.period_days or ctx.stats.get("first_run_period_days") or 7}
    )

    page = 0
    max_published = last_seen
    warning: str | None = None
    while True:
        q = build_query(params, page=page, per_page=PAGE_SIZE, **window)
        await release_writes(session)  # the previous page's upserts must not hold the write lock across the request
        data = await client.public(path, params=q)
        stats["requests"] += 1
        if not isinstance(data, dict):
            break
        items = [i for i in (data.get("items") or []) if isinstance(i, dict) and i.get("id")]
        found = data.get("found") or 0
        pages = data.get("pages") or 0
        stats["found"] = found
        for item in items:
            row, created = await upsert_short(session, item, search.id, ctx)
            stats["items"] += 1
            if created:
                stats["new"] += 1
                if search.watch_only:
                    await _watch_new(session, ctx, search, row)
            if row.published_at and (max_published is None or row.published_at > max_published):
                max_published = row.published_at
        page += 1
        if not items or page >= pages or page * PAGE_SIZE >= MAX_DEPTH:
            if found > MAX_DEPTH:
                warning = f"найдено {found} вакансий, обработаны первые {MAX_DEPTH}; сузьте запрос"
            break

    search.last_run_at = utcnow()
    search.last_seen_published_at = max_published
    search.last_found = stats.get("found")
    search.last_new = stats["new"]
    search.last_requests = stats["requests"]
    search.last_error = None
    search.last_warning = warning
    if warning:
        stats["warning"] = warning
    await session.flush()
    return stats


async def _watch_new(session: AsyncSession, ctx: ScanContext, search: SavedSearch, row: Vacancy) -> None:
    """A watch-only search: the vacancy is parked as `watched` (no rules, prescore or packets) and queued for the
    bot as a link (event search_new, chunked ten per message). The first run of a search is a backfill: the
    vacancies are stored, nothing is sent — like the first read of a company site."""
    from app.services.notify import emit_event

    row.pipeline_status = "watched"
    row.pipeline_reason = f"поиск-наблюдение «{search.name}»"
    if search.last_run_at is None:
        return
    await emit_event(
        session,
        ctx.settings,
        "search_new",
        {
            "vacancy_id": row.id,
            "hh_id": row.hh_id,
            "title": row.name,
            "employer": row.employer_name,
            "area": row.area_name,
            "address": row.address,
            "metro": _loads(row.metro_json, []),
            "salary_from": row.salary_from,
            "salary_to": row.salary_to,
            "currency": row.salary_currency,
            "url": row.alternate_url or f"https://hh.ru/vacancy/{row.hh_id}",
            "search": search.name,
            "search_id": search.id,
        },
    )


# ---- processing new vacancies ----------------------------------------------


async def fetch_full(client: HHClient, row: Vacancy) -> VacancyData | None:
    """Returns merged data or None when the vacancy is gone (404)."""
    try:
        raw = await client.public(f"/vacancies/{row.hh_id}")
    except HHNotFound:
        return None
    if not isinstance(raw, dict):
        raise HHError("unexpected vacancy payload")
    row.raw_full_json = _json(storable_full(raw))
    row.full_fetched_at = utcnow()
    row.full_fetch_errors = 0
    return vacancy_data_from_row(row)


NOT_A_TWIN = ("filtered_out", "hidden", "watched")


async def find_duplicate(session: AsyncSession, row: Vacancy) -> Vacancy | None:
    """An earlier, still relevant vacancy of the same employer with the same title: a repost or a second posting.

    Closed (archived) twins do not count — after the freshness check a repost of a closed vacancy is a real new
    one; twins dropped by the rules do not count either (the rules will judge the new one the same way), nor do
    twins parked by a watch-only search — nobody evaluated them, so a regular search must not inherit «дубль»."""
    if not row.name or not (row.employer_id or row.employer_name):
        return None
    stmt = select(Vacancy).where(Vacancy.id != row.id, Vacancy.archived.is_(False), Vacancy.id < row.id)
    if row.employer_id:
        stmt = stmt.where(Vacancy.employer_id == row.employer_id)
    else:
        stmt = stmt.where(Vacancy.employer_name == row.employer_name)
    name_key = norm(row.name)
    for twin in (await session.execute(stmt.order_by(Vacancy.id))).scalars():
        if norm(twin.name) != name_key or twin.pipeline_status in NOT_A_TWIN:
            continue
        if twin.duplicate_of_id:
            original = await session.get(Vacancy, twin.duplicate_of_id)
            if original is not None and not original.archived and original.pipeline_status not in NOT_A_TWIN:
                return original
        return twin
    return None


APPLIED_STATUSES = ("sent", "queued", "already_applied")


async def process_vacancy(session: AsyncSession, client: HHClient, row: Vacancy, ctx: ScanContext) -> str:
    """Duplicate check → short rules → full fetch → full rules → prescore. Returns the resulting pipeline status."""
    if not row.user_override and row.duplicate_of_id is None:
        twin = await find_duplicate(session, row)
        if twin is not None:
            row.duplicate_of_id = twin.id
            row.pipeline_status = "skipped"
            row.pipeline_reason = f"дубль вакансии {twin.hh_id}: " + (
                "отклик уже отправлен" if twin.pipeline_status in APPLIED_STATUSES else "уже есть в базе"
            )
            await audit(session, "vacancies", f"Дубль «{row.name}» → {twin.hh_id}", vacancy_id=row.id)
            return row.pipeline_status
    v = vacancy_data_from_row(row)
    if not row.user_override:
        result = apply_rules(v, ctx.rules)
        if not result.passed:
            _set_result(row, result)
            return row.pipeline_status

    needs_fetch = not row.raw_full_json or (
        row.full_fetched_at and utcnow() - (as_utc(row.full_fetched_at) or utcnow()) > FULL_REFRESH_AFTER
    )
    if needs_fetch:
        await release_writes(session)  # no open write transaction across the hh request (one SQLite writer)
        try:
            fetched = await fetch_full(client, row)
        except (HHAuthError, HHCaptchaRequired):
            raise
        except HHError as exc:
            row.full_fetch_errors += 1
            log.warning("full_fetch_failed", hh_id=row.hh_id, attempt=row.full_fetch_errors, error=exc.summary())
            if row.full_fetch_errors >= MAX_FULL_FETCH_ERRORS:
                row.pipeline_status = "filtered_out"
                row.pipeline_reason = f"не удалось загрузить описание ({exc.summary()})"
            return row.pipeline_status
        if fetched is None:
            row.pipeline_status = "filtered_out"
            row.pipeline_reason = "вакансия недоступна (404)"
            return row.pipeline_status
        v = fetched
        apply_data_to_row(row, v, ctx.rules.currency_rates)

    result = apply_rules(v, ctx.rules) if not row.user_override else RuleResult(status=None)
    if not result.passed:
        _set_result(row, result)
        return row.pipeline_status
    row.rules_passed = True
    row.rules_reasons_json = _json(result.reasons)
    row.rules_flags_json = _json(result.flags)

    ps = prescore(
        v,
        ctx.prefs,
        weights=ctx.weights,
        candidate_months=ctx.candidate_months,
        currency_rates=ctx.rules.currency_rates,
    )
    row.prescore = ps.score
    row.prescore_notes_json = _json(ps.notes)
    if row.user_override:
        row.pipeline_status = "prescored"
        row.pipeline_reason = "оставлена пользователем"
    elif result.needs_manual and not ctx.manual_to_claude:
        row.pipeline_status = "needs_manual"
        row.pipeline_reason = "нужен тест на hh.ru" if v.test_required else "отклик на внешнем сайте"
    elif ps.score >= ctx.prescore_min:
        row.pipeline_status = "prescored"
        row.pipeline_reason = None
    else:
        row.pipeline_status = "not_suitable"
        row.pipeline_reason = f"предварительная оценка {ps.score} ниже порога {ctx.prescore_min}"
    return row.pipeline_status


async def process_new(session: AsyncSession, client: HHClient, ctx: ScanContext, limit: int) -> dict[str, Any]:
    rows = (
        (
            await session.execute(
                select(Vacancy)
                .where(Vacancy.pipeline_status == "new")
                .order_by(Vacancy.published_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    stats: dict[str, int] = {
        "processed": 0,
        "prescored": 0,
        "not_suitable": 0,
        "filtered_out": 0,
        "needs_manual": 0,
        "other": 0,
        "left_new": 0,
    }
    for row in rows:
        row_ctx = ctx
        if row.selection_profile_id != ctx.profile_id:
            row_ctx = await context_for(session, client, ctx.settings, ctx, row.selection_profile_id)
        status = await process_vacancy(session, client, row, row_ctx)
        stats["processed"] += 1
        if status in stats:
            stats[status] += 1
        elif status == "new":
            stats["left_new"] += 1
        else:
            stats["other"] += 1
        await session.flush()
    remaining = (await session.execute(select(Vacancy.id).where(Vacancy.pipeline_status == "new").limit(1))).first()
    stats["pending_new"] = 1 if remaining else 0
    return stats


# ---- the job ----------------------------------------------------------------


async def run_scan(runtime: Runtime, *, search_ids: list[int] | None = None, only_due: bool = False) -> dict[str, Any]:
    """Job body: scans selected/due searches, then processes `new` vacancies (bounded by SCAN_MAX_NEW_PER_RUN)."""
    settings = runtime.settings
    client = runtime.hh_client()
    summary: dict[str, Any] = {"searches": [], "errors": 0}
    async with runtime.session_factory() as session:
        state = await get_system_state(session)
        if state.paused_reason:
            summary["skipped"] = f"paused: {state.paused_reason}"
            return summary
        ctx = await build_context(session, client, settings)
        ctx.stats["first_run_period_days"] = settings.scan_first_run_period_days
        await session.commit()

        query = select(SavedSearch).order_by(SavedSearch.id)
        if search_ids:
            query = query.where(SavedSearch.id.in_(search_ids))
        else:
            query = query.where(SavedSearch.enabled.is_(True))
        searches = (await session.execute(query)).scalars().all()
        now = utcnow()
        for search in searches:
            if only_due and search.last_run_at is not None:
                due_at = (as_utc(search.last_run_at) or now) + timedelta(minutes=search.interval_min)
                if due_at > now:
                    continue
            try:
                search_ctx = await context_for(session, client, settings, ctx, search.selection_profile_id)
                stats = await scan_search(session, client, search, search_ctx)
                summary["searches"].append(stats)
                message = f"Поиск «{search.name}»: найдено {stats.get('found', 0)}, новых {stats['new']}"
                await audit(session, "scan", message, search_id=search.id, requests=stats["requests"])
            except (HHAuthError, HHCaptchaRequired) as exc:
                search.last_error = exc.summary()
                search.last_run_at = utcnow()
                summary["errors"] += 1
                summary["aborted"] = exc.summary()
                if isinstance(exc, HHCaptchaRequired):
                    await set_pause(session, "captcha", runtime.captcha_url)
                await audit(session, "scan", f"Сканирование остановлено: {exc.summary()}", level="error")
                await session.commit()
                summary[FAILED_KEY] = f"сканирование остановлено: {exc.summary()}"
                return summary
            except (HHError, RuntimeError) as exc:
                search.last_error = str(exc) if isinstance(exc, RuntimeError) else exc.summary()
                search.last_run_at = utcnow()
                summary["errors"] += 1
                summary["searches"].append({"search_id": search.id, "name": search.name, "error": search.last_error})
                await audit(
                    session,
                    "scan",
                    f"Поиск «{search.name}»: ошибка {search.last_error}",
                    level="warning",
                    search_id=search.id,
                )
            await session.commit()

        try:
            processed = await process_new(session, client, ctx, settings.scan_max_new_per_run)
            summary["processed"] = processed
            state = await get_system_state(session)
            state.last_scan_at = utcnow()
            await audit(
                session,
                "scan",
                f"Обработано новых вакансий: {processed['processed']} (в кандидаты {processed['prescored']}, "
                f"ниже порога {processed['not_suitable']}, отфильтровано {processed['filtered_out']})",
            )
        except (HHAuthError, HHCaptchaRequired) as exc:
            summary["aborted"] = exc.summary()
            summary["errors"] += 1
            summary[FAILED_KEY] = f"обработка новых вакансий остановлена: {exc.summary()}"
            if isinstance(exc, HHCaptchaRequired):
                await set_pause(session, "captcha", runtime.captcha_url)
            await audit(session, "scan", f"Обработка остановлена: {exc.summary()}", level="error")
        await session.commit()

        if settings.batch_auto_create:
            # new candidates go straight into packets for /hh-batch
            from app.exchange.packets import PacketError, build_batches

            try:
                created = await build_batches(session, settings)
                summary["batches_created"] = [b.batch_id for b in created]
                await session.commit()
            except PacketError as exc:
                summary["batches_skipped"] = str(exc)
                await session.rollback()
        from app.services.counters import bump_counter

        await bump_counter(session, settings, "hh_requests", client.requests_made)
        await session.commit()
    summary["hh_requests"] = client.requests_made
    runs = summary["searches"]
    if runs and all("error" in r for r in runs) and FAILED_KEY not in summary:
        # hh.ru unreachable or refusing every search: a failed run, so three in a row reach Telegram (job_error)
        summary[FAILED_KEY] = f"все поиски ({len(runs)}) завершились ошибкой: {runs[0]['error']}"
    return summary


# a single «Переоценить» leaves these statuses as they are (see reprocess_keeps_status)
KEPT_ON_REPROCESS = ("sent", "queued", "already_applied", "awaiting_claude")


async def reprocess_keeps_status(session: AsyncSession, row: Vacancy) -> bool:
    """A vacancy with a response (sent, on its way, already on hh.ru, an assist page waiting for «Да, отправлено»),
    one in a packet waiting for /hh-batch, one with a current Claude verdict: the rules must not turn a sent vacancy
    into «отфильтрована», a prescore must not re-pack an evaluated one for a second letter (reprocess_all skips both
    for the same reason). «Переразобрать в Claude» is the way to a new evaluation."""
    from app.db.models import Application, VacancyEvaluation

    if row.pipeline_status in KEPT_ON_REPROCESS:
        return True
    open_app = (
        await session.execute(
            select(Application.id)
            .where(
                Application.vacancy_id == row.id,
                Application.status.in_(("queued", "sending", "needs_manual", "sent")),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if open_app is not None:
        return True
    verdict = (
        await session.execute(
            select(VacancyEvaluation.id)
            .where(VacancyEvaluation.vacancy_id == row.id, VacancyEvaluation.is_current.is_(True))
            .limit(1)
        )
    ).scalar_one_or_none()
    return verdict is not None


async def reprocess_vacancy(
    session: AsyncSession, client: HHClient, settings: Any, row: Vacancy, *, refetch: bool
) -> str:
    keep = await reprocess_keeps_status(session, row)
    if keep and not refetch:
        return row.pipeline_status
    ctx = await build_context(session, client, settings, profile_id=row.selection_profile_id)
    if keep:
        # only the card is refreshed; a vacancy gone from hh.ru (404) keeps its status too
        await release_writes(session)
        fetched = await fetch_full(client, row)
        if fetched is not None:
            apply_data_to_row(row, fetched, ctx.rules.currency_rates)
        return row.pipeline_status
    if refetch:
        row.raw_full_json = None
        row.full_fetched_at = None
    if row.pipeline_status in ("filtered_out", "not_suitable", "prescored", "needs_manual", "new", "hidden"):
        row.pipeline_status = "new"
        row.hidden = False
    return await process_vacancy(session, client, row, ctx)


REPROCESS_STATUSES = ("prescored", "not_suitable", "filtered_out", "needs_manual", "new")


async def reprocess_after_criteria_change(runtime: Runtime, *, profile_id: int | None = None) -> bool:
    """Re-evaluates «ниже порога» and friends in the background after the questionnaire (profile_id None) or a set
    changed (REPROCESS_ON_CRITERIA_CHANGE) — only the vacancies of that set; skipped while a scan or another
    re-evaluation runs. Returns True if started."""
    from app.services.jobs import JobBusy
    from app.services.state import auth_public_state, get_hh_auth

    if not runtime.settings.reprocess_on_criteria_change:
        return False
    async with runtime.session_factory() as session:
        if not auth_public_state(await get_hh_auth(session))["logged_in"]:
            return False
    if runtime.jobs.conflicting("reprocess") is not None:
        return False
    try:
        await runtime.jobs.start("reprocess", lambda: reprocess_all(runtime, profile_id=profile_id, scoped=True))
    except JobBusy:
        return False
    log.info("reprocess_after_criteria_change", profile_id=profile_id)
    return True


async def reprocess_all(
    runtime: Runtime,
    *,
    statuses: tuple[str, ...] = REPROCESS_STATUSES,
    refetch: bool = False,
    cancel_pending: bool = False,
    profile_id: int | None = None,
    scoped: bool = False,
) -> dict[str, Any]:
    """Job body `reprocess`: re-runs rules and prescore for every vacancy in the given statuses with the current
    criteria (its own selection set). Uses stored vacancy JSON, so it needs hh.ru only for vacancies that never
    got a full card (or with refetch=True). With cancel_pending the packets waiting for /hh-batch are cancelled
    first (inside the job, so the scanner cannot re-pack them in between) and their vacancies are re-evaluated too.
    With scoped=True only the vacancies of the set `profile_id` (None = base questionnaire) are touched.

    Two kinds of vacancies keep their status whatever the filter: those with a current Claude verdict (their
    «ниже порога» came from the evaluation, not from the rules — a prescore must not overwrite it and re-pack
    them) and those with an assist application waiting for the user's confirmation (`needs_manual` of the
    apply flow, not of the rules)."""
    from app.db.models import Application, VacancyEvaluation

    settings = runtime.settings
    client = runtime.hh_client()
    summary: dict[str, Any] = {"processed": 0, "by_status": {}, "errors": 0, "kept_evaluated": 0, "kept_assist": 0}
    async with runtime.session_factory() as session:
        state = await get_system_state(session)
        if state.paused_reason:
            summary["skipped"] = f"paused: {state.paused_reason}"
            return summary
        if cancel_pending:
            from app.exchange.packets import cancel_pending_batches

            summary["cancelled_batches"] = await cancel_pending_batches(session, settings)
            await session.commit()
        ctx = await build_context(session, client, settings)
        stmt = select(Vacancy.id).where(Vacancy.pipeline_status.in_(statuses), Vacancy.hidden.is_(False))
        if scoped and profile_id is None:
            stmt = stmt.where(Vacancy.selection_profile_id.is_(None))
        elif scoped:
            stmt = stmt.where(Vacancy.selection_profile_id == profile_id)
        ids = (await session.execute(stmt.order_by(Vacancy.id))).scalars().all()
        evaluated = set(
            (
                await session.execute(
                    select(VacancyEvaluation.vacancy_id).where(VacancyEvaluation.is_current.is_(True))
                )
            ).scalars()
        )
        in_assist = set(
            (
                await session.execute(
                    select(Application.vacancy_id).where(
                        Application.mode == "assist", Application.status == "needs_manual"
                    )
                )
            ).scalars()
        )
        for n, vid in enumerate(ids, 1):
            if vid in in_assist:
                summary["kept_assist"] += 1
                continue
            if vid in evaluated:
                summary["kept_evaluated"] += 1
                continue
            row = await session.get(Vacancy, vid)
            if row is None:
                continue
            row_ctx = await context_for(session, client, settings, ctx, row.selection_profile_id)
            if refetch:
                row.raw_full_json = None
                row.full_fetched_at = None
            row.pipeline_status = "new"
            row.pipeline_reason = None
            try:
                status = await process_vacancy(session, client, row, row_ctx)
            except (HHAuthError, HHCaptchaRequired) as exc:
                summary["aborted"] = exc.summary()
                if isinstance(exc, HHCaptchaRequired):
                    await set_pause(session, "captcha", runtime.captcha_url)
                await session.commit()
                break
            except HHError as exc:
                summary["errors"] += 1
                log.warning("reprocess_failed", vacancy=row.hh_id, error=exc.summary())
                status = row.pipeline_status
            summary["processed"] += 1
            summary["by_status"][status] = summary["by_status"].get(status, 0) + 1
            if n % 25 == 0:
                await session.commit()
        await audit(
            session,
            "scan",
            f"Переоценка по текущим критериям: {summary['processed']} вакансий → "
            + ", ".join(f"{k} {v}" for k, v in sorted(summary["by_status"].items())),
        )
        from app.services.counters import bump_counter

        await bump_counter(session, settings, "hh_requests", client.requests_made)
        await session.commit()
    summary["hh_requests"] = client.requests_made
    return summary
