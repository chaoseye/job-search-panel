"""Contacts of a studio from its own site (job `studio_contacts`): the home page and up to three contact / about
pages it links to (or the usual /contacts paths), read with plain HTTP GET, the panel's public User-Agent and a pause
between requests — no browser, no bypass of bot protection, and robots.txt is respected. A site that only renders
with JavaScript or refuses non-browsers gets «контактов не нашлось» with the reason; the user can type an address in.

What is kept: the studio's e-mail addresses as published (mailto links, text, Cloudflare-protected addresses, «[at]»
spellings), addresses on the studio's own domain and role mailboxes (info@, hello@, hr@ …) first, at most MAX_EMAILS;
one Telegram link; the page the contacts were on; a short summary of the site (title, description, headings) for
the letter and the fit. Addresses of the site's vendors (example.com, sentry, wix …) are dropped.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import structlog
from bs4 import BeautifulSoup
from sqlalchemy import or_, select

from app.config import Settings
from app.db.base import utcnow
from app.db.models import Studio
from app.net import HTTP_ERRORS
from app.services.audit import audit
from app.services.preferences import get_preferences
from app.services.studios import (
    EMAIL_RE,
    _json,
    _loads,
    contact_status,
    detect_services,
    refresh_fit,
    site_parts,
    studio_kind,
)

log = structlog.get_logger("studio_contacts")

JOB = "studio_contacts"
FETCH_TIMEOUT = 20.0
PAUSE_SEC = 1.0
MAX_PAGES = 4
MAX_BYTES = 2_000_000
MAX_EMAILS = 5
SUMMARY_CHARS = 800
# a site that did not open (no connection, a timeout, a server error) is tried again by the next run without ids,
# but not sooner than RETRY_AFTER: a VPN that sends Russian hosting through a foreign exit, a site down for the night
RETRY_ERRORS = ("сайт не открылся", "сайт ответил HTTP 5")
RETRY_AFTER = timedelta(hours=3)
CONTACT_PATHS = ("/contacts", "/contact", "/kontakty")
CONTACT_WORDS = ("контакт", "contact", "kontakt", "связаться", "о нас", "о студии", "о компании", "about")
ROLE_LOCALS = {
    "info",
    "hello",
    "hi",
    "mail",
    "office",
    "sales",
    "hr",
    "job",
    "jobs",
    "career",
    "careers",
    "team",
    "contact",
    "contacts",
    "studio",
    "welcome",
    "partner",
    "partners",
    "pr",
    "marketing",
    "order",
    "zakaz",
    "request",
    "project",
    "projects",
    "new",
    "biz",
    "business",
    "work",
    "cv",
    "resume",
    "vacancy",
    "vacancies",
    "support",
}
VENDOR_DOMAINS = (
    "example.com",
    "example.ru",
    "example.org",
    "domain.com",
    "domain.ru",
    "email.com",
    "yourdomain.com",
    "site.ru",
    "mysite.ru",
    "test.ru",
    "sentry.io",
    "wixpress.com",
    "wix.com",
    "tilda.ws",
    "tildacdn.com",
    "sentry-next.wixpress.com",
)
FILE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")
TELEGRAM_LINK = re.compile(r"https?://(?:t|telegram)\.me/([A-Za-z0-9_]{4,32})(?:[/?#]|$)")
# «info [at] studio.example», «hr (собака) studio (dot) example»
AT_SPELLING = re.compile(
    r"([A-Za-z0-9._%+-]+)\s*[\[(]\s*(?:at|собака)\s*[\])]\s*"
    r"([A-Za-z0-9-]+(?:\s*[\[(]\s*dot\s*[\])]\s*|\.)[A-Za-z0-9.-]+)",
    re.IGNORECASE,
)
DOT_SPELLING = re.compile(r"\s*[\[(]\s*dot\s*[\])]\s*", re.IGNORECASE)


@dataclass
class SiteContacts:
    emails: list[str] = field(default_factory=list)
    telegram: str | None = None
    contact_url: str | None = None
    summary: str | None = None
    error: str | None = None


class FetchError(Exception):
    pass


# ---- parsing ---------------------------------------------------------------------------------------------------------


def decode_cf_email(hexstr: str) -> str | None:
    """Cloudflare «email protection»: the first byte is the XOR key of the rest."""
    try:
        data = bytes.fromhex(hexstr)
    except ValueError:
        return None
    if len(data) < 2:
        return None
    return "".join(chr(b ^ data[0]) for b in data[1:])


def _email_ok(email: str) -> bool:
    email = email.lower().strip(".")
    local, _, domain = email.partition("@")
    if not local or not domain or email.endswith(FILE_SUFFIXES) or re.search(r"@\d+x\.", email):
        return False
    if any(domain == v or domain.endswith("." + v) for v in VENDOR_DOMAINS):
        return False
    return local not in ("noreply", "no-reply", "donotreply", "mailer-daemon")


def extract_emails(soup: BeautifulSoup, text: str) -> list[str]:
    found: list[str] = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        if href.lower().startswith("mailto:"):
            found.append(href[7:].split("?", 1)[0])
        elif "/cdn-cgi/l/email-protection#" in href:
            decoded = decode_cf_email(href.rsplit("#", 1)[1])
            if decoded:
                found.append(decoded)
    for node in soup.select("[data-cfemail]"):
        decoded = decode_cf_email(str(node.get("data-cfemail") or ""))
        if decoded:
            found.append(decoded)
    found += EMAIL_RE.findall(text)
    for local, domain in AT_SPELLING.findall(text):
        found.append(f"{local}@{DOT_SPELLING.sub('.', domain)}")
    out: list[str] = []
    for raw in found:
        email = raw.strip().strip(".,;:").lower()
        if EMAIL_RE.fullmatch(email) and _email_ok(email) and email not in out:
            out.append(email)
    return out


def rank_emails(emails: list[str], domain: str | None) -> list[str]:
    """The studio's own domain first, role mailboxes before personal ones; at most MAX_EMAILS."""

    def key(email: str) -> tuple[int, int]:
        local, _, host = email.partition("@")
        own = domain is not None and (host == domain or host.endswith("." + domain))
        return (0 if own else 1, 0 if local in ROLE_LOCALS else 1)

    return sorted(dict.fromkeys(emails), key=key)[:MAX_EMAILS]


