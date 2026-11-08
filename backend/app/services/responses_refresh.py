"""Refresh of the responses counter (job `responses`).

hh.ru gives the number of responses (`counters.responses`) only in search results with `responses_count_enabled=true`;
the full card of a vacancy has no counters. The scan is incremental: a vacancy comes in the results when it is
published (the scan sees it within ~30 minutes) and again only when the employer raises it. Without a refresh the panel
would keep the number of the first half hour, and the responses cap of «Вакансии» would hide too little.

Every RESPONSES_REFRESH_HOURS hours the panel re-reads the results of the enabled searches for the last
RESPONSES_REFRESH_DAYS days and updates the counter of the vacancies it already has. Nothing else changes: new vacancies
are the scan's (it runs the rules and the pre-score), and the statuses, `last_seen_at` and `published_at` stay as they
are. The stored search item gets the new counters too: re-processing a vacancy reads the counter from there.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import as_utc, utcnow
from app.db.models import JobRun, SavedSearch, Vacancy
from app.db.session import release_writes
from app.hh.client import HHClient
from app.hh.errors import HHAuthError, HHCaptchaRequired, HHError
from app.services.audit import audit
from app.services.jobs import FAILED_KEY
from app.services.scanner import search_path
from app.services.searches import MAX_DEPTH, PAGE_SIZE, SearchParams, build_query
from app.services.state import get_system_state, set_pause

if TYPE_CHECKING:  # pragma: no cover
    from app.runtime import Runtime

log = structlog.get_logger("responses_refresh")

JOB = "responses"
MAX_PERIOD_DAYS = 30  # hh.ru: `period` is at most 30 days


def responses_of(item: dict[str, Any]) -> int | None:
    """`counters.responses` of a search item, None when hh.ru did not give it."""
    counters = item.get("counters")
    value = counters.get("responses") if isinstance(counters, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def with_counters(raw_short_json: str | None, counters: dict[str, Any]) -> str | None:
    """The stored search item with the new counters; anything else of it stays (the scan rewrites the whole item when
    it sees the vacancy again). No stored item (a vacancy added by a link) — nothing to keep in step."""
    if not raw_short_json:
        return raw_short_json
    try:
        item = json.loads(raw_short_json)
    except ValueError:
        return raw_short_json
    if not isinstance(item, dict):
        return raw_short_json
    item["counters"] = counters
    return json.dumps(item, ensure_ascii=False)


async def refresh_due(session: AsyncSession, hours: int, now: datetime | None = None) -> bool:
    """Whether the next pass is due: the last one started `hours` ago or earlier, or there was none. A pass that could
    not start (another job of the group was running) leaves no run, so the next tick tries again; a failed one waits
    the whole interval, like a good one — hh.ru is not asked every quarter of an hour while it refuses."""
    last = (
        await session.execute(select(JobRun.started_at).where(JobRun.job == JOB).order_by(desc(JobRun.id)).limit(1))
    ).scalar_one_or_none()
    started = as_utc(last)
    return started is None or (now or utcnow()) - started >= timedelta(hours=hours)


async def _refresh_search(
    session: AsyncSession,
    client: HHClient,
    search: SavedSearch,
    period: int,
    summary: dict[str, Any],
    done: set[str],
) -> None:
    params = SearchParams.model_validate_json(search.params_json or "{}")
    path = await search_path(session, search)
    page = 0
    while True:
        q = build_query(params, page=page, per_page=PAGE_SIZE, period=period)
        await release_writes(session)  # the updates of the previous page must not hold the write lock over the request
        data = await client.public(path, params=q)
        summary["requests"] += 1
        if not isinstance(data, dict):
            break
        items = [i for i in (data.get("items") or []) if isinstance(i, dict) and i.get("id")]
        summary["items"] += len(items)
        # a vacancy several searches return is looked at once per pass
        given = {str(i["id"]): i for i in items if str(i["id"]) not in done and responses_of(i) is not None}
        if given:
            # the columns, not whole rows: a row carries the description and the full card
            rows = await session.execute(
                select(Vacancy.id, Vacancy.hh_id, Vacancy.responses_count, Vacancy.raw_short_json).where(
                    Vacancy.hh_id.in_(list(given))
                )
            )
            for vacancy_id, hh_id, current, raw in rows.all():
                if hh_id is None:  # never: the rows were selected by their hh.ru number
                    continue
                done.add(hh_id)
                item = given[hh_id]
                responses = responses_of(item)
                if responses == current:
                    continue
                await session.execute(
                    update(Vacancy)
                    .where(Vacancy.id == vacancy_id)
                    .values(responses_count=responses, raw_short_json=with_counters(raw, item["counters"]))
                )
                summary["updated"] += 1
        page += 1
        if not items or page >= (data.get("pages") or 0) or page * PAGE_SIZE >= MAX_DEPTH:
            break


async def refresh_responses(runtime: Runtime, *, days: int | None = None) -> dict[str, Any]:
    """Job body: the counters of the known vacancies from the results of the enabled searches for the last days."""
    settings = runtime.settings
    period = max(1, min(MAX_PERIOD_DAYS, days if days is not None else settings.responses_refresh_days))
    client = runtime.hh_client()
    summary: dict[str, Any] = {"days": period, "searches": 0, "requests": 0, "items": 0, "updated": 0, "errors": 0}
    async with runtime.session_factory() as session:
        state = await get_system_state(session)
        if state.paused_reason:
            summary["skipped"] = f"paused: {state.paused_reason}"
            return summary
        searches = (
            (
                await session.execute(
                    # the searches of HireHi have no responses counter (and are not hh.ru's to re-read)
                    select(SavedSearch)
                    .where(SavedSearch.enabled.is_(True), SavedSearch.source != "hirehi")
                    .order_by(SavedSearch.id)
                )
            )
            .scalars()
            .all()
        )
        done: set[str] = set()
        failed: list[str] = []
        for search in searches:
            try:
                await _refresh_search(session, client, search, period, summary, done)
            except (HHAuthError, HHCaptchaRequired) as exc:
                summary["errors"] += 1
                summary["aborted"] = exc.summary()
                if isinstance(exc, HHCaptchaRequired):
                    await set_pause(session, "captcha", runtime.captcha_url)
                await audit(session, "scan", f"Обновление числа откликов остановлено: {exc.summary()}", level="error")
                summary[FAILED_KEY] = f"обновление числа откликов остановлено: {exc.summary()}"
                await session.commit()
                break
            except (HHError, RuntimeError) as exc:
                error = str(exc) if isinstance(exc, RuntimeError) else exc.summary()
                summary["errors"] += 1
                failed.append(f"«{search.name}»: {error}")
                log.info("responses_refresh_search_failed", search_id=search.id, error=error)
            else:
                summary["searches"] += 1
            await session.commit()  # what a search updated stays, whatever the next one does

        from app.services.counters import bump_counter

        await bump_counter(session, settings, "hh_requests", client.requests_made)
        if FAILED_KEY not in summary:
            message = (
                f"Число откликов обновлено у {summary['updated']} вакансий: выдача {summary['searches']} поисков "
                f"за {period} дн., запросов к hh.ru {summary['requests']}"
            )
            if failed:
                message += f"; с ошибкой {len(failed)}: {'; '.join(failed[:3])}"
            await audit(session, "scan", message, level="warning" if failed else "info")
        await session.commit()
    if searches and summary["searches"] == 0 and FAILED_KEY not in summary:
        # hh.ru refused every search: a failed run, so three in a row reach Telegram (job_error)
        summary[FAILED_KEY] = f"все поиски ({len(searches)}) завершились ошибкой: {failed[0]}"
    return summary
