"""Public Telegram channels with vacancies → one feed in the panel and links to the bot.

The Bot API cannot read channels the bot is not an admin of, and the panel does not log into Telegram with the
user's account, so channels are read through their public web preview (`https://t.me/s/<username>`): a plain
HTTP page with the last 20 posts, older ones by `?before=<message id>`. Private channels and channels with the
preview switched off cannot be read — adding one fails with a clear reason. Each post is stored once
(`channel_posts`, unique per channel and message id); the posts of a channel's first sync are a backfill
(shown, but neither «new» nor sent to Telegram), later ones are «new» and, when they look like a vacancy
(`looks_like_vacancy`, or every post when the channel mode is `all`), go to the bot as the event `channel_post`.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
import structlog
from bs4 import BeautifulSoup
from sqlalchemy import Integer, cast, delete, desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import ChannelPost, TelegramChannel, Vacancy
from app.hh.textclean import html_to_text, sanitize_html
from app.hh.vacancy import HH_VACANCY_LINK
from app.net import HTTP_ERRORS
from app.services.audit import audit
from app.services.notify import emit_event
from app.services.textmatch import contains_term, norm, strip_emoji

log = structlog.get_logger("channels")

PREVIEW_BASE = "https://t.me/s"
CHANNEL_BASE = "https://t.me"
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
# t.me/<path> that are Telegram's own pages, not channels (t.me/joinchat/… used to add a channel named «joinchat»)
RESERVED_PATHS = frozenset(
    {"joinchat", "addlist", "addstickers", "addemoji", "addtheme", "share", "proxy", "socks", "setlanguage", "login"}
    | {"confirmphone", "invoice", "boost", "iv", "msg", "contact", "bg"}
)
_INPUT_PREFIX = re.compile(r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/(?:s/)?", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s\"'<>()]+")
# a shared chat folder (t.me/addlist/<slug>): its web page names the folder and counts the chats, nothing more
_FOLDER_RE = re.compile(r"(?:https?://)?(?:www\.)?t\.me/addlist/([A-Za-z0-9_\-]+)", re.IGNORECASE)
_FOLDER_DESC_RE = re.compile(r"You can add\s*<b>\s*(\d+)\s*chats?\s*</b>\s*from the\s*<strong>(.*?)</strong>", re.S)
FOLDER_HINT = (
    "Telegram не показывает состав папки в веб-версии (только в приложении), а панель не входит в Telegram под "
    "вашей учётной записью. Откройте папку в Telegram и вставьте её каналы списком: @имя через пробел или по "
    "одному в строке."
)
MODES = ("vacancies", "all")
JOB = "channel_sync"
MAX_PAGES = 3  # per sync, when more than one page of new posts appeared since the last check
FETCH_TIMEOUT = 20.0
PAUSE_BETWEEN_CHANNELS = 0.3
TITLE_CHARS = 200
MIN_TEXT_CHARS = 25  # job boards post «title / location / company» only
LIST_MAX = 5000

# a post is a vacancy when it carries a strong marker, two job-post markers, or a job-post marker plus a role
# word — and no résumé marker (unless it is tagged as a vacancy)
STRONG_MARKERS = (
    "#вакансия",
    "#вакансии",
    "#vacancy",
    "#job",
    "#jobs",
    "#hiring",
    "#работа",
    "вакансия",
    "вакансии",
    "дайджест вакансий",
    "подборка вакансий",
    "vacancy",
    "hiring",
    "wanted",
    "we are looking for",
    "we're looking for",
    "job opening",
    "join our team",
    "требуется",
    "требуются",
    "открыта позиция",
    "открытая позиция",
    "приглашаем в команду",
    "в нашу команду",
    "идеальный кандидат",
    # Ukrainian job channels
    "шукаємо",
    "шукає",
    "потрібен",
    "потрібна",
    "запрошуємо",
    "відкрита вакансія",
)
# words of a job post: salary, format, sections, how to apply (Russian, English, Ukrainian)
JOB_MARKERS = (
    "з/п",
    "зп:",
    "зп ",
    "зарплата",
    "оклад",
    "вилка",
    "salary",
    "compensation",
    "ищем",
    "ищет",
    "оплачиваем",
    # job boards: «Company: … Location: … Apply here», 💼/📍 lines
    "company:",
    "location:",
    "date posted",
    "apply here",
    "apply now",
    "apply →",
    "view here",
    "💼",
    "📍",
    "обязанности",
    "требования",
    "условия работы",
    "что предстоит",
    "задачи:",
    "чем предстоит",
    "мы предлагаем",
    "работодатель",
    "responsibilities",
    "requirements",
    "benefits",
    "опыт работы",
    "формат работы",
    "график работы",
    "удалённо",
    "удаленно",
    "удалёнка",
    "удаленка",
    "remote",
    "гибрид",
    "hybrid",
    "офис",
    "office",
    "полная занятость",
    "full-time",
    "fulltime",
    "part-time",
    "стажировк",
    "internship",
    "откликнуться",
    "отклик",
    "резюме присылайте",
    "присылайте резюме",
    "присылать резюме",
    "отправляйте резюме",
    "apply",
    "contact",
    "в лс",
    "вимоги",
    "обов'язки",
    "обовязки",
    "досвід",
    "умови",
    "оплата",
    "ремоут",
    "віддалено",
    "графік",
    "пропонуємо",
)
# levels and roles: count only together with a job marker (ads and articles mention roles too)
ROLE_MARKERS = (
    "junior",
    "middle",
    "senior",
    "lead",
    "head of",
    "тимлид",
    "стажёр",
    "стажер",
    "intern",
    "разработчик",
    "developer",
    "engineer",
    "инженер",
    "аналитик",
    "analyst",
    "менеджер",
    "manager",
    "программист",
    "специалист",
    "specialist",
    "архитектор",
    "architect",
    "devops",
    "data scientist",
    "тестировщик",
    "qa",
    "дизайнер",
    "designer",
    "копирайтер",
    "copywriter",
    "маркетолог",
    "marketer",
    "рекрутер",
    "recruiter",
    "buyer",
    "байер",
    "медиабайер",
    "affiliate manager",
    "аффилиат-менеджер",
    "farmer",
    "фармер",
    "обработчик",
    "оператор",
    "модератор",
    "moderator",
    "support manager",
    "sales manager",
    "sales-manager",
    "менеджер по продажам",
    "закупщик",
    "seo specialist",
    "seo-специалист",
    "сео-специалист",
    "smm-менеджер",
    "expert",
    "officer",
    "associate",
    "consultant",
    "counsel",
    "writer",
    "creator",
    "trader",
    "researcher",
    "директор",
    "director",
    "фахівець",
    "спеціаліст",
    "керівник",
)
RESUME_MARKERS = (
    "#резюме",
    "#resume",
    "#cv",
    "#ищуработу",
    "#opentowork",
    "ищу работу",
    "ищу проект",
    "ищу удал",
    "я в поисках работы",
    "нахожусь в поиске",
    "рассмотрю предложения",
    "open to work",
    "looking for a job",
    "looking for job",
    "looking for new opportunities",
    "шукаю роботу",
)
VACANCY_TAGS = ("#вакансия", "#вакансии", "#vacancy", "#job", "#jobs", "#hiring")
# a résumé for sure, whatever else the post says («Seeking or Hiring: SEEKING» templates carry the word «hiring»)
RESUME_EXPLICIT = ("#резюме", "#resume", "#cv", "#ищуработу", "#opentowork", "my experience (in bullets)")
_SEEKING_RE = re.compile(r"seeking or hiring\W{0,8}seeking", re.IGNORECASE)
# editorial posts about hiring (an interview with a CEO, a column, a podcast) are not vacancies: two of these without
# a strong marker and the post is an article. «интервью», «кейс», «опрос» are left out — real vacancies use them
ARTICLE_MARKERS = (
    "обсудил",
    "обсудили",
    "рассказал",
    "рассказала",
    "поговорили",
    "автор ",
    "колонка",
    "подкаст",
    "разбор",
    "мнение",
    "лонгрид",
    "статья",
    "статье",
    "почему кандидаты",
    "как проходит",
    "исследование",
    "новости",
)
# an amount of money («300k ₽», «$5k», «80 000 руб», «от 250к») counts as one job marker; a bare $ in a blog does not
_SALARY_RE = re.compile(r"(?:[$€₽]|usd|eur|руб)\s?\d|\d\s?(?:[$€₽]|k\b|к\b|usd|eur|руб|тыс)", re.IGNORECASE)


class ChannelError(Exception):
    pass


@dataclass
class PreviewPost:
    message_id: int
    posted_at: datetime | None
    html: str
    text: str
    links: list[str] = field(default_factory=list)


@dataclass
class PreviewPage:
    title: str | None
    posts: list[PreviewPost]
    before: int | None  # cursor to the older page (`?before=`), None on the last one


# ---- parsing ---------------------------------------------------------------------------------


def folder_slug(text: str) -> str | None:
    m = _FOLDER_RE.search((text or "").strip())
    return m.group(1) if m else None


def split_inputs(text: str) -> list[str]:
    """Several channels at once: whitespace, commas or new lines between them."""
    return [t for t in re.split(r"[\s,;]+", text or "") if t]


def parse_channel_input(text: str) -> str:
    """`@name`, `name`, `https://t.me/name`, `t.me/s/name`, `https://t.me/name/123` → `name` (lowercase)."""
    s = (text or "").strip()
    if folder_slug(s):
        raise ChannelError("это ссылка на папку Telegram, а не на канал. " + FOLDER_HINT)
    s = _INPUT_PREFIX.sub("", s).lstrip("@")
    if s.startswith("+") or s.lower().startswith("joinchat/"):
        raise ChannelError(
            "это ссылка-приглашение в закрытый канал или чат: панель читает только публичные каналы "
            "(их веб-страница t.me/s/имя открыта всем)"
        )
    s = s.split("/", 1)[0].split("?", 1)[0].strip()
    if s.lower() in RESERVED_PATHS:
        raise ChannelError(f"t.me/{s} — служебная ссылка Telegram, а не канал: нужна ссылка вида https://t.me/имя")
    if not USERNAME_RE.match(s):
        raise ChannelError("нужен публичный канал: @имя или ссылка вида https://t.me/имя (буквы, цифры, _)")
    return s.lower()


def parse_keywords(raw: str | None) -> list[str]:
    return [k.strip() for k in re.split(r"[,;\n]+", raw or "") if k.strip()]


def post_url(username: str, message_id: int) -> str:
    return f"{CHANNEL_BASE}/{username}/{message_id}"


def _is_tag_line(line: str) -> bool:
    """`#senior #удаленка` — a line of hashtags only (many job channels start posts with one)."""
    return "#" in line and all(w.startswith("#") for w in line.split())


# job boards post «Company: … / Title: … / Location: …»: the position is the title, the company follows it
_FIELD_RE = re.compile(r"^([A-Za-zА-Яа-яЁё][\w /-]{1,30}?)\s*:\s*(.+)$")
ROLE_KEYS = ("title", "job title", "position", "role", "job", "vacancy", "вакансия", "должность", "позиция", "роль")
COMPANY_KEYS = ("company", "employer", "компания", "работодатель", "проект", "project")


def _structured_title(lines: list[str]) -> str | None:
    fields: dict[str, str] = {}
    for line in lines:
        m = _FIELD_RE.match(line)
        if m:
            key = m.group(1).strip().lower()
            fields.setdefault(key, m.group(2).strip())
    first = _FIELD_RE.match(lines[0])
    if first is None or first.group(1).strip().lower() in ROLE_KEYS:
        return None  # a plain first line, or «Вакансия: X» which already is the title
    role = next((fields[k] for k in ROLE_KEYS if fields.get(k)), None)
    if not role:
        return None
    company = next((fields[k] for k in COMPANY_KEYS if fields.get(k)), None)
    return f"{role} — {company}" if company and norm(company) not in norm(role) else role


def post_title(text: str) -> str:
    """The first meaningful line; a short one (a company name) is joined with the next (the position); in
    «Company: … / Title: …» posts the position line."""
    lines: list[str] = []
    for line in (text or "").splitlines():
        s = strip_emoji(line)
        if len(s) >= 3 and not _is_tag_line(s):
            lines.append(s)
        if len(lines) == 8:
            break
    if not lines:
        return strip_emoji(text)[:TITLE_CHARS]
    structured = _structured_title(lines)
    if structured:
        return structured[:TITLE_CHARS]
    title = lines[0]
    if len(lines) > 1 and len(lines[1]) < 80:
        if title.endswith(":"):
            title = f"{title} {lines[1]}"
        elif len(title) < 25 and ":" not in title and ":" not in lines[1] and not lines[1][:1].islower():
            title = f"{title} — {lines[1]}"
    return title[:TITLE_CHARS]


def extract_links(anchors: list[str], text: str) -> list[str]:
    out: list[str] = []
    for url in [*anchors, *_URL_RE.findall(text or "")]:
        url = url.rstrip(".,;:!?»)")
        if url.startswith(("http://", "https://")) and url not in out:
            out.append(url)
    return out


def hh_vacancy_urls(links: list[str], text: str = "") -> list[str]:
    ids: list[str] = []
    for source in [*links, text or ""]:
        for m in HH_VACANCY_LINK.finditer(source):
            if m.group(1) not in ids:
                ids.append(m.group(1))
    return [f"https://hh.ru/vacancy/{i}" for i in ids]


def looks_like_vacancy(text: str) -> bool:
    """Heuristic for mixed channels: job words vs résumé words; short posts are never vacancies."""
    t = norm(text)
    if len(t) < MIN_TEXT_CHARS:
        return False
    if any(m in t for m in RESUME_EXPLICIT) or _SEEKING_RE.search(t):
        return False
    strong = sum(1 for m in STRONG_MARKERS if m in t)
    job = sum(1 for m in JOB_MARKERS if m in t) + (1 if _SALARY_RE.search(t) else 0)
    role = sum(1 for m in ROLE_MARKERS if m in t)
    resume = any(m in t for m in RESUME_MARKERS) and not any(tag in t for tag in VACANCY_TAGS)
    if resume and strong == 0:  # «ищу работу» without «ищем»/«требуется»: a résumé, not a vacancy
        return False
    if strong == 0 and sum(1 for m in ARTICLE_MARKERS if m in t) >= 2:
        return False  # a column about hiring, not a job post
    return strong >= 1 or job >= 2 or (job >= 1 and role >= 2)


_CYRILLIC_RE = re.compile(r"[а-яёіїєґ]")
_LATIN_RE = re.compile(r"[a-z]")
UK_LETTERS = "іїєґ"
RU_LETTERS = "ыэёъ"


def detect_language(text: str) -> str:
    """ru | en | uk | other by the alphabet: Ukrainian has і/ї/є/ґ and no ы/э/ё/ъ, Russian the reverse."""
    t = (text or "").lower()
    cyr = len(_CYRILLIC_RE.findall(t))
    lat = len(_LATIN_RE.findall(t))
    if cyr == 0 and lat == 0:
        return "other"
    if cyr < max(10, lat / 4):  # tech words in a Russian post are Latin, so only a Latin-dominated text is English
        return "en"
    uk = sum(t.count(c) for c in UK_LETTERS)
    ru = sum(t.count(c) for c in RU_LETTERS)
    return "uk" if uk >= 3 and uk >= ru else "ru"


def allowed_languages(settings: Settings) -> set[str]:
    return {p.strip().lower() for p in (settings.channel_languages or "").split(",") if p.strip()}


def is_vacancy_post(text: str, *, mode: str, keywords: list[str], languages: set[str] | None = None) -> bool:
    if languages and detect_language(text) not in languages:
        return False
    if keywords:
        t = norm(text)
        if not any(contains_term(t, k) for k in keywords):
            return False
    if mode == "all":
        return True
    return looks_like_vacancy(text)


def parse_preview(html: str) -> PreviewPage:
    """Posts of one t.me/s page: id, time, sanitized HTML, plain text, links; service messages and media-only
    posts are skipped (a vacancy always has text)."""
    soup = BeautifulSoup(html, "lxml")
    title_el = soup.select_one(".tgme_channel_info_header_title")
    title = title_el.get_text(" ", strip=True) if title_el else None
    posts: list[PreviewPost] = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        if "service_message" in (msg.get("class") or []):
            continue
        try:
            message_id = int(str(msg.get("data-post") or "").rsplit("/", 1)[-1])
        except ValueError:
            continue
        text_el = msg.select_one("div.js-message_text")  # the post itself, not the quoted reply
        if text_el is None:
            continue
        anchors: list[str] = []
        for a in text_el.find_all("a", href=True):
            href = str(a.get("href") or "")
            if href.startswith("?") or href.startswith("tg://"):
                a.unwrap()  # hashtags and app links are plain text in the panel
                continue
            anchors.append(href)
        inner = text_el.decode_contents()
        text = html_to_text(inner)
        if not text.strip():
            continue
        posted_at: datetime | None = None
        time_el = msg.select_one(".tgme_widget_message_date time[datetime]")
        if time_el is not None:
            try:
                posted_at = as_utc(datetime.fromisoformat(str(time_el.get("datetime"))))
            except ValueError:
                posted_at = None
        posts.append(
            PreviewPost(
                message_id=message_id,
                posted_at=posted_at,
                html=sanitize_html(inner),
                text=text,
                links=extract_links(anchors, text),
            )
        )
    more = soup.select_one("a.tme_messages_more[data-before]")
    before: int | None = None
    if more is not None:
        try:
            before = int(str(more.get("data-before")))
        except ValueError:
            before = None
    return PreviewPage(title=title, posts=posts, before=before)


# ---- fetching --------------------------------------------------------------------------------


async def folder_info(http: httpx.AsyncClient, settings: Settings, slug: str) -> dict[str, Any]:
    """Name and chat count of a shared folder from its public page (the chats themselves are not exposed)."""
    headers = {"User-Agent": settings.public_user_agent, "Accept-Language": "en"}
    try:
        resp = await http.get(
            f"{CHANNEL_BASE}/addlist/{slug}",
            headers=headers,
            timeout=httpx.Timeout(FETCH_TIMEOUT),
            follow_redirects=False,
        )
    except HTTP_ERRORS:
        return {}
    if resp.status_code != 200:
        return {}
    m = _FOLDER_DESC_RE.search(resp.text)
    if not m:
        return {}
    name = html_to_text(m.group(2)).strip()
    return {"name": name, "count": int(m.group(1))}


async def fetch_preview(
    http: httpx.AsyncClient, settings: Settings, username: str, *, before: int | None = None
) -> PreviewPage:
    url = f"{PREVIEW_BASE}/{username}" + (f"?before={before}" if before else "")
    headers = {"User-Agent": settings.public_user_agent, "Accept-Language": "ru,en;q=0.8"}
    try:
        resp = await http.get(url, headers=headers, timeout=httpx.Timeout(FETCH_TIMEOUT), follow_redirects=False)
    except HTTP_ERRORS as exc:
        raise ChannelError(f"сеть: {type(exc).__name__}") from exc
    if resp.status_code in (301, 302, 303, 307, 308):
        raise ChannelError(
            "предпросмотр недоступен: канал приватный, без веб-версии или такого канала нет "
            "(панель читает только публичные каналы через t.me/s/<имя>)"
        )
    if resp.status_code == 404:
        raise ChannelError("канал не найден")
    if resp.status_code != 200:
        raise ChannelError(f"t.me ответил {resp.status_code}")
    page = parse_preview(resp.text)
    if not page.posts and not page.title:
        raise ChannelError("страница без постов и названия — предпросмотр закрыт?")
    return page


# ---- sync ------------------------------------------------------------------------------------


async def _known_message_ids(session: AsyncSession, channel_id: int, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    rows = (
        await session.execute(
            select(ChannelPost.message_id).where(ChannelPost.channel_id == channel_id, ChannelPost.message_id.in_(ids))
        )
    ).scalars()
    return {int(m) for m in rows}


async def _vacancy_ids_by_hh(session: AsyncSession, urls: list[str]) -> dict[str, int]:
    hh_ids = [u.rsplit("/", 1)[-1] for u in urls]
    if not hh_ids:
        return {}
    rows = (await session.execute(select(Vacancy.hh_id, Vacancy.id).where(Vacancy.hh_id.in_(hh_ids)))).all()
    return {str(h): int(i) for h, i in rows}


def _first_vacancy_id(hh_urls: list[str], by_hh: dict[str, int]) -> int | None:
    for url in hh_urls:
        hh_id = url.rsplit("/", 1)[-1]
        if hh_id in by_hh:
            return by_hh[hh_id]
    return None


async def sync_channel(
    session: AsyncSession,
    settings: Settings,
    http: httpx.AsyncClient,
    channel: TelegramChannel,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reads the preview, stores posts newer than `last_message_id` (all posts on the first run), emits
    `channel_post` for new vacancy posts. Errors are kept in `last_error`, never raised."""
    now = now or utcnow()
    first = channel.last_message_id is None
    pages: list[PreviewPage] = []
    try:
        page = await fetch_preview(http, settings, channel.username)
        pages.append(page)
        while (
            not first
            and channel.last_message_id is not None
            and page.before is not None
            and page.before > channel.last_message_id
            and len(pages) < MAX_PAGES
        ):
            page = await fetch_preview(http, settings, channel.username, before=page.before)
            pages.append(page)
    except ChannelError as exc:
        channel.last_error = str(exc)
        channel.last_checked_at = now
        log.warning("channel_sync_failed", channel=channel.username, error=str(exc))
        return {"error": str(exc)}
    if pages[0].title:
        channel.title = strip_emoji(pages[0].title)[:300] or None
    posts: list[PreviewPost] = []
    seen_ids: set[int] = set()
    for page in pages:
        for p in page.posts:
            if p.message_id not in seen_ids:  # pages may overlap
                seen_ids.add(p.message_id)
                posts.append(p)
    candidates = [p for p in posts if first or p.message_id > int(channel.last_message_id or 0)]
    known = await _known_message_ids(session, channel.id, [p.message_id for p in candidates])
    fresh = sorted((p for p in candidates if p.message_id not in known), key=lambda p: p.message_id)
    keywords = parse_keywords(channel.keywords)
    languages = allowed_languages(settings)
    all_hh = hh_vacancy_urls([u for p in fresh for u in p.links], " ".join(p.text for p in fresh))
    vacancy_by_hh = await _vacancy_ids_by_hh(session, all_hh)
    added: list[tuple[ChannelPost, PreviewPost, list[str]]] = []
    for p in fresh:
        hh_urls = hh_vacancy_urls(p.links, p.text)
        row = ChannelPost(
            channel_id=channel.id,
            message_id=p.message_id,
            posted_at=p.posted_at,
            title=post_title(p.text),
            text=p.text,
            html=p.html,
            links_json=json.dumps(p.links, ensure_ascii=False),
            is_vacancy=is_vacancy_post(p.text, mode=channel.mode, keywords=keywords, languages=languages),
            lang=detect_language(p.text),
            status="seen" if first else "new",
            vacancy_id=_first_vacancy_id(hh_urls, vacancy_by_hh),
            seen_at=now if first else None,
            created_at=now,
        )
        session.add(row)
        added.append((row, p, hh_urls))
    top = max([p.message_id for p in posts], default=None)
    if top is not None:
        channel.last_message_id = max(int(channel.last_message_id or 0), top)
    elif first:
        channel.last_message_id = 0  # an empty channel: the next sync is no longer a backfill
    channel.last_checked_at = now
    channel.last_error = None
    channel.last_new = 0 if first else len(added)
    channel.posts_count = int(channel.posts_count or 0) + len(added)
    await session.flush()
    notified = 0
    # the panel feed always gets the event; Telegram only when the channel says so (and the event is enabled)
    event_settings = settings if channel.notify else settings.model_copy(update={"telegram_events": ""})
    if not first:
        for row, p, hh_urls in added:
            if not row.is_vacancy:
                continue
            body = p.text[len(row.title) :].strip() if p.text.startswith(row.title) else p.text
            rows = await emit_event(
                session,
                event_settings,
                "channel_post",
                {
                    "channel": channel.title or channel.username,
                    "username": channel.username,
                    "title": row.title,
                    "url": post_url(channel.username, row.message_id),
                    "hh_url": hh_urls[0] if hh_urls else None,
                    "snippet": " ".join(body.split())[:300],
                    "post_id": row.id,
                    "channel_id": channel.id,
                },
                now=now,
            )
            if any(r.channel == "telegram" and r.status == "queued" for r in rows):
                row.notified_at = now
                notified += 1
    vacancies = sum(1 for r, _, _ in added if r.is_vacancy)
    if added:
        await audit(
            session,
            "channels",
            f"@{channel.username}: {'первая загрузка' if first else 'новых постов'} {len(added)}"
            + ("" if first else f", вакансий {vacancies}"),
            channel_id=channel.id,
        )
    return {
        "posts": len(posts),
        "added": len(added),
        "new": 0 if first else len(added),
        "vacancies": vacancies,
        "notified": notified,
        "backfill": first,
    }


