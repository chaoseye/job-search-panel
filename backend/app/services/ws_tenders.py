"""Tenders of workspace.ru (page «Тендеры»): organizers publish orders for digital services
there and contractors answer them on the site — the purpose of the platform (its terms of use, п. 3.2). The panel
watches the first page of the tender list and of the chosen sections (robots.txt disallows `?page=`), reads the card
of every new tender, scores it against the questionnaire, sends the suitable ones to the bot (event `ws_tender`) and
keeps an answer written by Claude through the packet exchange (task `tender_reply`); the user posts it on
workspace.ru. Plain HTTP GET with the panel's public User-Agent, robots.txt with `*` rules (services/robots_rules.py),
a pause between requests; the organizer's contacts are never taken — the site opens them after an answer.

The first read of an empty table only fills it (no messages to the bot): the tenders already open are shown, the
following reads report the new ones.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import structlog
from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import WsTender, WsTenderReply, WsTenderReplyRequest
from app.net import HTTP_ERRORS
from app.services.audit import audit
from app.services.jobs import FAILED_KEY
from app.services.preferences import Preferences, get_preferences
from app.services.robots_rules import RobotsRules
from app.services.state import get_setting, set_setting
from app.services.textmatch import contains_term, norm

log = structlog.get_logger("ws_tenders")

JOB = "ws_tenders_sync"
BASE = "https://workspace.ru"
FETCH_TIMEOUT = 60.0  # the TLS handshake to workspace.ru may take half a minute through a VPN
PAUSE_SEC = 1.0
SETTINGS_KEY = "ws_tenders_settings"
# the sections of the tender catalogue (/tenders/<slug>/); robots.txt closes /tenders/mobile/, /tenders/ppc/ and
# /tenders/marketing-smm-pr/ — the open ones are these
SECTIONS: dict[str, str] = {
    "web-development": "Разработка сайтов",
    "crm": "CRM, 1С, ПО, чат-боты",
    "apps-development": "Мобильные приложения",
    "identity": "Дизайн и брендинг",
    "copywriting": "Контент",
    "videoproduction": "Видео и фото",
    "marketing": "Маркетинг",
    "smm": "SMM и PR",
    "seo": "Поисковая оптимизация",
    "context": "Реклама",
}
DEFAULT_SETTINGS: dict[str, Any] = {
    "sections": ["web-development", "crm", "apps-development"],
    # a tender outside the watched sections is suitable from this score (the questionnaire's skills and keywords)
    "min_score": 30,
    # formats, rates and terms for the answers (Claude takes prices and deadlines only from here)
    "rates": "",
}
SECTION_BONUS = 20
# a tender that turns suitable later (its card was read on the next run) still reaches the bot, an old one does not
# (a change of the watched sections must not flood the chat)
NOTIFY_MAX_AGE = timedelta(days=3)
STATUS_LABELS = {
    "new": "новый",
    "interesting": "интересно",
    "drafting": "отклик у Claude",
    "drafted": "отклик готов",
    "responded": "откликнулся",
    "replied": "ответили",
    "declined": "отказ",
    "skipped": "пропущен",
    "hidden": "скрыт",
}
WORK_STATUSES = ("new", "interesting", "drafting", "drafted")
REPLY_STATUSES = ("new", "interesting", "drafted")
# the organizer's answer, recorded by hand once the user has responded
ANSWER_STATUSES = ("replied", "declined")
RESPONDED_STATUSES = ("responded", *ANSWER_STATUSES)
GROUPS = ("relevant", "open", "drafted", "responded", "closed", "all")
MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
_TENDER_HREF = re.compile(r"^/tenders/([a-z0-9-]+)-(\d+)/$")


class TenderError(Exception):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


# ---- parsing -------------------------------------------------------------------------------------------------------


@dataclass
class ListItem:
    ws_id: int
    url: str
    title: str
    budget: str | None = None
    published_on: str | None = None
    deadline_on: str | None = None
    state: str = "open"
    views: int | None = None


@dataclass
class Detail:
    description: str = ""
    budget: str | None = None
    published_on: str | None = None
    deadline_on: str | None = None
    state: str | None = None
    service: str | None = None
    conditions: list[dict[str, str]] = field(default_factory=list)
    participants: int | None = None
    organizer: str | None = None
    city: str | None = None


def _text(node: Tag | None, sep: str = " ") -> str:
    return " ".join(node.get_text(sep, strip=True).split()) if node is not None else ""


def parse_date(raw: str | None) -> str | None:
    """«24 сентября 2026» → «2026-09-24»."""
    m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", (raw or "").lower())
    if not m or m.group(2) not in MONTHS:
        return None
    return f"{int(m.group(3)):04d}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"


def budget_max(raw: str | None) -> int | None:
    """The upper bound in roubles: «до 150 000 рублей» → 150000, «400 000 - 1 200 000» → 1200000, «от 900 000» →
    900000 (the only figure there is)."""
    numbers = [int(n.replace(" ", "").replace("\xa0", "")) for n in re.findall(r"\d[\d \xa0]*\d|\d", raw or "")]
    return max(numbers) if numbers else None


def _info(root: Tag) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in root.select(".b-tender__info-item"):
        title = _text(item.select_one(".b-tender__info-item-title")).rstrip(":").strip()
        if title:
            out[title] = _text(item.select_one(".b-tender__info-item-text"))
    return out


def parse_list(html: str) -> list[ListItem]:
    """The cards of a tender list page (10 newest first). Private tenders are skipped (robots.txt closes them)."""
    soup = BeautifulSoup(html, "lxml")
    items: list[ListItem] = []
    for card in soup.select("[data-tender-card]"):
        link = card.select_one(".b-tender__title a[href^='/tenders/']")
        href = str(link.get("href") or "") if link is not None else ""
        m = _TENDER_HREF.match(href)
        if link is None or m is None or m.group(1).startswith("private-tender"):
            continue
        info = _info(card)
        status = card.select_one("[data-change-status-text]")
        views = _text(card.select_one(".reference._views"))
        budget = card.select_one(".b-tender__block--title > .b-tender__info-item-text")
        items.append(
            ListItem(
                ws_id=int(m.group(2)),
                url=BASE + href,
                title=_text(link)[:500],
                budget=_text(budget)[:100] or None,
                published_on=parse_date(info.get("Опубликован")),
                deadline_on=parse_date(info.get("Крайний срок приема заявок")),
                state="open" if status is None or status.get("data-change-status-text") == "open" else "closed",
                views=int(views) if views.isdigit() else None,
            )
        )
    return items


def parse_detail(html: str) -> Detail:
    soup = BeautifulSoup(html, "lxml")
    detail = Detail()
    desc_node = soup.select_one(".tendercard__desc")
    if desc_node is not None:
        for li in desc_node.find_all("li"):
            li.replace_with(NavigableString(f"\n— {_text(li)}\n"))
        lines = [" ".join(line.split()) for line in desc_node.get_text("\n", strip=True).splitlines()]
        detail.description = "\n".join(line for line in lines if line)[:6000]
    info = _info(soup)
    detail.budget = info.get("Бюджет") or None
    detail.published_on = parse_date(info.get("Опубликован"))
    detail.deadline_on = parse_date(info.get("Крайний срок приема заявок"))
    status = soup.select_one(".tendercart-type__status-public")
    if status is not None:
        finished = "_finished" in status.get_attribute_list("class") or "Завершён" in _text(status)
        detail.state = "closed" if finished else "open"
    for block in soup.select(".tendercard-conditions"):
        title = _text(block.select_one(".tendercard-conditions__title"))
        content = block.select_one(".tendercard-conditions__content")
        if not title or content is None:
            continue
        if title.startswith("В тендере участвуют"):
            detail.participants = len(content.find_all("li"))
            continue
        value = ", ".join(_text(s) for s in content.find_all("span") if _text(s)) or _text(content)
        detail.conditions.append({"title": title[:100], "value": value[:300]})
        if title == "Требуемая услуга":
            detail.service = value[:300]
    for inner in soup.select(".card-info__inner"):
        head = _text(inner.select_one(".card-info__title")).lower().replace("o", "о")  # «Oрганизатор» has a Latin O
        if head.startswith("организатор"):
            detail.organizer = _text(inner.select_one(".card-info__desc"))[:100] or None
    seo = _text(soup.select_one(".tender-aside__seo"))
    m = re.search(r"заказчиком из ([^.?!,]+)", seo)
    if m:
        detail.city = m.group(1).strip()[:100]
    return detail


# ---- settings ------------------------------------------------------------------------------------------------------


async def tender_settings(session: AsyncSession) -> dict[str, Any]:
    saved = await get_setting(session, SETTINGS_KEY)
    out = dict(DEFAULT_SETTINGS)
    if isinstance(saved, dict):
        sections = saved.get("sections")
        if isinstance(sections, list):
            out["sections"] = [s for s in dict.fromkeys(str(x) for x in sections) if s in SECTIONS]
        score = saved.get("min_score")
        if isinstance(score, int) and 0 <= score <= 100:
            out["min_score"] = score
        rates = saved.get("rates")
        if isinstance(rates, str):
            out["rates"] = rates.strip()[:3000]
    return out


async def save_tender_settings(session: AsyncSession, values: dict[str, Any]) -> dict[str, Any]:
    current = await tender_settings(session)
    for key in DEFAULT_SETTINGS:
        if values.get(key) is not None:
            current[key] = values[key]
    if any(s not in SECTIONS for s in current["sections"]):
        raise TenderError(f"разделы: {', '.join(SECTIONS)}", 422)
    await set_setting(session, SETTINGS_KEY, current)
    return await tender_settings(session)


# ---- fit -----------------------------------------------------------------------------------------------------------


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def score_tender(
    row: WsTender, prefs: Preferences, settings: dict[str, Any], today: str | None = None
) -> tuple[int, list[str], bool]:
    """(score, notes, relevant): the questionnaire's skills (4 × weight) and target keywords (8) found in the tender,
    at most 70, plus SECTION_BONUS for a watched section. Suitable: open, no title stop word, and a watched section
    or `min_score`."""
    conditions = " ".join(f"{c.get('title')} {c.get('value')}" for c in _loads(row.conditions_json, []))
    text = norm(" ".join([row.title, row.service or "", row.description or "", conditions]))
    points = 0
    matched: list[str] = []
    for skill in prefs.skills_weights:
        if skill.name.strip() and skill.name not in matched and contains_term(text, skill.name):
            points += 4 * skill.weight
            matched.append(skill.name)
    for word in prefs.target_keywords:
        if word.strip() and word not in matched and contains_term(text, word):
            points += 8
            matched.append(word)
    points = min(points, 70)
    notes = [f"совпадает: {', '.join(matched[:8])}"] if matched else []
    watched = row.category in settings["sections"]
    if watched:
        points += SECTION_BONUS
        notes.append(f"раздел: {SECTIONS.get(row.category or '', row.category)}")
    title = norm(row.title)
    stop = next((w for w in prefs.stop_words_title if w.strip() and contains_term(title, w)), None)
    if stop:
        notes.append(f"стоп-слово в названии: {stop}")
    relevant = is_open(row, today) and stop is None and (watched or points >= int(settings["min_score"]))
    return min(points, 100), notes, relevant


def rescore(row: WsTender, prefs: Preferences, settings: dict[str, Any], today: str | None) -> None:
    row.fit_score, notes, row.relevant = score_tender(row, prefs, settings, today)
    row.fit_notes_json = _json(notes)


async def rescore_all(session: AsyncSession, settings: Settings) -> int:
    """After a change of the watched sections, the threshold or the questionnaire."""
    cfg = await tender_settings(session)
    prefs = await get_preferences(session)
    today = today_iso(settings)
    rows = (await session.execute(select(WsTender))).scalars().all()
    for row in rows:
        rescore(row, prefs, cfg, today)
    await session.flush()
    return len(rows)


def today_iso(settings: Settings, now: datetime | None = None) -> str:
    return (now or utcnow()).astimezone(ZoneInfo(settings.timezone)).date().isoformat()


def is_open(row: WsTender, today: str | None = None) -> bool:
    """The site says «Идет прием заявок» and the deadline has not passed."""
    if row.state != "open":
        return False
    return row.deadline_on is None or today is None or row.deadline_on >= today


# ---- fetching ------------------------------------------------------------------------------------------------------


async def _get(http: httpx.AsyncClient, settings: Settings, path: str) -> str:
    try:
        resp = await http.get(
            BASE + path,
            headers={"User-Agent": settings.public_user_agent, "Accept-Language": "ru,en;q=0.8"},
            timeout=httpx.Timeout(FETCH_TIMEOUT),
            follow_redirects=True,
        )
    except HTTP_ERRORS as exc:
        raise TenderError(f"workspace.ru не ответил: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise TenderError(f"workspace.ru ответил HTTP {resp.status_code} на {path}")
    return resp.text


async def fetch_robots(http: httpx.AsyncClient, settings: Settings) -> RobotsRules:
    """robots.txt of workspace.ru; without it nothing is read (a missing file would allow everything — the panel
    prefers to wait)."""
    text = await _get(http, settings, "/robots.txt")
    return RobotsRules.parse(text, settings.public_user_agent)


# ---- the job -------------------------------------------------------------------------------------------------------


@dataclass
class SyncSummary:
    pages: int = 0
    found: int = 0
    added: int = 0
    details: int = 0
    relevant_new: int = 0
    notified: int = 0
    errors: list[str] = field(default_factory=list)
    backfill: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "pages": self.pages,
            "found": self.found,
            "added": self.added,
            "details": self.details,
            "relevant_new": self.relevant_new,
            "notified": self.notified,
            "errors": self.errors[:10],
            "backfill": self.backfill,
        }


def _apply_detail(row: WsTender, detail: Detail, now: datetime) -> None:
    row.description = detail.description or row.description
    row.budget = detail.budget or row.budget
    row.budget_max = budget_max(row.budget)
    row.published_on = detail.published_on or row.published_on
    row.deadline_on = detail.deadline_on or row.deadline_on
    row.state = detail.state or row.state
    row.service = detail.service or row.service
    row.conditions_json = _json(detail.conditions) if detail.conditions else row.conditions_json
    row.participants = detail.participants if detail.participants is not None else row.participants
    row.organizer = detail.organizer or row.organizer
    row.city = detail.city or row.city
    row.detail_fetched_at, row.detail_error = now, None


async def sync_tenders(runtime: Any) -> dict[str, Any]:
    """Job `ws_tenders_sync`: the lists → new and changed tenders → the cards of the new open ones → score →
    the bot."""
    settings: Settings = runtime.settings
    http: httpx.AsyncClient = runtime.http
    summary = SyncSummary()
    now = utcnow()
    async with runtime.session_factory() as session:
        cfg = await tender_settings(session)
        prefs = await get_preferences(session)
        summary.backfill = not (await session.execute(select(func.count(WsTender.id)))).scalar()
    try:
        robots = await fetch_robots(http, settings)
    except TenderError as exc:
        return {**summary.as_dict(), "errors": [f"robots.txt: {exc}"], FAILED_KEY: f"robots.txt: {exc}"}

    # 1. the first page of the common list and of every watched section (the section wins as the category)
    listed: dict[int, tuple[ListItem, str | None]] = {}
    pages: list[tuple[str | None, str]] = [(None, "/tenders/"), *((s, f"/tenders/{s}/") for s in cfg["sections"])]
    for n, (section, path) in enumerate(pages):
        if not robots.allowed(path):
            summary.errors.append(f"{path}: закрыто в robots.txt")
            continue
        if n:
            await asyncio.sleep(PAUSE_SEC)
        try:
            html = await _get(http, settings, path)
        except TenderError as exc:
            summary.errors.append(str(exc))
            continue
        summary.pages += 1
        for item in parse_list(html):
            if not robots.allowed(item.url.removeprefix(BASE)):
                continue
            known = listed.get(item.ws_id)
            listed[item.ws_id] = (item, section or (known[1] if known else None))
    summary.found = len(listed)

    # 2. new rows and fresh list data
    today = today_iso(settings, now)
    async with runtime.session_factory() as session:
        rows: dict[int, WsTender] = {}
        added: list[WsTender] = []
        if listed:
            found = await session.execute(select(WsTender).where(WsTender.ws_id.in_(list(listed))))
            rows = {r.ws_id: r for r in found.scalars()}
        for ws_id, (item, section) in listed.items():
            row = rows.get(ws_id)
            if row is None:
                row = WsTender(ws_id=ws_id, url=item.url, title=item.title, first_seen_at=now, status="new")
                session.add(row)
                rows[ws_id] = row
                added.append(row)
                summary.added += 1
            row.title, row.url = item.title, item.url
            row.category = row.category or section
            row.budget = item.budget or row.budget
            row.budget_max = budget_max(row.budget)
            row.published_on = item.published_on or row.published_on
            row.deadline_on = item.deadline_on or row.deadline_on
            row.state = item.state
            row.views = item.views if item.views is not None else row.views
            row.last_seen_at = now
        await session.flush()
        new_ids = {r.id for r in added}
        todo = [
            r.id
            for r in rows.values()
            if r.detail_fetched_at is None and is_open(r, today) and r.status not in ("hidden", "skipped")
        ][: max(0, settings.ws_tenders_details_per_run)]
        for r in rows.values():
            rescore(r, prefs, cfg, today)
        await session.commit()

    # 3. the card of every new open tender (the description is what the score and Claude need)
    for tender_id in todo:
        async with runtime.session_factory() as session:
            row = await session.get(WsTender, tender_id)
            path = row.url.removeprefix(BASE) if row is not None else ""
        if row is None or not robots.allowed(path):
            continue
        await asyncio.sleep(PAUSE_SEC)
        try:
            detail: Detail | None = parse_detail(await _get(http, settings, path))
            error = None
        except TenderError as exc:
            detail, error = None, str(exc)
            summary.errors.append(error)
        async with runtime.session_factory() as session:
            row = await session.get(WsTender, tender_id)
            if row is None:
                continue
            if detail is not None:
                _apply_detail(row, detail, now)
                summary.details += 1
            else:
                row.detail_error = error
            rescore(row, prefs, cfg, today)
            await session.commit()

    # 4. the bot: suitable open tenders nobody was told about, seen in the last NOTIFY_MAX_AGE — a tender may become
    # suitable only once its card is read. The first read of an empty table tells nothing: `notified_at` is set anyway
    async with runtime.session_factory() as session:
        fresh = [
            r
            for r in (
                await session.execute(
                    select(WsTender).where(
                        WsTender.notified_at.is_(None),
                        WsTender.relevant.is_(True),
                        WsTender.status.in_(WORK_STATUSES),
                        WsTender.first_seen_at >= now - NOTIFY_MAX_AGE,
                    )
                )
            ).scalars()
            if is_open(r, today)
        ]
        summary.relevant_new = sum(1 for r in fresh if r.id in new_ids)
        from app.services.notify import emit_event

        for row in sorted(fresh, key=lambda r: -(r.fit_score or 0)):
            if not summary.backfill:
                await emit_event(session, settings, "ws_tender", tender_payload(row), now=now)
                summary.notified += 1
            row.notified_at = now
        if summary.added or summary.errors:
            await audit(
                session,
                "tenders",
                f"Тендеры workspace.ru: {'первая загрузка' if summary.backfill else 'новых'} {summary.added}, "
                f"подходящих новых {summary.relevant_new}, карточек прочитано {summary.details}"
                + (f", ошибок {len(summary.errors)}" if summary.errors else ""),
                level="warning" if summary.errors and not summary.pages else "info",
            )
        await session.commit()
    result = summary.as_dict()
    if not summary.pages and summary.errors:
        # workspace.ru did not answer at all: a failed run (three in a row reach the bot as job_error)
        result[FAILED_KEY] = summary.errors[0]
    return result


def tender_payload(row: WsTender) -> dict[str, Any]:
    return {
        "title": row.title,
        "url": row.url,
        "budget": row.budget,
        "deadline": row.deadline_on,
        "city": row.city,
        "score": row.fit_score,
        "section": SECTIONS.get(row.category or "", None),
        "tender_id": row.id,
    }


# ---- views ---------------------------------------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    dt = as_utc(value)
    return dt.isoformat(timespec="seconds") if dt else None


def tender_row(row: WsTender, today: str | None = None) -> dict[str, Any]:
    return {
        "id": row.id,
        "ws_id": row.ws_id,
        "url": row.url,
        "title": row.title,
        "category": row.category,
        "section": SECTIONS.get(row.category or "", None),
        "service": row.service,
        "budget": row.budget,
        "budget_max": row.budget_max,
        "published_on": row.published_on,
        "deadline_on": row.deadline_on,
        "state": row.state,
        "open": is_open(row, today),
        "city": row.city,
        "organizer": row.organizer,
        "participants": row.participants,
        "views": row.views,
        "fit_score": row.fit_score,
        "fit_notes": _loads(row.fit_notes_json, []),
        "relevant": row.relevant,
        "status": row.status,
        "status_label": STATUS_LABELS.get(row.status, row.status),
        "detail_error": row.detail_error,
        "first_seen_at": _iso(row.first_seen_at),
        "responded_at": _iso(row.responded_at),
        "answered_at": _iso(row.answered_at),
        "notes": row.notes,
    }


def reply_row(reply: WsTenderReply) -> dict[str, Any]:
    return {
        "id": reply.id,
        "version": reply.version,
        "text": reply.text,
        "origin": reply.origin,
        "batch_id": reply.batch_id,
        "needs_clarification": _loads(reply.needs_clarification_json, []),
        "warnings": _loads(reply.warnings_json, []),
        "is_current": reply.is_current,
        "created_at": _iso(reply.created_at),
    }


async def tender_detail(session: AsyncSession, settings: Settings, row: WsTender) -> dict[str, Any]:
    replies = (
        (
            await session.execute(
                select(WsTenderReply).where(WsTenderReply.tender_id == row.id).order_by(desc(WsTenderReply.version))
            )
        )
        .scalars()
        .all()
    )
    request = (
        await session.execute(
            select(WsTenderReplyRequest)
            .where(WsTenderReplyRequest.tender_id == row.id)
            .order_by(desc(WsTenderReplyRequest.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    current = next((r for r in replies if r.is_current), None)
    data = tender_row(row, today_iso(settings))
    data.update(
        {
            "description": row.description,
            "conditions": _loads(row.conditions_json, []),
            "reply": reply_row(current) if current else None,
            "replies": [reply_row(r) for r in replies],
            "request": None
            if request is None
            else {"id": request.id, "status": request.status, "batch_id": request.batch_id, "error": request.error},
        }
    )
    return data


def _in_group(row: WsTender, group: str, today: str) -> bool:
    if group == "relevant":
        return row.status in WORK_STATUSES and row.relevant and is_open(row, today)
    if group == "open":
        return row.status in WORK_STATUSES and is_open(row, today)
    if group == "drafted":
        return row.status == "drafted"
    if group == "responded":
        return row.status in RESPONDED_STATUSES  # with the organizer's answer too
    if group == "closed":
        return row.status in ("skipped", "hidden")
    return True


async def list_tenders(
    session: AsyncSession,
    settings: Settings,
    *,
    group: str = "relevant",
    section: str | None = None,
    q: str | None = None,
    sort: str = "fit",
    page: int = 0,
    per_page: int = 50,
) -> dict[str, Any]:
    today = today_iso(settings)
    rows = list((await session.execute(select(WsTender))).scalars().all())
    counts = {g: sum(1 for r in rows if _in_group(r, g, today)) for g in GROUPS}
    rows = [r for r in rows if _in_group(r, group, today)]
    if section:
        rows = [r for r in rows if r.category == section]
    if q and q.strip():
        needle = norm(q)
        rows = [r for r in rows if needle in norm(" ".join([r.title, r.description or "", r.service or ""]))]
    if sort == "published":
        rows.sort(key=lambda r: (r.published_on or "", r.ws_id), reverse=True)
    elif sort == "deadline":
        rows.sort(key=lambda r: (r.deadline_on or "9999-12-31", r.ws_id))
    elif sort == "budget":
        rows.sort(key=lambda r: (r.budget_max or 0, r.ws_id), reverse=True)
    else:
        rows.sort(key=lambda r: (r.fit_score or 0, r.published_on or "", r.ws_id), reverse=True)
    per_page = max(1, min(per_page, 200))
    return {
        "items": [tender_row(r, today) for r in rows[page * per_page : (page + 1) * per_page]],
        "total": len(rows),
        "page": page,
        "per_page": per_page,
        "counts": counts,
    }


async def new_relevant_count(session: AsyncSession, settings: Settings) -> int:
    """Menu badge: suitable open tenders the user has not looked at (status «new»)."""
    today = today_iso(settings)
    rows = (
        (await session.execute(select(WsTender).where(WsTender.status == "new", WsTender.relevant.is_(True))))
        .scalars()
        .all()
    )
    return sum(1 for r in rows if is_open(r, today))


# ---- actions -------------------------------------------------------------------------------------------------------


async def set_status(session: AsyncSession, row: WsTender, action: str, now: datetime | None = None) -> None:
    """interesting | skip | hide | restore | responded | replied | declined."""
    if action == "interesting":
        if row.status in ("new", "skipped", "hidden"):
            row.status = "interesting"
    elif action in ("skip", "hide"):
        row.status = "skipped" if action == "skip" else "hidden"
    elif action == "restore":
        # a tender already responded to comes back to «Откликнулся»: its answer is not posted twice (a recorded answer
        # of the organizer is marked again there, answered_at keeps the first record)
        if row.responded_at is not None:
            row.status = "responded"
        else:
            row.status = "drafted" if await current_reply(session, row.id) is not None else "interesting"
    elif action == "responded":
        # a recorded answer stays: «Я откликнулся» again does not take it back
        if row.status not in ANSWER_STATUSES:
            row.status = "responded"
        row.responded_at = row.responded_at or now or utcnow()
    elif action in ANSWER_STATUSES:
        if row.status not in RESPONDED_STATUSES:
            raise TenderError("Ответ заказчика отмечают у тендера, на который вы уже откликнулись")
        row.status, row.answered_at = action, row.answered_at or now or utcnow()
    else:
        raise TenderError(f"неизвестное действие: {action}", 422)
    await audit(session, "tenders", f"Тендер «{row.title[:120]}»: {STATUS_LABELS.get(row.status, row.status)}")


async def current_reply(session: AsyncSession, tender_id: int) -> WsTenderReply | None:
    return (
        await session.execute(
            select(WsTenderReply)
            .where(WsTenderReply.tender_id == tender_id, WsTenderReply.is_current.is_(True))
            .order_by(desc(WsTenderReply.version))
            .limit(1)
        )
    ).scalar_one_or_none()


async def _add_reply(session: AsyncSession, tender_id: int, text: str, origin: str, **extra: Any) -> WsTenderReply:
    version = int(
        (
            await session.execute(select(func.max(WsTenderReply.version)).where(WsTenderReply.tender_id == tender_id))
        ).scalar()
        or 0
    )
    for old in (
        await session.execute(
            select(WsTenderReply).where(WsTenderReply.tender_id == tender_id, WsTenderReply.is_current.is_(True))
        )
    ).scalars():
        old.is_current = False
    reply = WsTenderReply(tender_id=tender_id, version=version + 1, text=text, origin=origin, is_current=True, **extra)
    session.add(reply)
    await session.flush()
    return reply


async def save_reply(session: AsyncSession, row: WsTender, text: str) -> WsTenderReply:
    """The user's edit becomes a new current version."""
    text = text.replace("\r\n", "\n").strip()
    if not text:
        raise TenderError("нужен текст отклика", 422)
    reply = await _add_reply(session, row.id, text, "edited")
    if row.status in ("new", "interesting"):
        row.status = "drafted"
    return reply