def telegram_of(soup: BeautifulSoup) -> str | None:
    for a in soup.find_all("a", href=True):
        match = TELEGRAM_LINK.match(str(a["href"]).strip())
        if match and match.group(1).lower() not in ("share", "joinchat", "addstickers", "iv"):
            return f"https://t.me/{match.group(1)}"
    return None


def site_summary(soup: BeautifulSoup) -> str:
    parts: list[str] = []
    if soup.title and soup.title.string:
        parts.append(soup.title.string.strip())
    for key, value in (("name", "description"), ("property", "og:description")):
        meta = soup.find("meta", attrs={key: value})
        content = str(meta.get("content") or "").strip() if meta else ""
        if content and content not in parts:
            parts.append(content)
    for tag in soup.find_all(["h1", "h2"])[:6]:
        heading = " ".join(tag.get_text(" ", strip=True).split())
        if heading and heading not in parts and len(heading) < 200:
            parts.append(heading)
    return " · ".join(parts)[:SUMMARY_CHARS]


def contact_links(soup: BeautifulSoup, base_url: str, domain: str) -> list[str]:
    links: list[str] = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        label = " ".join(a.get_text(" ", strip=True).split()).lower()
        target = urljoin(base_url, href)
        host = (urlsplit(target).hostname or "").lower()
        if not (host == domain or host.endswith("." + domain)) or urlsplit(target).scheme not in ("http", "https"):
            continue
        low = href.lower()
        if any(w in low or w in label for w in CONTACT_WORDS):
            clean = target.split("#", 1)[0]
            if clean not in links and clean.rstrip("/") != base_url.rstrip("/"):
                links.append(clean)
    return links