async def enabled_channels_count(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(select(func.count(TelegramChannel.id)).where(TelegramChannel.enabled.is_(True)))
        ).scalar()
        or 0
    )


async def sync_channels(runtime: Any, *, channel_id: int | None = None) -> dict[str, Any]:
    """Job `channel_sync`: every enabled channel (or one by id) in its own transaction, a short pause between."""
    settings: Settings = runtime.settings
    summary: dict[str, Any] = {"channels": 0, "added": 0, "new": 0, "notified": 0, "errors": 0}
    async with runtime.session_factory() as session:
        stmt = select(TelegramChannel.id).order_by(TelegramChannel.id)
        if channel_id:
            stmt = stmt.where(TelegramChannel.id == channel_id)
        else:
            stmt = stmt.where(TelegramChannel.enabled.is_(True))
        ids = [int(i) for i in (await session.execute(stmt)).scalars()]
    if not ids:
        return {**summary, "skipped": "каналов нет"}
    for n, cid in enumerate(ids):
        if n:
            await asyncio.sleep(PAUSE_BETWEEN_CHANNELS)
        async with runtime.session_factory() as session:
            channel = await session.get(TelegramChannel, cid)
            if channel is None:
                continue
            result = await sync_channel(session, settings, runtime.http, channel)
            await session.commit()
        summary["channels"] += 1
        if "error" in result:
            summary["errors"] += 1
            continue
        summary["added"] += int(result["added"])
        summary["new"] += int(result["new"])
        summary["notified"] += int(result["notified"])
    if summary["new"] or summary["errors"]:
        log.info("channel_sync", **summary)
    return summary


