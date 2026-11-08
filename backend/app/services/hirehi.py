"""HireHi (hirehi.ru) — the second source of «Вакансии».

A HireHi vacancy goes the way of an hh.ru one: the rules and the pre-score of its criteria set, packets for Claude,
the letter, the bot, the statistics. What differs:

- Searches: a saved search with `source = hirehi` holds HireHiParams (app/hirehi/parse.py) in `params_json`. The
  section, the city hub, the formats and the levels go to the address of a listing page; the words are checked by the
  panel in the title. `search=` and `sort=` (closed in robots.txt) are never used.
- Reading, carefully: robots.txt first; a search not more often than once an hour (SEARCH_MIN_INTERVAL); the first
  listing pages only, newest first, until a vacancy the panel has seen (FIRST_RUN_PAGES on the first run, MAX_PAGES
  after); the cards only of new vacancies that passed the rules on the listing data (CARDS_PER_RUN); PAUSE_SEC between
  two requests, one at a time; the public User-Agent of the panel, without the e-mail; no API (/api/ is closed), no
  POST.
- A refusal (a DDoS-Guard challenge, HTTP 403 or 429, a redirect off the site) is an error of the source: the reading
  stops for BLOCK_HOURS and the bot is told once (event `job_error`). Nothing is worked around.
- A vacancy HireHi took from hh.ru («Отклик на hh» on the card) is no copy: it goes through hh.ru (route_to_hh) — the
  hh.ru vacancy already in the panel, or the one the hh.ru API finds among the employer's recent ones (by the hh.ru
  number from the logo, else by the company name), added like «По ссылке» but judged by the rules like any search
  result; the HireHi row becomes its duplicate. Not found — the row is skipped with the reason, and the log and its
  history name the employer's vacancy that came nearest.
- Closed: the card is gone (404) or its validThrough passed (check_closed, once a day). The tasks of hh.ru never touch
  these vacancies: they have no hh_id, and freshness / responses / the apply queue take `source = hh` only.
- Applying: by hand on the HireHi page (the assistant: the letter is copied, the page opens, «Да, отправлено»). The
  original link is given by HireHi to a signed-in user only, so the page of the vacancy on HireHi is where it starts.
"""

from __future__ import annotations

import asyncio
import json
import time
import weakref
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import SavedSearch, Vacancy
from app.db.session import release_writes
from app.hh.client import HHClient
from app.hh.errors import HHAuthError, HHCaptchaRequired, HHError
from app.hh.vacancy import normalize_vacancy
from app.hirehi.parse import (
    BASE,
    CATEGORIES,
    CITIES,
    FORMATS,
    HOST,
    LEVELS,
    SOURCE,
    SUBCATEGORIES,
    Card,
    HireHiParams,
    LayoutChanged,
    ListingItem,
    card_path,
    company_query,
    item_reason,
    parse_card,
    parse_listing,
    same_employer,
    title_scores,
)
from app.net import HTTP_ERRORS, error_text
from app.services.audit import audit
from app.services.jobs import FAILED_KEY
from app.services.robots_rules import RobotsRules
from app.services.sources import is_hh
from app.services.state import get_setting, get_system_state, set_pause, set_setting

if TYPE_CHECKING:  # pragma: no cover
    from app.runtime import Runtime
    from app.services.scanner import ScanContext

log = structlog.get_logger("hirehi")

JOB = "hirehi_sync"
JOB_LABEL = "чтение HireHi"
FETCH_TIMEOUT = 60.0
# between two requests to HireHi, whoever makes them (the job, «Проверить», «Переоценить»); read at call time — the
# tests set 0
PAUSE_SEC = 3.0
FIRST_RUN_PAGES = 1
MAX_PAGES = 3
SEARCH_MIN_INTERVAL = 60
BLOCK_HOURS = 6
STATE_KEY = "hirehi_state"
CHECK_EVERY = timedelta(hours=20)
STALE_DAYS = 2
MAX_CARD_ERRORS = 3
# the hh.ru twin of a HireHi vacancy: published at most this long before HireHi listed it (and a little after: the
# clocks of the two sites), with the title at least this similar (title_scores; HireHi lists a vacancy of hh.ru within
# about two hours of its publication)
TWIN_BEFORE = timedelta(days=14)
TWIN_AFTER = timedelta(hours=12)
TITLE_MATCH = 0.6
# a vacancy already in the panel is the twin without asking hh.ru only when published this near the time HireHi listed
# it: a farther one may be a sibling of the same employer with the same core of the title («Frontend Developer (B2B)»
# for «Frontend developer»), and the panel does not know all of the employer's vacancies — hh.ru does
TWIN_NEAR = timedelta(days=1)
# the search of hh.ru for the twin: the window [published - TWIN_BEFORE, published + TWIN_AFTER], newest first from its
# end, up to TWIN_PAGES pages of 100 while none is similar (a big employer publishes hundreds a week); hh.ru searches
# the last HH_SEARCH_DAYS days
TWIN_PAGES = 3
HH_SEARCH_DAYS = 30
# statuses the closed check looks at: waiting for a decision or ready to apply (as freshness.CHECK_STATUSES)
CHECK_STATUSES = ("new", "prescored", "evaluated", "suitable", "maybe", "letter_ready", "needs_manual")

GONE_REASON = "закрыта на HireHi (страница удалена)"
EXPIRED_REASON = "закрыта на HireHi (срок публикации истёк)"
HH_ROUTED_REASON = "вакансия с hh.ru: идёт через hh.ru"
HH_NOT_FOUND_REASON = "вакансия с hh.ru: на hh.ru её не нашлось (закрыта или название другое)"
CARD_FAILED_REASON = "не удалось прочитать карточку HireHi"
_CHALLENGE = ("ddos-guard", "challenge", "captcha", "checking your browser", "проверка браузера")