async def update_notes(session: AsyncSession, row: WsTender, notes: str | None) -> None:
    row.notes = (notes or "").strip()[:2000] or None
    await session.flush()


# ---- answers through Claude ----------------------------------------------------------------------------------------


async def request_replies(
    session: AsyncSession, settings: Settings, ids: list[int], notes: str | None = None
) -> tuple[int, list[str], str | None]:
    """Queues a `tender_reply` task per tender and tries to build a packet right away.
    Returns (queued, skipped reasons, first packet id or None)."""
    from app.exchange.packets import PacketError, build_batches

    queued = 0
    skipped: list[str] = []
    today = today_iso(settings)
    for tender_id in dict.fromkeys(ids):
        row = await session.get(WsTender, tender_id)
        if row is None:
            skipped.append(f"#{tender_id}: не найден")
            continue
        if row.status not in REPLY_STATUSES:
            skipped.append(f"«{row.title[:80]}»: {STATUS_LABELS.get(row.status, row.status)}")
            continue
        if not is_open(row, today):
            skipped.append(f"«{row.title[:80]}»: приём заявок закрыт")
            continue
        for old in (
            await session.execute(
                select(WsTenderReplyRequest).where(
                    WsTenderReplyRequest.tender_id == row.id, WsTenderReplyRequest.status == "queued"
                )
            )
        ).scalars():
            old.status, old.error = "cancelled", "заменён новым запросом"
        session.add(WsTenderReplyRequest(tender_id=row.id, user_notes=(notes or "").strip() or None))
        row.status = "drafting"
        queued += 1
    await session.flush()
    batch_id: str | None = None
    if queued:
        await audit(session, "tenders", f"Запрошены отклики на тендеры через Claude: {queued}")
        try:
            created = await build_batches(session, settings, vacancy_ids=[], include_revisions=True)
            batch_id = created[0].batch_id if created else None
        except PacketError as exc:
            skipped.append(str(exc))
    return queued, skipped, batch_id