# ---- channel management ----------------------------------------------------------------------


def _validate_mode(mode: str | None) -> str:
    value = (mode or "vacancies").strip().lower()
    if value not in MODES:
        raise ChannelError("режим: vacancies (только похожие на вакансии) или all (все посты)")
    return value


async def get_channel(session: AsyncSession, channel_id: int) -> TelegramChannel:
    row = await session.get(TelegramChannel, channel_id)
    if row is None:
        raise ChannelError("канал не найден")
    return row


async def create_channel(
    session: AsyncSession,
    settings: Settings,
    http: httpx.AsyncClient,
    text: str,
    *,
    mode: str = "vacancies",
    keywords: str | None = None,
    notify: bool = True,
) -> TelegramChannel:
    """Adds the channel and reads it right away (a backfill); an unreadable channel is not added."""
    username = parse_channel_input(text)
    existing = (
        await session.execute(select(TelegramChannel).where(TelegramChannel.username == username))
    ).scalar_one_or_none()
    if existing is not None:
        raise ChannelError(f"@{username} уже добавлен")
    channel = TelegramChannel(
        username=username,
        enabled=True,
        mode=_validate_mode(mode),
        keywords=", ".join(parse_keywords(keywords)) or None,
        notify=notify,
    )
    session.add(channel)
    await session.flush()
    result = await sync_channel(session, settings, http, channel)
    if "error" in result:
        await session.delete(channel)
        await session.flush()
        raise ChannelError(f"@{username}: {result['error']}")
    await audit(
        session, "channels", f"Добавлен канал @{username} ({channel.title or 'без названия'})", channel_id=channel.id
    )
    return channel