class HireHiError(Exception):
    """HireHi did not give a page (the network, a status, a changed markup): the run notes it, the next one tries."""


class HireHiBlocked(HireHiError):
    """HireHi refused the panel (a DDoS-Guard challenge, 403, 429, a redirect off the site): BLOCK_HOURS of rest."""


# ---- requests ----------------------------------------------------------------------------------------------------

# per HTTP client of a runtime (weak: a closed runtime takes its lock along; an id() could be reused by the next one)
_locks: weakref.WeakKeyDictionary[httpx.AsyncClient, asyncio.Lock] = weakref.WeakKeyDictionary()
_last_request: weakref.WeakKeyDictionary[httpx.AsyncClient, float] = weakref.WeakKeyDictionary()


def _lock(http: httpx.AsyncClient) -> asyncio.Lock:
    lock = _locks.get(http)
    if lock is None:
        lock = _locks[http] = asyncio.Lock()
    return lock


def looks_like_challenge(text: str | None) -> bool:
    """A browser check instead of the page: the ordinary pages of HireHi have none of these words (2026-11-08)."""
    low = (text or "")[:60000].lower()
    return any(word in low for word in _CHALLENGE)


async def fetch(http: httpx.AsyncClient, settings: Settings, path: str, *, allow_missing: bool = False) -> str | None:
    """GET of a page of hirehi.ru (`path` with its query): one request at a time, PAUSE_SEC after the previous one.
    None for a 404 when `allow_missing` (a removed vacancy card)."""
    async with _lock(http):
        wait = PAUSE_SEC - (time.monotonic() - _last_request.get(http, float("-inf")))
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            resp = await http.get(
                BASE + path,
                headers={"User-Agent": settings.public_user_agent, "Accept-Language": "ru,en;q=0.8"},
                timeout=httpx.Timeout(FETCH_TIMEOUT),
                follow_redirects=True,
            )
        except HTTP_ERRORS as exc:
            raise HireHiError(f"HireHi не ответил: {error_text(exc)}") from exc
        finally:
            _last_request[http] = time.monotonic()
    host = (resp.url.host or "").lower()
    if host not in (HOST, "www." + HOST):
        raise HireHiBlocked(f"HireHi перенаправил на {host or 'другой адрес'}")
    if resp.status_code in (403, 429):
        check = ": проверка браузера DDoS-Guard" if looks_like_challenge(resp.text) else ""
        raise HireHiBlocked(f"HireHi ответил HTTP {resp.status_code}{check}")
    if resp.status_code == 404 and allow_missing:
        return None
    if resp.status_code != 200:
        if looks_like_challenge(resp.text):
            raise HireHiBlocked(f"HireHi показал проверку браузера (HTTP {resp.status_code})")
        raise HireHiError(f"HireHi ответил HTTP {resp.status_code} на {path}")
    return resp.text


async def fetch_robots(http: httpx.AsyncClient, settings: Settings) -> RobotsRules:
    """robots.txt of HireHi; without it nothing is read (a missing file would allow everything — the panel waits)."""
    text = await fetch(http, settings, "/robots.txt")
    return RobotsRules.parse(text or "", settings.public_user_agent)


async def read_listing(http: httpx.AsyncClient, settings: Settings, robots: RobotsRules, path: str) -> Any:
    if not robots.allowed(path):
        raise HireHiError(f"{path}: закрыто в robots.txt HireHi")
    page = await fetch(http, settings, path) or ""
    try:
        return parse_listing(page)
    except LayoutChanged as exc:
        if looks_like_challenge(page):
            raise HireHiBlocked("HireHi показал проверку браузера вместо выдачи") from exc
        raise HireHiError(f"{exc} ({path})") from exc


async def read_card(http: httpx.AsyncClient, settings: Settings, robots: RobotsRules, path: str) -> Card | None:
    """The card of a vacancy; None when HireHi removed the page (404)."""
    if not robots.allowed(path):
        raise HireHiError(f"{path}: закрыто в robots.txt HireHi")
    page = await fetch(http, settings, path, allow_missing=True)
    if page is None:
        return None
    try:
        return parse_card(page)
    except LayoutChanged as exc:
        if looks_like_challenge(page):
            raise HireHiBlocked("HireHi показал проверку браузера вместо карточки") from exc
        raise HireHiError(f"{exc} ({path})") from exc


# ---- the state: a refusal and the closed check ----------------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    d = as_utc(dt)
    return d.isoformat(timespec="seconds") if d else None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


async def get_state(session: AsyncSession) -> dict[str, Any]:
    state = await get_setting(session, STATE_KEY)
    return state if isinstance(state, dict) else {}


def blocked_until(state: dict[str, Any], now: datetime | None = None) -> datetime | None:
    until = _parse_iso(state.get("blocked_until"))
    return until if until is not None and until > (now or utcnow()) else None


def _local_time(settings: Settings, moment: datetime) -> str:
    return moment.astimezone(ZoneInfo(settings.timezone or "UTC")).strftime("%d.%m %H:%M")


async def block(session: AsyncSession, settings: Settings, reason: str, now: datetime | None = None) -> datetime:
    """HireHi refused the panel: no reading for BLOCK_HOURS, a journal entry, the bot told once per refusal."""
    from app.services.notify import emit_event

    moment = now or utcnow()
    state = await get_state(session)
    already = blocked_until(state, moment) is not None
    until = moment + timedelta(hours=BLOCK_HOURS)
    state.update({"blocked_until": _iso(until), "reason": reason, "blocked_at": _iso(moment)})
    await set_setting(session, STATE_KEY, state)
    message = f"{reason}. Панель не читает HireHi до {_local_time(settings, until)} и ничего не обходит"
    await audit(session, "hirehi", f"HireHi закрыл доступ: {message}", level="error")
    if not already:
        await emit_event(session, settings, "job_error", {"job": JOB_LABEL, "error": message, "failures": 1})
    log.warning("hirehi_blocked", reason=reason, until=_iso(until))
    return until