# ---- fetching --------------------------------------------------------------------------------------------------------


async def _fetch(http: httpx.AsyncClient, settings: Settings, url: str) -> tuple[str, str]:
    """(final url, html). Errors carry a short reason for the studio card."""
    try:
        resp = await http.get(
            url,
            headers={"User-Agent": settings.public_user_agent, "Accept-Language": "ru,en;q=0.8"},
            timeout=httpx.Timeout(FETCH_TIMEOUT),
            follow_redirects=True,
        )
    except HTTP_ERRORS as exc:
        raise FetchError(f"сайт не открылся: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        low = resp.text[:20000].lower()
        if "challenge" in low or "captcha" in low or "antibot" in low:
            raise FetchError("сайт закрыт защитой от роботов — панель её не обходит")
        raise FetchError(f"сайт ответил HTTP {resp.status_code}")
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype:
        raise FetchError(f"не страница ({ctype or 'без типа'})")
    return str(resp.url), resp.content[:MAX_BYTES].decode(resp.encoding or "utf-8", errors="replace")


async def _robots(http: httpx.AsyncClient, settings: Settings, site: str) -> RobotFileParser | None:
    """robots.txt of the site, None when there is none. A site that does not answer at all (no connection, a timeout)
    raises FetchError: the home page would only wait as long again (about half a minute per dead site)."""
    try:
        resp = await http.get(
            urljoin(site, "/robots.txt"),
            headers={"User-Agent": settings.public_user_agent},
            timeout=httpx.Timeout(10.0),
            follow_redirects=True,
        )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise FetchError(f"сайт не открылся: {type(exc).__name__}") from exc
    except HTTP_ERRORS:
        return None
    if resp.status_code != 200:
        return None
    parser = RobotFileParser()
    parser.parse(resp.text.splitlines())
    return parser


async def fetch_contacts(
    http: httpx.AsyncClient, settings: Settings, site_url: str, *, pause: float | None = None
) -> SiteContacts:
    site, domain = site_parts(site_url)
    if site is None or domain is None:
        return SiteContacts(error="нет адреса сайта")
    pause = PAUSE_SEC if pause is None else pause  # read at call time: tests set the module constant to 0
    agent = settings.public_user_agent
    try:
        robots = await _robots(http, settings, site)
    except FetchError as exc:
        return SiteContacts(error=str(exc))
    if robots is not None and not robots.can_fetch(agent, site + "/"):
        return SiteContacts(error="robots.txt сайта запрещает роботам его читать")
    try:
        home_url, home_html = await _fetch(http, settings, site + "/")
    except FetchError as exc:
        return SiteContacts(error=str(exc))
    home = BeautifulSoup(home_html, "lxml")
    result = SiteContacts(summary=site_summary(home) or None)
    emails = extract_emails(home, home.get_text(" ", strip=True))
    result.telegram = telegram_of(home)
    if emails:
        result.contact_url = home_url
    candidates = contact_links(home, home_url, domain)
    candidates += [urljoin(site, p) for p in CONTACT_PATHS if not candidates]
    for url in candidates[: MAX_PAGES - 1]:
        if robots is not None and not robots.can_fetch(agent, url):
            continue
        await asyncio.sleep(pause)
        try:
            page_url, page_html = await _fetch(http, settings, url)
        except FetchError:
            continue
        soup = BeautifulSoup(page_html, "lxml")
        page_emails = extract_emails(soup, soup.get_text(" ", strip=True))
        if page_emails and result.contact_url in (None, home_url):
            result.contact_url = page_url
        emails += page_emails
        result.telegram = result.telegram or telegram_of(soup)
    result.emails = rank_emails(emails, domain)
    if not result.emails and not result.telegram:
        result.error = "на сайте не нашлось почты и Telegram (возможно, контакты рисуются скриптом — впишите вручную)"
    return result


async def apply_contacts(session: Any, row: Studio, found: SiteContacts, prefs: Any) -> None:
    """Found addresses join the ones typed in by hand; the studio's status follows its contacts."""
    manual = _loads(row.emails_json, [])
    row.emails_json = _json(list(dict.fromkeys([*manual, *found.emails]))[:MAX_EMAILS])
    row.telegram = row.telegram or found.telegram
    row.contact_url = found.contact_url or row.contact_url
    row.contacts_error = found.error
    row.contacts_checked_at = utcnow()
    if found.summary:
        row.site_summary = found.summary
        text = " ".join(x for x in (row.name, row.description or "", row.site_summary) if x)
        row.services_json = _json(detect_services(text))
        kind = studio_kind(text)
        if kind != "unknown" and row.kind in ("unknown", None):
            row.kind = kind
    if row.status in ("new", "ready", "no_contacts"):
        row.status = contact_status(row)
    await refresh_fit(session, row, prefs)


async def _due(session: Any, limit: int, now: datetime) -> tuple[list[int], int]:
    """(studio ids, how many of them are retries): the studios never checked, best fit first, then the ones whose
    site did not open (RETRY_ERRORS) at least RETRY_AFTER ago, the longest waiting first."""
    fresh = list(
        (
            await session.execute(
                select(Studio.id)
                .where(Studio.site_url.isnot(None), Studio.contacts_checked_at.is_(None), Studio.status == "new")
                .order_by(Studio.fit_score.desc().nulls_last(), Studio.id)
            )
        ).scalars()
    )[:limit]
    retry = list(
        (
            await session.execute(
                select(Studio.id)
                .where(
                    Studio.site_url.isnot(None),
                    Studio.status == "no_contacts",
                    Studio.contacts_checked_at < now - RETRY_AFTER,
                    or_(*(Studio.contacts_error.like(f"{prefix}%") for prefix in RETRY_ERRORS)),
                )
                .order_by(Studio.contacts_checked_at, Studio.id)
            )
        ).scalars()
    )[: max(0, limit - len(fresh))]
    return fresh + retry, len(retry)


async def check_contacts(runtime: Any, ids: list[int] | None = None) -> dict[str, Any]:
    """Job `studio_contacts`: the given studios, else the ones never checked and the ones whose site did not open
    (_due), at most STUDIOS_CONTACTS_PER_RUN sites per run."""
    settings: Settings = runtime.settings
    summary: dict[str, Any] = {"checked": 0, "with_contacts": 0, "without": 0, "errors": 0, "retried": 0}
    limit = max(0, settings.studios_contacts_per_run)
    async with runtime.session_factory() as session:
        if ids:
            stmt = select(Studio.id).where(Studio.site_url.isnot(None), Studio.id.in_(ids))
            todo = list(
                (await session.execute(stmt.order_by(Studio.fit_score.desc().nulls_last(), Studio.id))).scalars()
            )[:limit]
        else:
            todo, summary["retried"] = await _due(session, limit, utcnow())
        prefs = await get_preferences(session)
    for studio_id in todo:
        async with runtime.session_factory() as session:
            row = await session.get(Studio, studio_id)
            if row is None or not row.site_url:
                continue
            site_url = row.site_url
        found = await fetch_contacts(runtime.http, settings, site_url)
        async with runtime.session_factory() as session:
            row = await session.get(Studio, studio_id)
            if row is None:
                continue
            await apply_contacts(session, row, found, prefs)
            await session.commit()
        summary["checked"] += 1
        if found.emails or found.telegram:
            summary["with_contacts"] += 1
        else:
            summary["without"] += 1
            summary["errors"] += 1 if found.error and "не нашлось" not in found.error else 0
    async with runtime.session_factory() as session:
        await audit(
            session,
            "studios",
            f"Контакты студий: проверено {summary['checked']}, с контактами {summary['with_contacts']}"
            + (f" (повторно — {summary['retried']}: сайт раньше не открылся)" if summary["retried"] else ""),
        )
        await session.commit()
    return summary