async def add_channels(
    session: AsyncSession,
    settings: Settings,
    http: httpx.AsyncClient,
    text: str,
    *,
    mode: str = "vacancies",
    keywords: str | None = None,
    notify: bool = True,
) -> dict[str, Any]:
    """Several channels pasted at once; each is read right away, failures are reported per entry.

    A folder link (t.me/addlist/…) cannot be expanded: the entry gets the folder name and chat count with a hint."""
    added: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    seen: set[str] = set()
    for token in split_inputs(text):
        slug = folder_slug(token)
        if slug:
            info = await folder_info(http, settings, slug)
            what = f"папка «{info['name']}» ({info['count']} чатов)" if info else "папка Telegram"
            errors.append({"input": token, "error": f"это {what}, а не канал. {FOLDER_HINT}"})
            continue
        try:
            username = parse_channel_input(token)
        except ChannelError as exc:
            errors.append({"input": token, "error": str(exc)})
            continue
        if username in seen:
            continue
        seen.add(username)
        try:
            channel = await create_channel(
                session, settings, http, username, mode=mode, keywords=keywords, notify=notify
            )
        except ChannelError as exc:
            errors.append({"input": token, "error": str(exc)})
            continue
        added.append(channel_row(channel))
    return {"added": added, "errors": errors}