async def state_view(session: AsyncSession) -> dict[str, Any]:
    state = await get_state(session)
    until = blocked_until(state)
    return {
        "blocked_until": _iso(until),
        "reason": state.get("reason") if until else None,
        "checked_at": state.get("checked_at"),
    }


# ---- searches ----------------------------------------------------------------------------------------------------


def params_of(search: SavedSearch) -> HireHiParams:
    try:
        return HireHiParams.model_validate_json(search.params_json or "{}")
    except ValueError:
        return HireHiParams()


def search_due(search: SavedSearch, now: datetime) -> bool:
    if search.last_run_at is None:
        return True
    interval = max(search.interval_min or SEARCH_MIN_INTERVAL, SEARCH_MIN_INTERVAL)
    return (as_utc(search.last_run_at) or now) + timedelta(minutes=interval) <= now


def options() -> dict[str, Any]:
    """What the search form offers (the sections, the city hubs, the formats and the levels of the site)."""
    return {
        "categories": [{"id": k, "name": v} for k, v in CATEGORIES.items()],
        "subcategories": sorted(({"id": k, "name": v} for k, v in SUBCATEGORIES.items()), key=lambda x: x["name"]),
        "cities": [{"id": k, "name": v} for k, v in CITIES.items()],
        "formats": [{"id": k, "name": v} for k, v in FORMATS.items()],
        "levels": [{"id": k, "name": v} for k, v in LEVELS.items()],
        "min_interval": SEARCH_MIN_INTERVAL,
    }


def _item_row(item: ListingItem, reason: str | None) -> dict[str, Any]:
    pub = item.published_at
    return {
        "id": item.id,
        "name": item.title,
        "employer_name": item.company,
        "format": item.format,
        "level": item.level,
        "category": item.category,
        "salary": item.salary,
        "published_at": pub.isoformat() if pub else None,
        "alternate_url": BASE + item.path if item.path else None,
        "reason": reason,
    }


async def preview(runtime: Runtime, params: HireHiParams, *, page: int = 1) -> dict[str, Any]:
    """«Проверить» of a HireHi search: one listing page, nothing stored; every vacancy says whether the panel's
    filters keep it."""
    http, settings = runtime.http, runtime.settings
    async with runtime.session_factory() as session:
        until = blocked_until(await get_state(session))
    if until is not None:
        raise HireHiBlocked(f"HireHi закрыл доступ, панель ждёт до {_local_time(settings, until)}")
    try:
        robots = await fetch_robots(http, settings)
        listing = await read_listing(http, settings, robots, params.path(page))
    except HireHiBlocked as exc:
        async with runtime.session_factory() as session:
            await block(session, settings, str(exc))
            await session.commit()
        raise
    items = [_item_row(i, item_reason(i, params)) for i in listing.items]
    return {
        "found": listing.total,
        "page": page,
        "has_more": listing.has_more,
        "url": BASE + params.path(page),
        "matched": sum(1 for i in items if i["reason"] is None),
        "items": items,
    }


# ---- the rows ----------------------------------------------------------------------------------------------------


def card_of(row: Vacancy) -> Card | None:
    if not row.raw_full_json:
        return None
    try:
        raw = json.loads(row.raw_full_json)
    except ValueError:
        return None
    return Card.from_raw(raw) if isinstance(raw, dict) else None


def path_of(row: Vacancy) -> str | None:
    """The card path of a HireHi row: the listing item, else the stored card, else the link."""
    if row.raw_short_json:
        try:
            raw = json.loads(row.raw_short_json)
        except ValueError:
            raw = None
        if isinstance(raw, dict) and card_path(raw.get("path")):
            return card_path(raw.get("path"))
    card = card_of(row)
    if card is not None and card.path:
        return card.path
    return card_path(row.alternate_url)


def _tag(row: Vacancy, search_id: int) -> None:
    if f",{search_id}," not in (row.source_search_ids or ","):
        row.source_search_ids = (row.source_search_ids or ",") + f"{search_id},"


def _apply(row: Vacancy, rates: dict[str, float]) -> None:
    from app.services.scanner import apply_data_to_row, vacancy_data_from_row

    apply_data_to_row(row, vacancy_data_from_row(row), rates)


async def upsert_item(
    session: AsyncSession, item: ListingItem, search: SavedSearch, rates: dict[str, float]
) -> tuple[Vacancy, bool]:
    row = (
        await session.execute(select(Vacancy).where(Vacancy.source == SOURCE, Vacancy.source_id == item.id))
    ).scalar_one_or_none()
    now = utcnow()
    created = row is None
    if row is None:
        row = Vacancy(
            hh_id=None,
            source=SOURCE,
            source_id=item.id,
            first_seen_at=now,
            pipeline_status="new",
            source_search_ids=",",
            selection_profile_id=search.selection_profile_id,
        )
        session.add(row)
    row.raw_short_json = json.dumps(item.raw(), ensure_ascii=False)
    card = card_of(row)
    if card is not None and card.gone_at:
        # listed again: the page is back (the check saw a 404 once); the status stays what the check made it
        back = Card(card.posting, card.apply_label, card.breadcrumbs, card.data)
        row.raw_full_json = json.dumps(back.raw(), ensure_ascii=False)
    _apply(row, rates)
    row.last_seen_at = now
    _tag(row, search.id)
    await session.flush()
    return row, created


def _close(row: Vacancy, reason: str) -> None:
    row.archived = True
    row.pipeline_status = "filtered_out"
    row.pipeline_reason = reason
    row.batch_id = None


