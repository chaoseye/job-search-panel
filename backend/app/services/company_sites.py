"""Career sites of chosen companies → one feed in the panel and links to the bot.

Every site has an adapter (ADAPTERS) that reads the public vacancy list the way the site publishes it — a JSON API the
site's own page uses (Sber, X5) or server-rendered HTML (Avito, Merlion, ВкусВилл) — with the panel's public
User-Agent (no e-mail), no browser and no bypass of bot protection (job.goldapple.ru, career.ozon.ru and job.wb.ru sit
behind JS challenges and are therefore covered through hh.ru searches by company name instead). A vacancy is a
«match» when it is in one of SITES_CITIES (office or hybrid) or remote, its title carries none of the questionnaire's
title stop words, and — when the site has keywords — mentions one of them. The first read of a site is a backfill
(shown, not «new», nothing to the bot); later reads mark new matches «new» and send them as the event `site_vacancy`.
Vacancies that disappear from the site become `gone`.

Mass hiring (shops, delivery, warehouses of ВкусВилл, Пятёрочка, Перекрёсток — thousands of identical positions by
address) is read as one row per position and city with the number of addresses, and as its own site entry so it can
be switched off separately from the office vacancies.

Adding a site = one adapter function returning SiteVacancyItem rows + an entry in ADAPTERS (see README).
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
import structlog
from bs4 import BeautifulSoup
from sqlalchemy import desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import CompanySite, SiteVacancy
from app.net import HTTP_ERRORS
from app.services.audit import audit
from app.services.notify import emit_event
from app.services.textmatch import contains_term, norm, norm_plain, strip_emoji

log = structlog.get_logger("company_sites")

JOB = "sites_sync"
FETCH_TIMEOUT = 30.0
PAUSE_BETWEEN_SITES = 0.5
SNIPPET_CHARS = 300
LIST_MAX = 5000
REMOTE_WORDS = ("удалён", "удален", "remote", "дистанцион")


@dataclass
class SiteVacancyItem:
    external_id: str
    title: str
    url: str
    location: str | None = None
    work_format: str | None = None
    remote: bool = False
    snippet: str | None = None
    published_at: datetime | None = None
    company: str | None = None


Fetcher = Callable[[httpx.AsyncClient, Settings], Awaitable[list[SiteVacancyItem]]]


@dataclass
class SiteAdapter:
    key: str
    name: str
    url: str
    fetch: Fetcher
    note: str = ""


@dataclass
class SyncResult:
    items: int = 0
    added: int = 0
    new: int = 0
    matches: int = 0
    notified: int = 0
    gone: int = 0
    backfill: bool = False
    error: str | None = None
    errors: list[str] = field(default_factory=list)


class SiteError(Exception):
    pass


def _headers(settings: Settings) -> dict[str, str]:
    return {"User-Agent": settings.public_user_agent, "Accept-Language": "ru,en;q=0.8"}


# what the sites' own scripts send with their AJAX requests (Bitrix answers with the list part only)
_XHR = {"X-Requested-With": "XMLHttpRequest"}


async def _request(
    http: httpx.AsyncClient,
    settings: Settings,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    try:
        resp = await http.request(
            method,
            url,
            headers={**_headers(settings), **(headers or {})},
            timeout=httpx.Timeout(FETCH_TIMEOUT),
            follow_redirects=True,
            **kwargs,
        )
    except HTTP_ERRORS as exc:
        raise SiteError(f"сеть: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        low = resp.text[:20000].lower()
        if "challenge" in low or "antibot" in low or "captcha" in low:
            raise SiteError("сайт закрыт защитой от роботов (JS-challenge) — панель его не обходит")
        raise SiteError(f"HTTP {resp.status_code}")
    return resp


async def _get(http: httpx.AsyncClient, settings: Settings, url: str, **kwargs: Any) -> httpx.Response:
    return await _request(http, settings, "GET", url, **kwargs)


async def _post(http: httpx.AsyncClient, settings: Settings, url: str, **kwargs: Any) -> httpx.Response:
    return await _request(http, settings, "POST", url, **kwargs)


def _json(resp: httpx.Response, what: str = "ответ API") -> Any:
    try:
        return resp.json()
    except ValueError as exc:
        raise SiteError(f"{what} не разобран") from exc


def _cookie_header(resp: httpx.Response) -> dict[str, str]:
    """The session cookie of a page, sent back explicitly with the page's own AJAX call (the shared client's jar is
    not relied upon)."""
    jar = "; ".join(f"{k}={v}" for k, v in resp.cookies.items())
    return {"Cookie": jar} if jar else {}


def _parse_iso(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return as_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def _has_remote_word(*texts: str | None) -> bool:
    return any(w in (t or "").lower() for t in texts for w in REMOTE_WORDS)


_PAREN_RE = re.compile(r"\s*\([^)]*\)")


def mass_title(title: str) -> str:
    """«Бариста (м. Горьковская)» → «Бариста»: mass hiring is one row per position, not per address."""
    out = " ".join(_PAREN_RE.sub("", title).split()).strip(" ,;-–—")
    return out or title.strip()


def _mass_id(city: str, title: str) -> str:
    # norm_plain, not norm: the ids of rows already stored hash this form — a new form would re-announce them
    return f"{slugify(city)[:24]}:{hashlib.sha1(norm_plain(title).encode('utf-8')).hexdigest()[:12]}"


def _money(value: Any) -> int:
    try:
        return int(float(value)) if value not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


def _mass_snippet(addresses: int, salaries: list[str]) -> str:
    text = f"массовый найм: адресов {addresses}"
    if salaries:
        text += " · " + " / ".join(salaries[:3])
    return text


# ---- adapters --------------------------------------------------------------------------------


async def fetch_avito(http: httpx.AsyncClient, settings: Settings) -> list[SiteVacancyItem]:
    """career.avito.com: the whole list is server-rendered with data-vacancy-* attributes."""
    resp = await _get(http, settings, "https://career.avito.com/vacancies/")
    soup = BeautifulSoup(resp.text, "lxml")
    items: list[SiteVacancyItem] = []
    for div in soup.select("div.vacancies-section__item[data-vacancy-id]"):
        link = div.select_one("a.vacancies-section__item-name")
        if link is None:
            continue
        href = str(link.get("href") or "")
        geo = str(div.get("data-vacancy-geo") or "").strip()
        remote = str(div.get("data-vacancy-remote") or "").strip().lower() in ("да", "yes", "true", "1")
        section = str(div.get("data-vacancy-section") or "").strip()
        team = str(div.get("data-vacancy-team") or "").strip()
        items.append(
            SiteVacancyItem(
                external_id=str(div.get("data-vacancy-id")),
                title=link.get_text(" ", strip=True),
                url=href if href.startswith("http") else f"https://career.avito.com{href}",
                location=geo or None,
                work_format="удалённо" if remote else None,
                remote=remote,
                snippet=" · ".join(x for x in (section, team) if x) or None,
                company="Авито",
            )
        )
    if not items:
        raise SiteError("список вакансий не найден — разметка сайта изменилась?")
    return items


SBER_API = "https://rabota.sber.ru/public/app-candidate-public-api-gateway/api/v1"
SBER_SCHEDULES = {1: "полный день", 2: "сменный график", 3: "гибкий график", 4: "удалённо", 5: "вахта"}
SBER_REMOTE_SCHEDULE = 4
SBER_PAGE = 100
_SBER_TRANSLIT = {
    **{c: c for c in "abcdefghijklmnopqrstuvwxyz"},
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "c",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ь": "",
    "ы": "y",
    "ъ": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def slugify(title: str) -> str:
    """Latin slug of a Russian title — the transliteration rabota.sber.ru uses for /search/<slug>-<id>/ (the server
    redirects a wrong slug anyway); also the stable id of grouped mass-hiring rows."""
    out = "".join(_SBER_TRANSLIT.get(ch, " ") for ch in title.lower())
    return re.sub(r"-+", "-", out.strip().replace(" ", "-"))


async def _sber_query(http: httpx.AsyncClient, settings: Settings, query: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    skip = 0
    while True:
        resp = await _get(http, settings, f"{SBER_API}/publications?{query}&skip={skip}&take={SBER_PAGE}")
        payload = _json(resp)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise SiteError("ответ API не разобран")
        page = data.get("vacancies") or []
        out.extend(v for v in page if isinstance(v, dict))
        total = int(data.get("total") or 0)
        skip += SBER_PAGE
        if not page or skip >= total or skip >= 5000:
            break
    return out


async def fetch_sber(http: httpx.AsyncClient, settings: Settings) -> list[SiteVacancyItem]:
    """rabota.sber.ru (sberbank-talents.ru redirects there): the public API of the search page — the cities from
    SITES_CITIES plus every remote vacancy."""
    queries: list[str] = [f"schedules={SBER_REMOTE_SCHEDULE}"]
    for city in cities(settings):
        resp = await _get(http, settings, f"{SBER_API}/locations", params={"search": city})
        locations = _json(resp, "справочник городов").get("data") or []
        wanted = norm(city)
        for loc in locations:
            name = norm(str(loc.get("name") or ""))
            if name in (wanted, f"г {wanted}") and loc.get("id"):
                queries.append(f"locations={loc['id']}")
                break
    seen: dict[str, SiteVacancyItem] = {}
    for query in queries:
        for v in await _sber_query(http, settings, query):
            ext = str(v.get("internalId") or v.get("publicationId") or "")
            if not ext or ext in seen:
                continue
            title = str(v.get("title") or "").strip()
            schedule = v.get("workScheduleId")
            try:
                schedule_id = int(schedule) if schedule is not None else None
            except (TypeError, ValueError):
                schedule_id = None
            published = _parse_iso(v.get("publicationDate"))
            city_name = str(v.get("city") or "").strip()
            region = str(v.get("region") or "").strip()
            location = ", ".join(x for x in (city_name, region) if x) or None
            seen[ext] = SiteVacancyItem(
                external_id=ext,
                title=title,
                url=f"https://rabota.sber.ru/search/{slugify(title)}-{ext}/",
                location=location,
                work_format=SBER_SCHEDULES.get(schedule_id or 0),
                remote=schedule_id == SBER_REMOTE_SCHEDULE,
                snippet=" ".join(str(v.get("introduction") or "").split())[:SNIPPET_CHARS] or None,
                published_at=published,
                company=str(v.get("company") or "Сбер"),
            )
    return list(seen.values())


async def fetch_merlion(http: httpx.AsyncClient, settings: Settings) -> list[SiteVacancyItem]:
    """job.merlion.com/vacancy/: blocks per region with plain links; regions are named in the filter above them."""
    resp = await _get(http, settings, "https://job.merlion.com/vacancy/")
    soup = BeautifulSoup(resp.text, "lxml")
    regions: dict[str, str] = {}
    for a in soup.select("a[data-region]"):
        rid = str(a.get("data-region") or "")
        name = a.get_text(" ", strip=True).replace("‑", "-").replace("\xa0", " ")
        if rid and name and rid not in regions:
            regions[rid] = name
    items: list[SiteVacancyItem] = []
    for block in soup.select("div.ttinfo[data-region]"):
        region = regions.get(str(block.get("data-region") or ""), "")
        for a in block.select("a[href^='/vacancy/']"):
            href = str(a.get("href") or "")
            m = re.search(r"/vacancy/(\d+)", href)
            if not m:
                continue
            title = a.get_text(" ", strip=True)
            remote = any(w in title.lower() for w in REMOTE_WORDS)
            items.append(
                SiteVacancyItem(
                    external_id=m.group(1),
                    title=title,
                    url=f"https://job.merlion.com/vacancy/{m.group(1)}",
                    location=region or None,
                    work_format="удалённо" if remote else None,
                    remote=remote,
                    company="Merlion",
                )
            )
    if not items:
        raise SiteError("список вакансий не найден — разметка сайта изменилась?")
    return items


VKUSVILL = "https://vkusvill.ru"
VKUSVILL_OFFICE_PAGE = 100  # the endpoint's ceiling; larger values fall back to 10
_VKUSVILL_ID_RE = re.compile(r"_(\d+)\.html?$")


async def fetch_vkusvill_office(http: httpx.AsyncClient, settings: Settings) -> list[SiteVacancyItem]:
    """vkusvill.ru/job/office/: the office list the page itself loads through /ajax/job/hh_list_filter.php (Bitrix
    `sessid` from the page plus its session cookie); every city — the rule decides, «удалённо» in the title = remote."""
    page = await _get(http, settings, f"{VKUSVILL}/job/office/")
    m = re.search(r'name="sessid"[^>]*value="([^"]+)"', page.text)
    if not m:
        raise SiteError("sessid не найден — разметка сайта изменилась?")
    headers = {**_XHR, **_cookie_header(page)}
    items: list[SiteVacancyItem] = []
    for page_no in range(1, 11):
        resp = await _post(
            http,
            settings,
            f"{VKUSVILL}/ajax/job/hh_list_filter.php",
            data={
                "nextPage": page_no,
                "pageSize": VKUSVILL_OFFICE_PAGE,
                "searchText": "",
                "teamId": "",
                "cityId": "",
                "action": "filterVacancies",
                "sessid": m.group(1),
            },
            headers=headers,
        )
        payload = _json(resp, "список вакансий")
        if not isinstance(payload, dict) or payload.get("success") != "Y":
            raise SiteError("сайт не отдал список офисных вакансий (sessid отклонён?)")
        data = payload.get("data")
        if not isinstance(data, dict):
            data = {}
        soup = BeautifulSoup(str(data.get("vacancies") or ""), "lxml")
        links = soup.select("a.js-job-office-vacancy")
        for a in links:
            href = str(a.get("href") or "")
            name = a.select_one(".VV24_VakansiiOfis_Role__Item_Name")
            title = (name or a).get_text(" ", strip=True)
            team = a.select_one("._team")
            city = a.select_one("._city")
            city_name = city.get_text(" ", strip=True).strip("• ").strip() if city else ""
            ext = _VKUSVILL_ID_RE.search(href)
            remote = _has_remote_word(title)
            items.append(
                SiteVacancyItem(
                    external_id=ext.group(1) if ext else href[-64:],
                    title=title,
                    url=href if href.startswith("http") else f"{VKUSVILL}{href}",
                    location=city_name or None,
                    work_format="удалённо" if remote else None,
                    remote=remote,
                    snippet=team.get_text(" ", strip=True) if team else None,
                    company="ВкусВилл",
                )
            )
        # the pager of the answer names the next pages; without it the list is complete
        if not links or not any(int(n) > page_no for n in re.findall(r'data-page="(\d+)"', str(data.get("nav") or ""))):
            break
    if not items:
        raise SiteError("список вакансий не найден — разметка сайта изменилась?")
    return items


async def fetch_vkusvill_retail(http: httpx.AsyncClient, settings: Settings) -> list[SiteVacancyItem]:
    """vkusvill.ru/job/vacancys/ — shops, delivery, dark stores, warehouses: thousands of positions by address, 5 per
    page. The page's smart filter names the checkbox of every city (SITES_CITIES), the map view of the filtered list
    returns all its vacancies as one JSON (title, address, salary, link) — positions without a map point are not
    seen. One row per position and city with the number of addresses."""
    page = await _get(http, settings, f"{VKUSVILL}/job/vacancys/")
    soup = BeautifulSoup(page.text, "lxml")
    boxes: dict[str, str] = {}
    for inp in soup.select('input[name^="arrFilter_vacancies_"][data-title]'):
        boxes.setdefault(norm(str(inp.get("data-title") or "")), str(inp.get("name") or ""))
    if not boxes:
        raise SiteError("фильтр городов не найден — разметка сайта изменилась?")
    items: list[SiteVacancyItem] = []
    for city in cities(settings):
        box = boxes.get(norm(city))
        if not box:
            log.info("site_city_missing", site="vkusvill_retail", city=city)
            continue
        params = {box: "Y", "set_filter": "Y", "tab": "map"}
        resp = await _get(http, settings, f"{VKUSVILL}/job/vacancys/", params=params, headers=_XHR)
        payload = _json(resp, "карта вакансий")
        points = payload.get("points") if isinstance(payload, dict) else None
        if isinstance(points, str):
            try:
                points = json.loads(points)
            except ValueError as exc:
                raise SiteError("карта вакансий не разобрана") from exc
        groups: dict[str, list[dict[str, Any]]] = {}
        for point in points if isinstance(points, list) else []:
            props = point.get("properties") if isinstance(point, dict) else None
            title = strip_emoji(html.unescape(str((props or {}).get("title") or ""))).strip()
            if props and title:
                groups.setdefault(mass_title(title), []).append(props)
        for title, rows in groups.items():
            salaries = sorted({" ".join(html.unescape(str(r.get("salary") or "")).split()) for r in rows} - {""})
            href = str(rows[0].get("url") or "")
            items.append(
                SiteVacancyItem(
                    external_id=_mass_id(city, title),
                    title=title,
                    url=href if href.startswith("http") else f"{VKUSVILL}{href}",
                    location=city,
                    remote=False,
                    snippet=_mass_snippet(len(rows), salaries),
                    company="ВкусВилл",
                )
            )
    return items


X5_API = "https://rabota5ka.ru/gw/api/vacancy"
X5_PAGE = 100
X5_MAX_PAGES = 80
# what the site's own page sends: the API then lists the chosen city first, other cities after it
X5_GEO = {"radius": 30000, "warehouseRadius": 100}
X5_PEREKRESTOK_PORTAL = "cross"


async def _x5_json(http: httpx.AsyncClient, settings: Settings, path: str, params: dict[str, Any]) -> dict[str, Any]:
    payload = _json(await _get(http, settings, f"{X5_API}/{path}", params=params))
    if not isinstance(payload, dict):
        raise SiteError("ответ API не разобран")
    return payload


async def fetch_x5_office(http: httpx.AsyncClient, settings: Settings) -> list[SiteVacancyItem]:
    """rabota5ka.ru/office-vacancies: the office vacancies of X5 (Пятёрочка, Чижик, transport) through the page's own
    API, 10 per page; every city — the rule decides, `workFormat` («Гибрид», «Офис», «Удалённо») tells remote."""
    items: list[SiteVacancyItem] = []
    page = 1
    while page <= X5_MAX_PAGES:
        data = await _x5_json(http, settings, "office-vacancy", {"page": page})
        rows = [r for r in (data.get("items") or []) if isinstance(r, dict)]
        for v in rows:
            title = strip_emoji(str(v.get("name") or "")).strip()
            ext = str(v.get("id") or v.get("externalId") or "")
            if not ext or not title:
                continue
            city = str((v.get("city") or {}).get("name") or "").strip()
            fmt = str(v.get("workFormat") or "").strip()
            schedule = str(v.get("schedule") or "").strip()
            items.append(
                SiteVacancyItem(
                    external_id=ext,
                    title=title,
                    url=f"https://rabota5ka.ru/office-vacancies?search={quote(title)}",
                    location=city or None,
                    work_format=" · ".join(x for x in (fmt, schedule) if x) or None,
                    remote=_has_remote_word(fmt, title),
                    snippet=" ".join(str(v.get("mainResponsobilities") or "").split())[:SNIPPET_CHARS] or None,
                    published_at=_parse_iso(v.get("createdAt")),
                    company=str(v.get("businessUnit") or "X5"),
                )
            )
        if not rows or page >= int(data.get("totalPages") or 0):
            break
        page += 1
    return items


async def _x5_mass(
    http: httpx.AsyncClient,
    settings: Settings,
    *,
    portal: str | None,
    company: str,
    vacancy_url: Callable[[str], str],
) -> list[SiteVacancyItem]:
    """Shop, café, warehouse and courier positions of an X5 portal (rabota5ka.ru, rabota.perekrestok.ru): the API sorts
    the chosen city first, so the pages are read until the first position of another city. One row per position type
    and city with the number of addresses and the salary range."""
    portal_params: dict[str, Any] = {"portals": portal} if portal else {}
    data = await _x5_json(http, settings, "cities", portal_params)
    city_ids = {
        norm(str(c.get("name") or "")): str(c.get("id"))
        for c in (data.get("cities") or [])
        if isinstance(c, dict) and c.get("id")
    }
    items: list[SiteVacancyItem] = []
    for city in cities(settings):
        city_id = city_ids.get(norm(city))
        if not city_id:
            log.info("site_city_missing", site=f"x5:{portal or 'default'}", city=city)
            continue
        groups: dict[str, list[dict[str, Any]]] = {}
        page = 1
        while page <= X5_MAX_PAGES:
            params = {"cityId": city_id, "limit": X5_PAGE, "page": page, **X5_GEO, **portal_params}
            data = await _x5_json(http, settings, "hire-request", params)
            rows = [r for r in (data.get("items") or []) if isinstance(r, dict)]
            own = [r for r in rows if str((r.get("city") or {}).get("id") or "") == city_id]
            for r in own:
                vac = r.get("vacancy") or {}
                groups.setdefault(str(vac.get("id") or vac.get("name") or r.get("name") or ""), []).append(r)
            if not rows or len(own) < len(rows) or len(rows) < X5_PAGE:
                break
            page += 1
        for key, rows in groups.items():
            first = rows[0]
            title = strip_emoji(str((first.get("vacancy") or {}).get("name") or first.get("name") or "")).strip()
            if not key or not title:
                continue
            lows = [x for x in (_money(r.get("salaryFrom")) for r in rows) if x > 0]
            highs = [x for x in (_money(r.get("salaryTo")) for r in rows) if x > 0]
            salary = ""
            if lows and highs:
                salary = f"{min(lows):,} – {max(highs):,} ₽".replace(",", " ")
            elif lows:
                salary = f"от {min(lows):,} ₽".replace(",", " ")
            items.append(
                SiteVacancyItem(
                    external_id=f"{city_id}:{key}"[:64],
                    title=title,
                    url=vacancy_url(str(first.get("id") or "")),
                    location=city,
                    remote=False,
                    snippet=_mass_snippet(len(rows), [salary] if salary else []),
                    company=company,
                )
            )
    return items


async def fetch_pyaterochka_retail(http: httpx.AsyncClient, settings: Settings) -> list[SiteVacancyItem]:
    return await _x5_mass(
        http, settings, portal=None, company="Пятёрочка", vacancy_url=lambda i: f"https://rabota5ka.ru/vacancy/{i}"
    )


async def fetch_perekrestok(http: httpx.AsyncClient, settings: Settings) -> list[SiteVacancyItem]:
    return await _x5_mass(
        http,
        settings,
        portal=X5_PEREKRESTOK_PORTAL,
        company="Перекрёсток",
        vacancy_url=lambda i: f"https://rabota.perekrestok.ru/vacancies/{i}",
    )


ADAPTERS: dict[str, SiteAdapter] = {
    "avito": SiteAdapter("avito", "Авито", "https://career.avito.com/vacancies/", fetch_avito),
    "sber": SiteAdapter(
        "sber",
        "Сбер",
        "https://rabota.sber.ru/search/",
        fetch_sber,
        note="sberbank-talents.ru — тот же сайт (перенаправляет на rabota.sber.ru)",
    ),
    "merlion": SiteAdapter("merlion", "Merlion", "https://job.merlion.com/vacancy/", fetch_merlion),
    "vkusvill": SiteAdapter(
        "vkusvill",
        "ВкусВилл — офис",
        f"{VKUSVILL}/job/office/",
        fetch_vkusvill_office,
        note="офисные вакансии по всем городам; магазины, доставка и склады — отдельной строкой",
    ),
    "vkusvill_retail": SiteAdapter(
        "vkusvill_retail",
        "ВкусВилл — магазины, доставка, склады",
        f"{VKUSVILL}/job/vacancys/",
        fetch_vkusvill_retail,
        note="массовый найм по адресам: одна строка на позицию в городе из SITES_CITIES, в описании — число адресов "
        "(позиции без точки на карте сайта не видны)",
    ),
    "pyaterochka": SiteAdapter(
        "pyaterochka",
        "Пятёрочка (X5) — офис",
        "https://rabota5ka.ru/office-vacancies",
        fetch_x5_office,
        note="офисные вакансии X5 (Пятёрочка, Чижик, транспорт) по всем городам; ссылка открывает список с поиском "
        "по названию",
    ),
    "pyaterochka_retail": SiteAdapter(
        "pyaterochka_retail",
        "Пятёрочка — магазины, склады, курьеры",
        "https://rabota5ka.ru/vacancies",
        fetch_pyaterochka_retail,
        note="массовый найм по адресам: одна строка на позицию в городе из SITES_CITIES, в описании — число адресов и "
        "вилка зарплат",
    ),
    "perekrestok": SiteAdapter(
        "perekrestok",
        "Перекрёсток — магазины, кафе, склады",
        "https://rabota.perekrestok.ru/vacancies",
        fetch_perekrestok,
        note="на сайте только массовый найм по адресам (офисных вакансий нет; тот же API X5, что у Пятёрочки): одна "
        "строка на позицию в городе из SITES_CITIES",
    ),
}
# sites the panel cannot read and why (shown on the page)
UNSUPPORTED: dict[str, str] = {
    "job.goldapple.ru": "закрыт JS-защитой от роботов; вакансии Золотого Яблока можно получать поиском hh.ru "
    "по названию компании",
    "career.ozon.ru": "закрыт защитой от роботов (Antibot Challenge); вакансии Ozon можно получать поиском hh.ru "
    "по названию компании",
    "job.wb.ru": "закрыт защитой от роботов (WB antibot); вакансии Wildberries можно получать поиском hh.ru "
    "по названию компании",
}


# ---- matching --------------------------------------------------------------------------------


def cities(settings: Settings) -> list[str]:
    return [c.strip() for c in (settings.sites_cities or "").split(",") if c.strip()]


def parse_keywords(raw: str | None) -> list[str]:
    return [k.strip() for k in re.split(r"[,;\n]+", raw or "") if k.strip()]


CITY_ALIASES = {"санкт-петербург": ("спб", "санкт петербург", "петербург")}


def _city_in(loc: str, city: str) -> bool:
    """A whole word of the location, allowing a case ending («в Санкт-Петербурге»): «Орёл» must not match «Орелстрой»,
    «Мир» — «Мирный», «спб» — «СПбГУ» (abbreviations take no ending)."""
    if not city:
        return False
    ending = "[а-я]{0,2}" if len(city) >= 5 else ""
    return re.search(rf"(?<!\w){re.escape(city)}{ending}(?!\w)", loc) is not None


def location_matches(location: str | None, remote: bool, city_list: list[str]) -> bool:
    if remote:
        return True
    loc = norm(location)
    if not loc:
        return False
    for city in city_list:
        c = norm(city)
        if any(_city_in(loc, name) for name in (c, *CITY_ALIASES.get(c, ()))):
            return True
    return False


def is_match(item: SiteVacancyItem, *, city_list: list[str], stop_words: list[str], keywords: list[str]) -> bool:
    if not location_matches(item.location, item.remote, city_list):
        return False
    title = norm(item.title)
    if any(contains_term(title, w) for w in stop_words):
        return False
    if keywords and not any(contains_term(title, k) for k in keywords):
        return False
    return True


# ---- sync ------------------------------------------------------------------------------------


async def ensure_sites(session: AsyncSession) -> list[CompanySite]:
    rows = {r.key: r for r in (await session.execute(select(CompanySite))).scalars().all()}
    created = False
    for key in ADAPTERS:
        if key not in rows:
            row = CompanySite(key=key, enabled=True, notify=True)
            session.add(row)
            rows[key] = row
            created = True
    if created:
        await session.flush()
    return [rows[key] for key in ADAPTERS if key in rows]


async def _stop_words(session: AsyncSession) -> list[str]:
    from app.services.preferences import get_preferences

    prefs = await get_preferences(session)
    return list(prefs.stop_words_title or [])


async def sync_site(
    session: AsyncSession,
    settings: Settings,
    http: httpx.AsyncClient,
    site: CompanySite,
    *,
    now: datetime | None = None,
) -> SyncResult:
    now = now or utcnow()
    adapter = ADAPTERS.get(site.key)
    result = SyncResult()
    if adapter is None:
        result.error = "адаптер не найден"
        return result
    first = site.first_synced_at is None
    result.backfill = first
    try:
        items = await adapter.fetch(http, settings)
    except SiteError as exc:
        site.last_error = str(exc)
        site.last_checked_at = now
        result.error = str(exc)
        log.warning("site_sync_failed", site=site.key, error=str(exc))
        return result
    result.items = len(items)
    city_list = cities(settings)
    stop_words = await _stop_words(session)
    keywords = parse_keywords(site.keywords)
    existing = {
        r.external_id: r
        for r in (await session.execute(select(SiteVacancy).where(SiteVacancy.site_key == site.key))).scalars().all()
    }
    seen_ids: set[str] = set()
    notify: list[SiteVacancy] = []
    for item in items:
        if item.external_id in seen_ids:
            continue
        seen_ids.add(item.external_id)
        match = is_match(item, city_list=city_list, stop_words=stop_words, keywords=keywords)
        row = existing.get(item.external_id)
        title = strip_emoji(item.title)[:500]
        if row is None:
            row = SiteVacancy(
                site_key=site.key,
                external_id=item.external_id,
                title=title,
                url=item.url,
                company=item.company,
                location=(item.location or "")[:500] or None,
                work_format=item.work_format,
                remote=item.remote,
                snippet=item.snippet,
                published_at=item.published_at,
                is_match=match,
                status="seen" if first else "new",
                first_seen_at=now,
                last_seen_at=now,
                seen_at=now if first else None,
            )
            session.add(row)
            result.added += 1
            if not first:
                result.new += 1
                if match:
                    notify.append(row)
        else:
            row.title = title
            row.url = item.url
            row.location = (item.location or "")[:500] or None
            row.work_format = item.work_format
            row.remote = item.remote
            row.snippet = item.snippet
            row.published_at = item.published_at or row.published_at
            row.is_match = match
            row.last_seen_at = now
            if row.status == "gone":  # re-published
                row.status = "seen"
                row.gone_at = None
        if match:
            result.matches += 1
    for ext, row in existing.items():
        if ext not in seen_ids and row.status != "gone":
            row.status = "gone"
            row.gone_at = now
            result.gone += 1
    if first:
        site.first_synced_at = now
    site.last_checked_at = now
    site.last_error = None
    site.last_new = 0 if first else len(notify)
    site.vacancies_count = len(seen_ids)
    await session.flush()
    if notify and site.notify:
        for row in notify:
            await emit_event(
                session,
                settings,
                "site_vacancy",
                {
                    "site": adapter.name,
                    "company": row.company or adapter.name,
                    "title": row.title,
                    "url": row.url,
                    "location": row.location,
                    "work_format": row.work_format,
                    "remote": row.remote,
                    "vacancy_id": row.id,
                    "site_key": site.key,
                },
                now=now,
            )
            row.notified_at = now
            result.notified += 1
    if result.added or result.gone:
        await audit(
            session,
            "sites",
            f"{adapter.name}: {'первая загрузка' if first else 'новых'} {result.added}, подходящих новых "
            f"{len(notify)}, исчезло {result.gone}, всего на сайте {len(seen_ids)}",
            site=site.key,
        )
    return result


async def sync_sites(runtime: Any, *, site_key: str | None = None) -> dict[str, Any]:
    """Job `sites_sync`: every enabled site (or one by key) in its own transaction."""
    settings: Settings = runtime.settings
    summary: dict[str, Any] = {"sites": 0, "added": 0, "new": 0, "notified": 0, "gone": 0, "errors": 0}
    async with runtime.session_factory() as session:
        rows = await ensure_sites(session)
        keys = [r.key for r in rows if (r.key == site_key if site_key else r.enabled)]
        await session.commit()
    if not keys:
        return {**summary, "skipped": "сайтов нет"}
    for n, key in enumerate(keys):
        if n:
            await asyncio.sleep(PAUSE_BETWEEN_SITES)
        async with runtime.session_factory() as session:
            site = (await session.execute(select(CompanySite).where(CompanySite.key == key))).scalar_one_or_none()
            if site is None:
                continue
            result = await sync_site(session, settings, runtime.http, site)
            await session.commit()
        summary["sites"] += 1
        if result.error:
            summary["errors"] += 1
            continue
        summary["added"] += result.added
        summary["new"] += result.new
        summary["notified"] += result.notified
        summary["gone"] += result.gone
    if summary["new"] or summary["errors"]:
        log.info("sites_sync", **summary)
    return summary


async def enabled_sites_count(session: AsyncSession) -> int:
    return int(
        (await session.execute(select(func.count(CompanySite.id)).where(CompanySite.enabled.is_(True)))).scalar() or 0
    )


# ---- rows and the feed -------------------------------------------------------------------------


def _iso(value: datetime | None) -> str | None:
    dt = as_utc(value)
    return dt.isoformat(timespec="seconds") if dt else None


async def new_matches_count(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(
                select(func.count(SiteVacancy.id)).where(SiteVacancy.status == "new", SiteVacancy.is_match.is_(True))
            )
        ).scalar()
        or 0
    )


async def list_sites(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await ensure_sites(session)
    counts = {
        str(k): int(n)
        for k, n in (
            await session.execute(
                select(SiteVacancy.site_key, func.count(SiteVacancy.id))
                .where(SiteVacancy.status == "new", SiteVacancy.is_match.is_(True))
                .group_by(SiteVacancy.site_key)
            )
        ).all()
    }
    matches = {
        str(k): int(n)
        for k, n in (
            await session.execute(
                select(SiteVacancy.site_key, func.count(SiteVacancy.id))
                .where(SiteVacancy.status != "gone", SiteVacancy.is_match.is_(True))
                .group_by(SiteVacancy.site_key)
            )
        ).all()
    }
    out = []
    for row in rows:
        adapter = ADAPTERS[row.key]
        out.append(
            {
                "key": row.key,
                "name": adapter.name,
                "url": adapter.url,
                "note": adapter.note,
                "enabled": row.enabled,
                "notify": row.notify,
                "keywords": row.keywords,
                "last_checked_at": _iso(row.last_checked_at),
                "last_new": row.last_new,
                "last_error": row.last_error,
                "vacancies_count": row.vacancies_count,
                "matches": matches.get(row.key, 0),
                "new_matches": counts.get(row.key, 0),
                "first_synced_at": _iso(row.first_synced_at),
            }
        )
    return out


def vacancy_row(row: SiteVacancy) -> dict[str, Any]:
    adapter = ADAPTERS.get(row.site_key)
    return {
        "id": row.id,
        "site_key": row.site_key,
        "site": adapter.name if adapter else row.site_key,
        "external_id": row.external_id,
        "title": row.title,
        "url": row.url,
        "company": row.company,
        "location": row.location,
        "work_format": row.work_format,
        "remote": row.remote,
        "snippet": row.snippet,
        "published_at": _iso(row.published_at),
        "is_match": row.is_match,
        "status": row.status,
        "first_seen_at": _iso(row.first_seen_at),
        "last_seen_at": _iso(row.last_seen_at),
        "notified_at": _iso(row.notified_at),
    }


async def list_vacancies(
    session: AsyncSession,
    *,
    site_key: str | None = None,
    only_new: bool = False,
    only_match: bool = True,
    include_hidden: bool = False,
    include_gone: bool = False,
    q: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> dict[str, Any]:
    stmt = select(SiteVacancy)
    if site_key:
        stmt = stmt.where(SiteVacancy.site_key == site_key)
    if only_new:
        stmt = stmt.where(SiteVacancy.status == "new")
    else:
        excluded = []
        if not include_hidden:
            excluded.append("hidden")
        if not include_gone:
            excluded.append("gone")
        if excluded:
            stmt = stmt.where(SiteVacancy.status.not_in(excluded))
    if only_match:
        stmt = stmt.where(SiteVacancy.is_match.is_(True))
    stmt = stmt.order_by(desc(SiteVacancy.first_seen_at), desc(SiteVacancy.id)).limit(LIST_MAX)
    rows = list((await session.execute(stmt)).scalars().all())
    if q and q.strip():
        needle = norm(q)
        rows = [r for r in rows if needle in norm(r.title) or needle in norm(r.location) or needle in norm(r.snippet)]
    total = len(rows)
    per_page = max(1, min(per_page, 200))
    page = max(1, page)
    chunk = rows[(page - 1) * per_page : page * per_page]
    return {
        "items": [vacancy_row(r) for r in chunk],
        "total": total,
        "page": page,
        "per_page": per_page,
        "new_count": await new_matches_count(session),
    }


async def summary(session: AsyncSession, settings: Settings) -> dict[str, Any]:
    rows = await ensure_sites(session)
    checked = [dt for dt in (as_utc(r.last_checked_at) for r in rows) if dt is not None]
    last = max(checked) if checked else None
    return {
        "sites": len(rows),
        "enabled": sum(1 for r in rows if r.enabled),
        "new_matches": await new_matches_count(session),
        "last_checked_at": last.isoformat(timespec="seconds") if last else None,
        "interval_min": settings.sites_sync_min,
        "cities": cities(settings),
        "unsupported": UNSUPPORTED,
    }


async def get_site(session: AsyncSession, key: str) -> CompanySite:
    await ensure_sites(session)
    row = (await session.execute(select(CompanySite).where(CompanySite.key == key))).scalar_one_or_none()
    if row is None:
        raise SiteError("сайт не найден")
    return row


async def update_site(session: AsyncSession, settings: Settings, site: CompanySite, **fields: Any) -> CompanySite:
    """Partial update; changed keywords re-flag the stored vacancies of the site."""
    if fields.get("enabled") is not None:
        site.enabled = bool(fields["enabled"])
    if fields.get("notify") is not None:
        site.notify = bool(fields["notify"])
    if fields.get("keywords") is not None:
        keywords = ", ".join(parse_keywords(str(fields["keywords"]))) or None
        if keywords != site.keywords:
            site.keywords = keywords
            await reflag_site(session, settings, site)
    await session.flush()
    return site


async def reflag_site(session: AsyncSession, settings: Settings, site: CompanySite) -> int:
    city_list = cities(settings)
    stop_words = await _stop_words(session)
    keywords = parse_keywords(site.keywords)
    changed = 0
    for row in (await session.execute(select(SiteVacancy).where(SiteVacancy.site_key == site.key))).scalars():
        item = SiteVacancyItem(row.external_id, row.title, row.url, row.location, row.work_format, row.remote)
        flag = is_match(item, city_list=city_list, stop_words=stop_words, keywords=keywords)
        if flag != row.is_match:
            row.is_match = flag
            changed += 1
    await session.flush()
    return changed


async def get_vacancy(session: AsyncSession, vacancy_id: int) -> SiteVacancy:
    row = await session.get(SiteVacancy, vacancy_id)
    if row is None:
        raise SiteError("вакансия не найдена")
    return row


async def set_status(session: AsyncSession, row: SiteVacancy, status: str) -> SiteVacancy:
    if status not in ("new", "seen", "hidden"):
        raise SiteError("статус: new, seen или hidden")
    row.status = status
    if status != "new" and row.seen_at is None:
        row.seen_at = utcnow()
    await session.flush()
    return row


async def mark_seen(session: AsyncSession, *, ids: list[int] | None = None, site_key: str | None = None) -> int:
    stmt = update(SiteVacancy).where(SiteVacancy.status == "new").values(status="seen", seen_at=utcnow())
    if ids:
        stmt = stmt.where(SiteVacancy.id.in_(ids))
    if site_key:
        stmt = stmt.where(SiteVacancy.site_key == site_key)
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)