async def update_channel(
    session: AsyncSession, settings: Settings, channel: TelegramChannel, **fields: Any
) -> TelegramChannel:
    """Partial update; a changed mode or keywords re-flags the stored posts of the channel."""
    reflag = False
    if fields.get("enabled") is not None:
        channel.enabled = bool(fields["enabled"])
    if fields.get("notify") is not None:
        channel.notify = bool(fields["notify"])
    if fields.get("mode") is not None:
        mode = _validate_mode(fields["mode"])
        reflag = reflag or mode != channel.mode
        channel.mode = mode
    if fields.get("keywords") is not None:
        keywords = ", ".join(parse_keywords(str(fields["keywords"]))) or None
        reflag = reflag or keywords != channel.keywords
        channel.keywords = keywords
    if reflag:
        await reflag_posts(session, settings, channel_id=channel.id)
    await session.flush()
    return channel


async def reflag_posts(session: AsyncSession, settings: Settings, *, channel_id: int | None = None) -> dict[str, int]:
    """Recomputes `is_vacancy`, the language and the title for stored posts with the current dictionaries,
    languages and channel settings; channel titles lose their emoji too."""
    stmt = select(TelegramChannel)
    if channel_id:
        stmt = stmt.where(TelegramChannel.id == channel_id)
    channels = (await session.execute(stmt)).scalars().all()
    languages = allowed_languages(settings)
    changed = total = 0
    for channel in channels:
        if channel.title:
            channel.title = strip_emoji(channel.title)[:300] or None
        keywords = parse_keywords(channel.keywords)
        posts = (await session.execute(select(ChannelPost).where(ChannelPost.channel_id == channel.id))).scalars()
        for post in posts:
            total += 1
            post.lang = detect_language(post.text)
            post.title = post_title(post.text)
            flag = is_vacancy_post(post.text, mode=channel.mode, keywords=keywords, languages=languages)
            if flag != post.is_vacancy:
                post.is_vacancy = flag
                changed += 1
    await session.flush()
    return {"posts": total, "changed": changed, "channels": len(channels)}