def _store_card(row: Vacancy, card: Card, rates: dict[str, float]) -> None:
    row.raw_full_json = json.dumps(card.raw(), ensure_ascii=False)
    row.full_fetched_at = utcnow()
    row.full_fetch_errors = 0
    _apply(row, rates)


async def read_search(
    session: AsyncSession,
    http: httpx.AsyncClient,
    settings: Settings,
    robots: RobotsRules,
    search: SavedSearch,
    ctx: ScanContext,
) -> dict[str, Any]:
    """The listing pages of one search, newest first, until a vacancy this search has already seen (a row it found, or
    the newest publication of its previous run; a vacancy another search found does not stop it); new matching
    vacancies become rows `new` (or `watched`)."""
    from app.services.scanner import _watch_new

    params = params_of(search)
    stats: dict[str, Any] = {"search_id": search.id, "name": search.name, "requests": 0, "items": 0, "new": 0}
    stats["not_matched"] = 0
    pages = FIRST_RUN_PAGES if search.last_run_at is None else MAX_PAGES
    # like the first scan of hh.ru (SCAN_FIRST_RUN_PERIOD_DAYS): an older vacancy of a listing is not taken
    oldest = utcnow() - timedelta(days=max(1, settings.scan_first_run_period_days))
    watermark = as_utc(search.last_seen_published_at)
    newest = watermark
    page = 1
    while page <= pages:
        path = params.path(page)
        await release_writes(session)  # the rows of the previous page must not hold the write lock over the request
        listing = await read_listing(http, settings, robots, path)
        stats["requests"] += 1
        if page == 1:
            stats["found"] = listing.total
        ids = [i.id for i in listing.items]
        tag = f",{search.id},"
        known = {
            source_id
            for source_id, tags in (
                await session.execute(
                    select(Vacancy.source_id, Vacancy.source_search_ids).where(
                        Vacancy.source == SOURCE, Vacancy.source_id.in_(ids)
                    )
                )
            ).all()
            if tag in (tags or "")
        }
        reached = False
        for item in listing.items:
            stats["items"] += 1
            published = item.published_at
            if published is not None and (newest is None or published > newest):
                newest = published
            if item.id in known or (watermark is not None and published is not None and published <= watermark):
                reached = True
            if published is not None and published < oldest:
                stats["old"] = stats.get("old", 0) + 1
                continue
            if item_reason(item, params) is not None or not item.path or not robots.allowed(item.path):
                stats["not_matched"] += 1
                continue
            row, created = await upsert_item(session, item, search, ctx.rules.currency_rates)
            if created:
                stats["new"] += 1
                if search.watch_only:
                    await _watch_new(session, ctx, search, row)
        if reached or not listing.has_more or not listing.items:
            break
        page += 1
    search.last_run_at = utcnow()
    search.last_seen_published_at = newest
    search.last_found = stats.get("found")
    search.last_new = stats["new"]
    search.last_requests = stats["requests"]
    search.last_error = None
    search.last_warning = None
    await session.flush()
    return stats


CardReader = Callable[[Vacancy], Awaitable[Card | None]]


async def process_row(
    session: AsyncSession,
    settings: Settings,
    hh_client: HHClient,
    row: Vacancy,
    ctx: ScanContext,
    *,
    read: CardReader | None = None,
    summary: dict[str, Any] | None = None,
) -> str:
    """One HireHi row through the pipeline: the duplicate check and the rules on the listing data, the card (only now,
    `read`), then a vacancy of hh.ru goes through hh.ru, any other — the rules of the card and the pre-score."""
    from app.services.scanner import process_vacancy

    stats = summary if summary is not None else {}
    card = card_of(row)
    if card is None:
        status = await process_vacancy(session, hh_client, row, ctx)
        if status != "new" or read is None:
            return status
        try:
            fetched = await read(row)
        except HireHiBlocked:
            raise
        except HireHiError as exc:
            row.full_fetch_errors += 1
            log.info("hirehi_card_failed", source_id=row.source_id, attempt=row.full_fetch_errors, error=str(exc))
            stats["card_errors"] = stats.get("card_errors", 0) + 1
            if row.full_fetch_errors >= MAX_CARD_ERRORS:
                row.pipeline_status = "filtered_out"
                row.pipeline_reason = f"{CARD_FAILED_REASON} ({exc})"
            return row.pipeline_status
        stats["cards"] = stats.get("cards", 0) + 1
        if fetched is None:
            _close(row, GONE_REASON)
            await audit(session, "vacancies", f"Вакансия HireHi удалена с сайта: «{row.name}»", vacancy_id=row.id)
            return row.pipeline_status
        _store_card(row, fetched, ctx.rules.currency_rates)
        card = fetched
    if card.from_hh and not row.user_override:
        return await route_to_hh(session, settings, hh_client, row, ctx, summary=stats)
    return await process_vacancy(session, hh_client, row, ctx)


async def process_new(
    session: AsyncSession,
    runtime: Runtime,
    hh_client: HHClient,
    robots: RobotsRules,
    ctx: ScanContext,
    summary: dict[str, Any],
) -> None:
    """The new HireHi rows: the cards of at most CARDS_PER_RUN of them per run (the rest wait for the next one)."""
    from app.services.scanner import context_for

    settings, http = runtime.settings, runtime.http
    limit = max(0, settings.hirehi_cards_per_run)
    rows = (
        (
            await session.execute(
                select(Vacancy)
                .where(Vacancy.source == SOURCE, Vacancy.pipeline_status == "new")
                .order_by(Vacancy.published_at.desc(), Vacancy.id.desc())
            )
        )
        .scalars()
        .all()
    )

    async def reader(row: Vacancy) -> Card | None:
        path = path_of(row)
        if not path:
            raise HireHiError("у вакансии нет адреса карточки")
        await release_writes(session)  # no open write transaction across the request
        return await read_card(http, settings, robots, path)

    for row in rows:
        row_ctx = ctx
        if row.selection_profile_id != ctx.profile_id:
            row_ctx = await context_for(session, hh_client, settings, ctx, row.selection_profile_id)
        can_read = summary.get("cards", 0) + summary.get("card_errors", 0) < limit
        status = await process_row(
            session, settings, hh_client, row, row_ctx, read=reader if can_read else None, summary=summary
        )
        summary["processed"] = summary.get("processed", 0) + 1
        by_status = summary.setdefault("by_status", {})
        by_status[status] = by_status.get(status, 0) + 1
        await session.flush()