def packet_tender(row: WsTender, settings: dict[str, Any]) -> dict[str, Any]:
    """What Claude gets about the tender, and the user's formats, rates and terms for the answer."""
    return {
        "title": row.title,
        "url": row.url,
        "section": SECTIONS.get(row.category or "", None),
        "service": row.service,
        "conditions": _loads(row.conditions_json, []),
        "budget": row.budget,
        "published_on": row.published_on,
        "deadline_on": row.deadline_on,
        "city": row.city,
        "organizer": row.organizer,
        "participants": row.participants,
        "description": (row.description or "")[:6000],
        "rates": settings.get("rates") or "",
    }


async def finish_reply(
    session: AsyncSession,
    request: WsTenderReplyRequest,
    *,
    text: str,
    batch_id: str,
    needs_clarification: list[str],
    warnings: list[str],
) -> WsTenderReply:
    """Importer: Claude's answer becomes the tender's current version."""
    reply = await _add_reply(
        session,
        request.tender_id,
        text,
        "claude",
        batch_id=batch_id,
        needs_clarification_json=_json(needs_clarification),
        warnings_json=_json(warnings),
    )
    request.status, request.error = "done", None
    row = await session.get(WsTender, request.tender_id)
    if row is not None and row.status == "drafting":
        row.status = "drafted"
    await session.flush()
    return reply


async def fail_reply(session: AsyncSession, request: WsTenderReplyRequest, error: str) -> None:
    """Importer: no answer — the request is closed with the reason, the tender goes back to where it was."""
    request.status, request.error = "cancelled", error[:500]
    row = await session.get(WsTender, request.tender_id)
    if row is not None and row.status == "drafting":
        row.status = "drafted" if await current_reply(session, row.id) is not None else "interesting"
    await session.flush()