async def delete_channel(session: AsyncSession, channel: TelegramChannel) -> None:
    await session.execute(delete(ChannelPost).where(ChannelPost.channel_id == channel.id))
    await audit(session, "channels", f"Удалён канал @{channel.username}", channel_id=channel.id)
    await session.delete(channel)
    await session.flush()


# ---- posts -----------------------------------------------------------------------------------


async def new_posts_count(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(
                select(func.count(ChannelPost.id)).where(ChannelPost.status == "new", ChannelPost.is_vacancy.is_(True))
            )
        ).scalar()
        or 0
    )


async def summary(session: AsyncSession, settings: Settings) -> dict[str, Any]:
    total, enabled = (
        await session.execute(select(func.count(TelegramChannel.id), func.sum(cast(TelegramChannel.enabled, Integer))))
    ).one()
    last = (await session.execute(select(func.max(TelegramChannel.last_checked_at)))).scalar()
    last_dt = as_utc(last)
    return {
        "channels": int(total or 0),
        "enabled": int(enabled or 0),
        "new_posts": await new_posts_count(session),
        "last_checked_at": last_dt.isoformat(timespec="seconds") if last_dt else None,
        "interval_min": settings.channel_sync_min,
        "languages": sorted(allowed_languages(settings)),
    }


def _iso(value: datetime | None) -> str | None:
    dt = as_utc(value)
    return dt.isoformat(timespec="seconds") if dt else None