# ---- a vacancy of hh.ru ------------------------------------------------------------------------------------------


async def find_hh_twin(session: AsyncSession, row: Vacancy) -> Vacancy | None:
    """The hh.ru vacancy already in the panel that a HireHi row repeats: the same employer (the hh.ru number from the
    logo, else a name that holds the HireHi one — same_employer), published at most TWIN_NEAR before HireHi listed it
    (a farther one may be a sibling of the same employer: then hh.ru is asked, it sees them all), the title similar
    (HireHi rewrites titles — title_scores). The best score, then the nearest in time, then the share of the whole
    title: HireHi drops exactly the brackets, so the real twin often has a smaller share of its whole title than a
    sibling without them."""
    published = as_utc(row.published_at) or utcnow()
    stmt = select(Vacancy).where(
        Vacancy.source == "hh",
        Vacancy.published_at >= published - TWIN_NEAR,
        Vacancy.published_at <= published + TWIN_AFTER,
    )
    name = (row.employer_name or "").strip()
    if row.employer_id:
        stmt = stmt.where(Vacancy.employer_id == row.employer_id)
    elif name:
        # the hh.ru name may be longer («Онлайн-школа Звезда» for «Звезда»): narrowed here (lower() of the connection
        # knows the Cyrillic case), judged by same_employer
        stmt = stmt.where(func.lower(Vacancy.employer_name).contains(name.lower(), autoescape=True))
    else:
        return None
    best: tuple[tuple[float, float, float], Vacancy] | None = None
    for cand in (await session.execute(stmt)).scalars():
        if not row.employer_id and not same_employer(name, cand.employer_name):
            continue
        score, full = title_scores(row.name, cand.name)
        if score < TITLE_MATCH:
            continue
        gap = abs(((as_utc(cand.published_at) or published) - published).total_seconds())
        key = (score, -gap, full)
        if best is None or key > best[0]:
            best = (key, cand)
    return best[1] if best is not None else None


async def settle_twin(session: AsyncSession, row: Vacancy, twin: Vacancy) -> Vacancy:
    """The twin of a HireHi row against the duplicates the scan of hh.ru made, whichever way it was found: a duplicate
    of this very HireHi row (same title, the HireHi row came first) goes back to work — hh.ru is where the vacancy goes
    now; a duplicate of another hh.ru vacancy gives way to that one. In find_hh_twin alone it missed a twin published
    more than TWIN_NEAR before: the base does not look so far, hh.ru gives back its existing row, and the two rows
    stayed duplicates of each other."""
    if twin.duplicate_of_id == row.id:
        twin.duplicate_of_id = None
        twin.pipeline_status = "new"
        twin.pipeline_reason = None
    elif twin.duplicate_of_id:
        original = await session.get(Vacancy, twin.duplicate_of_id)
        if original is not None and is_hh(original):
            return original
    return twin


async def fetch_hh_twin(
    session: AsyncSession,
    settings: Settings,
    client: HHClient,
    row: Vacancy,
    ctx: ScanContext,
    *,
    trace: dict[str, Any] | None = None,
) -> Vacancy | None:
    """The hh.ru vacancy of a HireHi row through the hh.ru API (GET /vacancies published around the time HireHi listed
    it): of the employer by its hh.ru number from the logo, else of every employer whose name holds
    the HireHi one (`text` + `search_field=company_name`: HireHi shortens names, and one name may be several
    employers' — same_employer and the title tell them apart). The best title_scores, then the nearest in time (HireHi
    lists a vacancy of hh.ru within hours of its publication), then the share of the whole title. Found — the panel's
    row of it (an existing one, or a new `new` one, as «По ссылке» makes it, but without «оставить»: the rules judge
    it). `trace` gets how hh.ru was asked and the employer's vacancy nearest by the title (the log and the history of a
    miss)."""
    from app.services.scanner import _json, apply_data_to_row

    seen = trace if trace is not None else {}
    if row.employer_id:
        query = {"employer_id": row.employer_id}
    elif text := company_query(row.employer_name):
        query = {"text": text, "search_field": "company_name"}
    else:
        seen["by"] = "no employer"
        return None
    seen["by"] = "employer_id" if row.employer_id else "company_name"
    await release_writes(session)  # no open write transaction across the hh.ru requests
    published = as_utc(row.published_at) or utcnow()
    now = utcnow()
    # newest first from the end of the window: the later vacancies of a busy employer do not push the twin off
    start = max(published - TWIN_BEFORE, now - timedelta(days=HH_SEARCH_DAYS))
    end = min(published + TWIN_AFTER, now)
    if end <= start:
        return None
    best: tuple[tuple[float, float, float], dict[str, Any]] | None = None
    nearest: tuple[float, str, str] | None = None
    for page in range(TWIN_PAGES):
        data = await client.public(
            "/vacancies",
            params={
                **query,
                "date_from": start.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "date_to": end.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "per_page": 100,
                "page": page,
                "order_by": "publication_time",
                "responses_count_enabled": "true",
            },
        )
        data = data if isinstance(data, dict) else {}
        seen["pages"] = page + 1
        for item in data.get("items") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            short = normalize_vacancy(item, full=False)
            if short.archived:
                continue
            if not row.employer_id and not same_employer(row.employer_name, short.employer_name):
                continue
            hh_published = short.published_at
            if hh_published is not None and hh_published > published + TWIN_AFTER:
                continue
            score, full = title_scores(row.name, short.name)
            seen["candidates"] = seen.get("candidates", 0) + 1
            if nearest is None or score > nearest[0]:
                nearest = (score, short.name or "", str(item["id"]))
            if score < TITLE_MATCH:
                continue
            gap = abs(((hh_published or published) - published).total_seconds())
            key = (score, -gap, full)
            if best is None or key > best[0]:
                best = (key, item)
        pages = data.get("pages")
        if best is not None or not isinstance(pages, int) or page + 1 >= pages:
            break
    if nearest is not None:
        seen["nearest"] = {"name": nearest[1], "hh_id": nearest[2], "score": round(nearest[0], 2)}
    if best is None:
        return None
    item = best[1]
    hh_id = str(item["id"])
    existing = (await session.execute(select(Vacancy).where(Vacancy.hh_id == hh_id))).scalar_one_or_none()
    if existing is not None:
        return existing
    hh_row = Vacancy(
        hh_id=hh_id,
        source="hh",
        first_seen_at=utcnow(),
        pipeline_status="new",
        source_search_ids=row.source_search_ids or ",",
        selection_profile_id=row.selection_profile_id,
    )
    session.add(hh_row)
    apply_data_to_row(hh_row, normalize_vacancy(item, full=False), ctx.rules.currency_rates)
    hh_row.raw_short_json = _json(item)
    hh_row.last_seen_at = utcnow()
    await session.flush()
    return hh_row


async def route_to_hh(
    session: AsyncSession,
    settings: Settings,
    client: HHClient,
    row: Vacancy,
    ctx: ScanContext,
    *,
    summary: dict[str, Any] | None = None,
) -> str:
    """A HireHi vacancy taken from hh.ru goes through hh.ru: the HireHi row becomes a duplicate (`skipped`) of the hh.ru
    vacancy — the one in the panel or the one the hh.ru API finds, settled against the duplicates of the scan
    (settle_twin); a new one runs the pipeline of hh.ru right away. An hh.ru error leaves the row `new` for the next run
    (MAX_CARD_ERRORS times)."""
    from app.services.scanner import context_for, process_vacancy

    stats = summary if summary is not None else {}
    twin = await find_hh_twin(session, row)
    created = False
    trace: dict[str, Any] = {}
    if twin is None:
        try:
            twin = await fetch_hh_twin(session, settings, client, row, ctx, trace=trace)
        except HHCaptchaRequired:
            raise
        except HHAuthError as exc:
            # no token of the application (no keys of hh.ru, revoked): the row waits as `new`, the next HireHi
            # vacancies go on; nothing is counted against it — the network was not the problem
            stats["hh_unavailable"] = stats.get("hh_unavailable", 0) + 1
            log.info("hirehi_hh_twin_auth", source_id=row.source_id, error=exc.summary())
            return row.pipeline_status
        except HHError as exc:
            row.full_fetch_errors += 1
            log.info("hirehi_hh_twin_failed", source_id=row.source_id, error=exc.summary())
            if row.full_fetch_errors >= MAX_CARD_ERRORS:
                row.pipeline_status = "skipped"
                row.pipeline_reason = f"{HH_NOT_FOUND_REASON}: hh.ru {exc.summary()}"
            return row.pipeline_status
        created = twin is not None and twin.pipeline_status == "new" and twin.full_fetched_at is None
    if twin is None:
        row.pipeline_status = "skipped"
        row.pipeline_reason = HH_NOT_FOUND_REASON
        stats["hh_not_found"] = stats.get("hh_not_found", 0) + 1
        # what was asked and what came nearest: a miss can be checked by hand
        log.info(
            "hirehi_hh_twin_not_found",
            source_id=row.source_id,
            title=row.name,
            employer=row.employer_name,
            employer_id=row.employer_id,
            **trace,
        )
        nearest = trace.get("nearest")
        hint = ""
        if nearest:
            score = f"{nearest['score']:.2f}".replace(".", ",")
            hint = f" (ближе всех у работодателя: «{nearest['name']}», сходство {score})"
        await audit(session, "vacancies", f"HireHi: «{row.name}» с hh.ru, на hh.ru не нашлась{hint}", vacancy_id=row.id)
        return row.pipeline_status
    twin = await settle_twin(session, row, twin)
    if twin.hh_id:
        _tag_all(twin, row)
    row.duplicate_of_id = twin.id
    row.pipeline_status = "skipped"
    row.pipeline_reason = f"{HH_ROUTED_REASON} (вакансия {twin.hh_id})"
    stats["via_hh"] = stats.get("via_hh", 0) + 1
    await session.flush()
    await audit(session, "vacancies", f"HireHi: «{row.name}» с hh.ru → вакансия {twin.hh_id}", vacancy_id=row.id)
    if created or twin.pipeline_status == "new":
        twin_ctx = await context_for(session, client, settings, ctx, twin.selection_profile_id)
        await process_vacancy(session, client, twin, twin_ctx)
    return row.pipeline_status


def _tag_all(target: Vacancy, row: Vacancy) -> None:
    """The hh.ru vacancy remembers the HireHi searches that found it (the search filter of «Вакансии» shows it)."""
    for part in (row.source_search_ids or "").split(","):
        if part.strip().isdigit():
            _tag(target, int(part))


# ---- closed vacancies --------------------------------------------------------------------------------------------