def channel_row(channel: TelegramChannel, *, new_posts: int = 0) -> dict[str, Any]:
    return {
        "id": channel.id,
        "username": channel.username,
        "url": f"{CHANNEL_BASE}/{channel.username}",
        "title": channel.title,
        "enabled": channel.enabled,
        "mode": channel.mode,
        "keywords": channel.keywords,
        "notify": channel.notify,
        "last_message_id": channel.last_message_id,
        "last_checked_at": _iso(channel.last_checked_at),
        "last_new": channel.last_new,
        "last_error": channel.last_error,
        "posts_count": channel.posts_count,
        "new_posts": new_posts,
        "created_at": _iso(channel.created_at),
    }


async def list_channels(session: AsyncSession) -> list[dict[str, Any]]:
    channels = (await session.execute(select(TelegramChannel).order_by(TelegramChannel.id))).scalars().all()
    rows = (
        await session.execute(
            select(ChannelPost.channel_id, func.count(ChannelPost.id))
            .where(ChannelPost.status == "new", ChannelPost.is_vacancy.is_(True))
            .group_by(ChannelPost.channel_id)
        )
    ).all()
    counts: dict[int, int] = {int(cid): int(n) for cid, n in rows}
    return [channel_row(c, new_posts=counts.get(c.id, 0)) for c in channels]


def post_row(post: ChannelPost, channel: TelegramChannel | None, *, vacancy: Vacancy | None = None) -> dict[str, Any]:
    from app.services.vacancies import STATUS_LABELS

    try:
        links = [str(u) for u in json.loads(post.links_json or "[]")]
    except ValueError:
        links = []
    username = channel.username if channel else ""
    status = vacancy.pipeline_status if vacancy else None
    return {
        "id": post.id,
        "channel_id": post.channel_id,
        "channel": (channel.title or channel.username) if channel else None,
        "username": username,
        "message_id": post.message_id,
        "url": post_url(username, post.message_id) if username else None,
        "posted_at": _iso(post.posted_at),
        "title": post.title,
        "text": post.text,
        "html": post.html,
        "links": links,
        "hh_links": hh_vacancy_urls(links, post.text),
        "is_vacancy": post.is_vacancy,
        "lang": post.lang,
        "status": post.status,
        "vacancy_id": post.vacancy_id,
        "vacancy_status": status,
        "vacancy_status_label": STATUS_LABELS.get(status, status) if status else None,
        "notified_at": _iso(post.notified_at),
        "seen_at": _iso(post.seen_at),
        "created_at": _iso(post.created_at),
    }


async def list_posts(
    session: AsyncSession,
    *,
    channel_id: int | None = None,
    only_new: bool = False,
    only_vacancies: bool = True,
    include_hidden: bool = False,
    q: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> dict[str, Any]:
    stmt = select(ChannelPost)
    if channel_id:
        stmt = stmt.where(ChannelPost.channel_id == channel_id)
    if only_new:
        stmt = stmt.where(ChannelPost.status == "new")
    elif not include_hidden:
        stmt = stmt.where(ChannelPost.status != "hidden")
    if only_vacancies:
        stmt = stmt.where(ChannelPost.is_vacancy.is_(True))
    stmt = stmt.order_by(desc(ChannelPost.posted_at), desc(ChannelPost.message_id)).limit(LIST_MAX)
    rows = list((await session.execute(stmt)).scalars().all())
    if q and q.strip():
        needle = norm(q)  # SQLite LIKE is case-insensitive for ASCII only; Cyrillic is matched here
        rows = [r for r in rows if needle in norm(r.text)]
    total = len(rows)
    per_page = max(1, min(per_page, 200))
    page = max(1, page)
    chunk = rows[(page - 1) * per_page : page * per_page]
    channels = {c.id: c for c in (await session.execute(select(TelegramChannel))).scalars().all()}
    vacancy_ids = [r.vacancy_id for r in chunk if r.vacancy_id]
    vacancies: dict[int, Vacancy] = {}
    if vacancy_ids:
        vacancies = {
            v.id: v for v in (await session.execute(select(Vacancy).where(Vacancy.id.in_(vacancy_ids)))).scalars()
        }
    return {
        "items": [post_row(r, channels.get(r.channel_id), vacancy=vacancies.get(r.vacancy_id or 0)) for r in chunk],
        "total": total,
        "page": page,
        "per_page": per_page,
        "new_count": await new_posts_count(session),
    }


async def get_post(session: AsyncSession, post_id: int) -> ChannelPost:
    row = await session.get(ChannelPost, post_id)
    if row is None:
        raise ChannelError("пост не найден")
    return row


async def set_post_status(session: AsyncSession, post: ChannelPost, status: str) -> ChannelPost:
    if status not in ("new", "seen", "hidden"):
        raise ChannelError("статус: new, seen или hidden")
    post.status = status
    if status != "new" and post.seen_at is None:
        post.seen_at = utcnow()
    await session.flush()
    return post


async def mark_seen(session: AsyncSession, *, ids: list[int] | None = None, channel_id: int | None = None) -> int:
    stmt = update(ChannelPost).where(ChannelPost.status == "new").values(status="seen", seen_at=utcnow())
    if ids:
        stmt = stmt.where(ChannelPost.id.in_(ids))
    if channel_id:
        stmt = stmt.where(ChannelPost.channel_id == channel_id)
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)


async def attach_vacancy(session: AsyncSession, post: ChannelPost) -> Vacancy | None:
    """Links the post to the panel vacancy its hh.ru link points to (after «в панель» or a later scan)."""
    try:
        links = [str(u) for u in json.loads(post.links_json or "[]")]
    except ValueError:
        links = []
    hh_urls = hh_vacancy_urls(links, post.text)
    vacancy_id = _first_vacancy_id(hh_urls, await _vacancy_ids_by_hh(session, hh_urls))
    if vacancy_id is None:
        return None
    post.vacancy_id = vacancy_id
    await session.flush()
    return await session.get(Vacancy, vacancy_id)


async def new_posts_text(session: AsyncSession, *, limit: int = 10) -> str:
    """Bot command /channels: the newest unseen vacancy posts."""
    from app.services.notify import esc, link

    listing = await list_posts(session, only_new=True, only_vacancies=True, per_page=limit)
    items = listing["items"]
    if not items:
        return "📣 Новых вакансий в Telegram-каналах нет."
    lines = [f"{i}. {link(it['url'], it['title'])} — @{esc(it['username'])}" for i, it in enumerate(items, 1)]
    more = f"\n… и ещё {listing['total'] - len(items)}" if listing["total"] > len(items) else ""
    return f"📣 <b>Новые вакансии в каналах: {listing['total']}</b>\n" + "\n".join(lines) + more