async def check_closed(
    session: AsyncSession,
    http: httpx.AsyncClient,
    settings: Settings,
    robots: RobotsRules,
    rates: dict[str, float],
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Once a day: a HireHi vacancy waiting for a decision is closed when its validThrough passed (no request) or its
    card is gone (404). The card is re-read only for those no listing showed for STALE_DAYS and not checked for as
    long, at most HIREHI_CHECK_PER_RUN. Then the unconfirmed responses of the closed ones are cancelled after the same
    grace as for hh.ru (freshness.cancel_unconfirmed)."""
    from app.services.freshness import cancel_unconfirmed

    moment = now or utcnow()
    cap = settings.hirehi_check_per_run if limit is None else limit
    summary: dict[str, Any] = {"expired": 0, "gone": 0, "checked": 0, "check_errors": 0}
    rows = (
        (
            await session.execute(
                select(Vacancy)
                .where(
                    Vacancy.source == SOURCE,
                    Vacancy.pipeline_status.in_(CHECK_STATUSES),
                    Vacancy.archived.is_(False),
                    Vacancy.hidden.is_(False),
                )
                .order_by(Vacancy.last_seen_at.asc())
            )
        )
        .scalars()
        .all()
    )
    stale_since = moment - timedelta(days=STALE_DAYS)
    to_read: list[Vacancy] = []
    for row in rows:
        card = card_of(row)
        valid = card.valid_through if card else None
        if valid is not None and valid <= moment:
            _close(row, EXPIRED_REASON)
            summary["expired"] += 1
            await audit(session, "vacancies", f"Закрыта на HireHi (срок истёк): «{row.name}»", vacancy_id=row.id)
            continue
        seen = as_utc(row.last_seen_at)
        checked = as_utc(row.full_fetched_at)
        if (seen is None or seen < stale_since) and (checked is None or checked < stale_since):
            to_read.append(row)
    for row in to_read[: max(0, cap)]:
        path = path_of(row)
        if not path:
            continue
        await release_writes(session)  # closed rows are committed before the next request
        try:
            card = await read_card(http, settings, robots, path)
        except HireHiBlocked:
            raise
        except HireHiError as exc:
            summary["check_errors"] += 1
            log.info("hirehi_check_failed", source_id=row.source_id, error=str(exc))
            continue
        summary["checked"] += 1
        if card is None:
            old = card_of(row)
            if old is not None:
                gone = Card(old.posting, old.apply_label, old.breadcrumbs, old.data, gone_at=_iso(moment))
                row.raw_full_json = json.dumps(gone.raw(), ensure_ascii=False)
            _close(row, GONE_REASON)
            summary["gone"] += 1
            await audit(session, "vacancies", f"Закрыта на HireHi (удалена): «{row.name}»", vacancy_id=row.id)
            continue
        _store_card(row, card, rates)
        if row.archived:  # the fresh card's validThrough passed
            _close(row, EXPIRED_REASON)
            summary["expired"] += 1
    await release_writes(session)
    summary["responses_cancelled"] = await cancel_unconfirmed(session, moment, source=SOURCE)
    return summary


# ---- the job -------------------------------------------------------------------------------------------------------


async def _due_searches(session: AsyncSession, search_ids: list[int] | None, only_due: bool) -> list[SavedSearch]:
    stmt = select(SavedSearch).where(SavedSearch.source == SOURCE).order_by(SavedSearch.id)
    if search_ids:
        stmt = stmt.where(SavedSearch.id.in_(search_ids))
    else:
        stmt = stmt.where(SavedSearch.enabled.is_(True))
    searches = list((await session.execute(stmt)).scalars().all())
    if only_due and not search_ids:
        now = utcnow()
        searches = [s for s in searches if search_due(s, now)]
    return searches


def _check_due(state: dict[str, Any], now: datetime) -> bool:
    checked = _parse_iso(state.get("checked_at"))
    return checked is None or now - checked >= CHECK_EVERY


async def sync_due(runtime: Runtime) -> bool:
    """Whether the tick has anything to do: a due search or the daily check, HireHi not refusing, no pause."""
    async with runtime.session_factory() as session:
        if (await get_system_state(session)).paused_reason:
            return False
        state = await get_state(session)
        now = utcnow()
        if blocked_until(state, now) is not None:
            return False
        has_rows = (
            await session.execute(select(Vacancy.id).where(Vacancy.source == SOURCE).limit(1))
        ).scalar_one_or_none() is not None
        if has_rows and _check_due(state, now):
            return True
        return bool(await _due_searches(session, None, True))


async def sync_hirehi(
    runtime: Runtime, *, search_ids: list[int] | None = None, only_due: bool = True, check: bool | None = None
) -> dict[str, Any]:
    """Job `hirehi_sync`: robots.txt → the due searches → the cards of the new vacancies (and the hh.ru route) →
    the closed check once a day → packets for Claude."""
    from app.services.scanner import build_context

    settings, http = runtime.settings, runtime.http
    now = utcnow()
    summary: dict[str, Any] = {"searches": [], "errors": 0}
    async with runtime.session_factory() as session:
        system = await get_system_state(session)
        if system.paused_reason:
            summary["skipped"] = f"paused: {system.paused_reason}"
            return summary
        state = await get_state(session)
        until = blocked_until(state, now)
        if until is not None:
            summary["skipped"] = f"HireHi закрыл доступ, панель ждёт до {_iso(until)}"
            return summary
        # the ids only: the rows are read again by the session that writes them (a row of this closed session would
        # take no change — its last_run_at would never be saved and every run would be a first one)
        due_ids = [s.id for s in await _due_searches(session, search_ids, only_due)]
        has_rows = (
            await session.execute(select(Vacancy.id).where(Vacancy.source == SOURCE).limit(1))
        ).scalar_one_or_none() is not None
        do_check = has_rows and (_check_due(state, now) if check is None else check)
        pending = (
            await session.execute(
                select(Vacancy.id).where(Vacancy.source == SOURCE, Vacancy.pipeline_status == "new").limit(1)
            )
        ).scalar_one_or_none() is not None
    if not due_ids and not do_check and not pending:
        summary["skipped"] = "нет поисков HireHi к чтению"
        return summary
    try:
        robots = await fetch_robots(http, settings)
    except HireHiError as exc:
        if isinstance(exc, HireHiBlocked):
            async with runtime.session_factory() as session:
                await block(session, settings, str(exc), now)
                await session.commit()
        summary[FAILED_KEY] = f"robots.txt HireHi: {exc}"
        return summary

    client = runtime.hh_client()
    blocked: str | None = None
    async with runtime.session_factory() as session:
        ctx = await build_context(session, client, settings)
        await session.commit()
        searches = [s for s in [await session.get(SavedSearch, sid) for sid in due_ids] if s is not None]
        for search in searches:
            try:
                stats = await read_search(session, http, settings, robots, search, ctx)
                summary["searches"].append(stats)
                await audit(
                    session,
                    "scan",
                    f"Поиск HireHi «{search.name}»: на сайте {stats.get('found') or 0}, новых {stats['new']}",
                    search_id=search.id,
                    requests=stats["requests"],
                )
            except HireHiBlocked as exc:
                blocked = str(exc)
                search.last_error = blocked
                await block(session, settings, blocked, now)
                await session.commit()
                break
            except HireHiError as exc:
                search.last_error = str(exc)
                search.last_run_at = utcnow()
                summary["errors"] += 1
                summary["searches"].append({"search_id": search.id, "name": search.name, "error": str(exc)})
                await audit(session, "scan", f"Поиск HireHi «{search.name}»: ошибка {exc}", level="warning")
            await session.commit()

        if blocked is None:
            try:
                await process_new(session, runtime, client, robots, ctx, summary)
            except HireHiBlocked as exc:
                blocked = str(exc)
                await block(session, settings, blocked, now)
            except HHCaptchaRequired as exc:
                summary["aborted"] = exc.summary()
                await set_pause(session, "captcha", runtime.captcha_url)
                await audit(session, "scan", f"HireHi: hh.ru остановил проверку дублей: {exc.summary()}", level="error")
            await session.commit()

        if blocked is None and do_check:
            try:
                summary["closed_check"] = await check_closed(
                    session, http, settings, robots, ctx.rules.currency_rates, now=now
                )
                state = await get_state(session)
                state["checked_at"] = _iso(now)
                await set_setting(session, STATE_KEY, state)
            except HireHiBlocked as exc:
                blocked = str(exc)
                await block(session, settings, blocked, now)
            await session.commit()

        if settings.batch_auto_create and blocked is None:
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
        by_status = summary.get("by_status") or {}
        if summary.get("processed") or blocked:
            await audit(
                session,
                "scan",
                f"HireHi: обработано {summary.get('processed', 0)}, карточек прочитано {summary.get('cards', 0)}, "
                f"в кандидаты {by_status.get('prescored', 0)}, через hh.ru {summary.get('via_hh', 0)}"
                + (f"; остановлено: {blocked}" if blocked else ""),
                level="error" if blocked else "info",
            )
        await session.commit()
    summary["hh_requests"] = client.requests_made
    if blocked:
        summary["blocked"] = blocked
        summary[FAILED_KEY] = blocked
    runs = summary["searches"]
    if runs and all("error" in r for r in runs) and FAILED_KEY not in summary:
        summary[FAILED_KEY] = f"все поиски HireHi ({len(runs)}) завершились ошибкой: {runs[0]['error']}"
    return summary


# ---- one vacancy by hand: «Переоценить» ------------------------------------------------------------------------


async def reprocess_vacancy(runtime: Runtime, session: AsyncSession, row: Vacancy, *, refetch: bool) -> str:
    """«Переоценить» of a HireHi vacancy: with `refetch` its card is read again (one request). A vacancy with a
    response, in a packet or with a Claude verdict keeps its status, as on hh.ru (scanner.reprocess_vacancy): only the
    stored card is refreshed (a passed term marks it `archived`), a removed card (404) changes nothing — the card
    promises «обновится только описание», and a sent response must not leave «Отправлено». The rest: a removed card
    or a passed term closes the vacancy, then the rules and the pre-score of the stored card (a vacancy of hh.ru —
    the hh.ru route)."""
    from app.services.scanner import build_context, reprocess_keeps_status

    settings, http = runtime.settings, runtime.http
    client = runtime.hh_client()
    keep = await reprocess_keeps_status(session, row)
    if keep and not refetch:
        return row.pipeline_status
    if refetch:
        until = blocked_until(await get_state(session))
        if until is not None:
            raise HireHiBlocked(f"HireHi закрыл доступ, панель ждёт до {_local_time(settings, until)}")
        path = path_of(row)
        if not path:
            raise HireHiError("у вакансии нет адреса карточки")
        await release_writes(session)
        try:
            robots = await fetch_robots(http, settings)
            card = await read_card(http, settings, robots, path)
        except HireHiBlocked as exc:
            await block(session, settings, str(exc))
            raise
        if card is None:
            if keep:
                return row.pipeline_status
            _close(row, GONE_REASON)
            await audit(session, "vacancies", f"Вакансия HireHi удалена с сайта: «{row.name}»", vacancy_id=row.id)
            return row.pipeline_status
        ctx = await build_context(session, client, settings, profile_id=row.selection_profile_id)
        was_archived = row.archived
        _store_card(row, card, ctx.rules.currency_rates)
        if keep:
            return row.pipeline_status
        if row.archived and not was_archived:
            _close(row, EXPIRED_REASON)
            return row.pipeline_status
    if row.archived or keep:
        return row.pipeline_status
    ctx = await build_context(session, client, settings, profile_id=row.selection_profile_id)
    if row.pipeline_status in ("filtered_out", "not_suitable", "prescored", "needs_manual", "new", "hidden"):
        row.pipeline_status = "new"
        row.hidden = False
    return await process_row(session, settings, client, row, ctx)
