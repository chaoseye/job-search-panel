"""Studios: development and design studios the user writes to — found on hh.ru, among
the employers of the panel's vacancies, in the user's own list and in Telegram channel posts; contacts from the
studio's site (services/studio_contacts.py); a personal letter per studio written by Claude through the packet
exchange (task `studio_letter`); the user sends it from their own mailbox and marks it sent, replies are caught by
the mail sync.

hh.ru data may serve job search only (developer agreement §3.3 «поиск работы, сотрудников», §4.5, §4.7 — no
advertising mailings): a studio with hh data gets an initiative job application («application»), never a sales
offer. A studio from the user's list or a channel may get either.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import Studio, StudioLetter, StudioLetterRequest, Vacancy
from app.exchange.schemas import PITCH_STYLES
from app.services.audit import audit
from app.services.preferences import Preferences, get_preferences
from app.services.state import get_setting, set_setting
from app.services.textmatch import contains_term, norm

log = structlog.get_logger("studios")

SETTINGS_KEY = "studios_settings"
DEFAULT_KEYWORDS = [
    "веб-студия",
    "студия разработки",
    "digital-агентство",
    "digital-студия",
    "дизайн-студия",
    "студия дизайна",
    "разработка сайтов",
    "разработка мобильных приложений",
    "аутсорс разработки",
    "продуктовая студия",
    "брендинговое агентство",
]
DEFAULT_SETTINGS: dict[str, Any] = {
    "keywords": DEFAULT_KEYWORDS,
    "only_with_vacancies": True,
    "signature": "",
    "offer_notes": "",
}

STATUS_LABELS = {
    "new": "контакты не проверены",
    "ready": "есть контакты",
    "no_contacts": "контактов не нашлось",
    "drafting": "письмо у Claude",
    "drafted": "письмо готово",
    "sent": "отправлено",
    "replied": "ответили",
    "declined": "отказ",
    "skipped": "пропущена",
    "hidden": "скрыта",
}
# the tabs of the page
GROUPS: dict[str, list[str] | None] = {
    "work": ["new", "ready", "no_contacts", "drafting", "drafted"],
    "ready": ["ready"],
    "drafted": ["drafted"],
    "sent": ["sent"],
    "replied": ["replied", "declined"],
    "closed": ["skipped", "hidden"],
    "all": None,
}
KIND_LABELS = {"dev": "разработка", "design": "дизайн", "mixed": "разработка и дизайн", "unknown": "—"}
SOURCE_LABELS = {
    "hh_search": "hh.ru: поиск",
    "hh_vacancies": "hh.ru: вакансии в базе",
    "list": "мой список",
    "channel": "Telegram-канал",
}
HH_SOURCES = ("hh_search", "hh_vacancies")
LETTER_KINDS = ("application", "offer")
# Claude writes a draft per style, the user picks one; the first one is current after
# the import. «business» — facts with figures, 600–1100 characters; «simple» — short and plain, as to a person
LETTER_STYLES = PITCH_STYLES
STYLE_LABELS = {"business": "деловой", "simple": "простой"}
SENT_VIA = ("email", "telegram", "form", "other")
# statuses a studio can get a new letter in
LETTER_STATUSES = ("new", "ready", "no_contacts", "drafted")

# what the studio does, from its name, description and site text → tags shown in the list and given to Claude
SERVICES: dict[str, tuple[str, ...]] = {
    "веб-разработка": ("разработка сайтов", "веб-разработ", "web-разработ", "web development", "создание сайтов"),
    "мобильные приложения": ("мобильн", "ios", "android", "flutter", "react native"),
    "дизайн и UX/UI": ("дизайн", "design", "ux", "ui/ux", "интерфейс"),
    "брендинг": ("брендинг", "branding", "фирменный стиль", "айдентик"),
    "AI и автоматизация": (
        "искусственн интеллект",
        "нейросет",
        "машинн обучен",
        "llm",
        "gpt",
        "чат-бот",
        "chatbot",
        "ai",
    ),
    "e-commerce": ("интернет-магазин", "e-commerce", "ecommerce", "маркетплейс"),
    "маркетинг и SEO": ("seo", "продвижени", "маркетинг", "контекстн", "smm"),
    "1С и Битрикс": ("1с", "битрикс", "bitrix"),
    "CRM и интеграции": ("crm", "интеграци", "amocrm", "автоматизац"),
    "продуктовая разработка": ("mvp", "продуктов", "стартап", "saas"),
}
DESIGN_MARKERS = ("дизайн", "design", "брендинг", "branding", "ux", "креатив", "creative", "айдентик")
DEV_MARKERS = ("разработ", "develop", "программ", "веб-студ", "web studio", "приложени", "сайт", "digital", "it-")
# hh.ru industries of development companies (the /industries dictionary): system integration and IT consulting,
# software development, internet companies
DEV_INDUSTRIES = frozenset({"7.539", "7.540", "7.541"})
# a service company says what it is — «веб-студия», «студия дизайна», «digital-агентство», «аутсорс разработки»,
# «разработка сайтов под ключ». Single words («digital», «кейсы», «заказчик») are in every other vacancy post and
# «digital assets» is a crypto company: as single words they make dozens of channel posts «studios», nearly all noise
STUDIO_PHRASES = re.compile(
    r"(?:веб|web|digital|диджитал|дизайн|design|креативн\w*|creative|брендингов\w*|branding|ит|it|продуктов\w*"
    r"|product|мобильн\w*|интернет|ux|ui)[- ]?(?:студи\w*|studio|агентств\w*|agency|бюро)"
    r"|(?:студи\w*|studio|агентств\w*|agency)\s+(?:разработк\w*|дизайна|веб|web|digital|интернет\w*|мобильн\w*"
    r"|брендинга|design|development|продуктов\w*)"
    r"|аутсорс\w*\s+(?:разработк\w*|it|ит)|outsourc\w*\s+(?:development|software)"
    r"|software\s+(?:development\s+)?(?:company|house|agency)"
    r"|(?:разработк\w*|создани\w*)\s+(?:сайтов|приложений|мобильных приложений|по|продуктов)\s+"
    r"(?:на заказ|под ключ|для клиентов|для бизнеса)",
    re.IGNORECASE,
)
# a company that calls itself so by name: «… Studio», «Агентство …» (the industries of an hh card exclude beauty salons
# and fitness studios; channel posts have no industries, so there the phrase is required)
STUDIO_NAME = re.compile(r"(?<![a-zа-я])(?:студи\w*|studio|агентств\w*|agency|digital)(?![a-zа-я])", re.IGNORECASE)

# Studios are looked for digital work (sites and apps, automation, product work), so a studio is kept only when it
# does digital work. «Студия дизайна» on hh.ru is mostly an interior studio: interiors, renovation, curtains, decor,
# printing on hoodies, industrial design. A text about such work
# (OFFLINE_WORK) needs a strong sign of digital work (STRONG_DIGITAL); any other text — any sign (DIGITAL_WORK) or a
# development industry of the hh card. The patterns run on norm() text; a word starts and ends as in textmatch.
_W0, _W1 = r"(?<![a-zа-я0-9])", r"(?![a-zа-я0-9])"
STRONG_DIGITAL = re.compile(
    _W0 + r"(?:(?:веб|web)[- ]?(?:студи|разработ|дизайн|design|develop|сервис|приложени|систем|сайт|проект)\w*"
    r"|(?:digital|диджитал|it|ит|интернет)[- ](?:агентств|студи|компани|решени|продукт|маркетинг|проект)\w*"
    # «разработка сайтов», «создаём мобильные приложения»; not «разработка дизайн-проекта по …»: no bare «по»
    r"|(?:разработк|разрабатыва|создани|создае|делае)\w* (?:[a-zа-я0-9-]+ ){0,2}(?:сайт|интернет-магазин|лендинг"
    r"|приложени|программ|софт|сервис|бот|чат-бот|crm|игр|интерфейс|цифров|веб|web)\w*"
    r"|мобильн\w* приложени\w*|программн\w* обеспечени\w*|software|saas|ux/ui|ui/ux|дизайн\w* интерфейс\w*)" + _W1
)
DIGITAL_WORK = re.compile(
    _W0 + r"(?:(?:веб|web)(?!инар)[a-zа-я0-9-]*|digital\w*|диджитал\w*|цифров\w*|it|ит|айти|it-\w+|ит-\w+"
    r"|сайт(?:ы|ов|ами|ах)|лендинг\w*|интернет-\w+|e-?commerce|маркетплейс\w*"
    r"|ios|android|crm|erp|api|1с|1c|битрикс\w*|bitrix\w*|amocrm|saas|mvp|программн\w*|software|develop\w*"
    r"|frontend|backend|fullstack|python|php|javascript|react|wordpress"
    r"|ux|ui|интерфейс\w*|юзабилити|usability|figma"
    r"|брендинг\w*|branding|айдентик\w*|логотип\w*|фирменн\w* стил\w*|графическ\w* дизайн\w*|моушн\w*|motion"
    r"|seo|smm|таргет\w*|контекстн\w* реклам\w*|маркетинг\w*|маркетингов\w*|копирайтинг\w*"
    r"|креативн\w* (?:агентств|студи)\w*|рекламн\w* (?:агентств|креатив)\w*|видеопроизводств\w*|видеопродакшн\w*"
    r"|автоматизаци\w*|интеграци\w*|чат-бот\w*|бот|боты|ботов|нейросет\w*|искусственн\w* интеллект\w*|ai|ии|llm|gpt"
    r"|геймдев|gamedev)" + _W1
)
OFFLINE_WORK = re.compile(
    _W0 + r"(?:интерьер\w*|ремонт\w*|отделк\w*|дизайн-проект\w*|архитектур\w*|ландшафт\w*|мебел\w*|текстил\w*"
    r"|штор\w*|декор\w*|квартир\w*|коттедж\w*|загородн\w*|стабилизированн\w*|озеленени\w*|флорист\w*"
    r"|толстовк\w*|свитшот\w*|футболк\w*|вышивк\w*|шелкограф\w*|сублимаци\w*|промышленн\w* дизайн\w*)" + _W1
)


# ---- settings --------------------------------------------------------------------------------------------------------


async def studio_settings(session: AsyncSession) -> dict[str, Any]:
    saved = await get_setting(session, SETTINGS_KEY)
    out = dict(DEFAULT_SETTINGS)
    if isinstance(saved, dict):
        for key, default in DEFAULT_SETTINGS.items():
            value = saved.get(key)
            if isinstance(default, list) and isinstance(value, list):
                out[key] = [str(v).strip() for v in value if str(v).strip()][:30]
            elif isinstance(default, bool) and isinstance(value, bool):
                out[key] = value
            elif isinstance(default, str) and isinstance(value, str):
                out[key] = value.strip()[:3000]
    return out


async def save_studio_settings(session: AsyncSession, values: dict[str, Any]) -> dict[str, Any]:
    current = await studio_settings(session)
    current.update({k: v for k, v in values.items() if k in DEFAULT_SETTINGS and v is not None})
    await set_setting(session, SETTINGS_KEY, current)
    return await studio_settings(session)


# ---- small helpers ---------------------------------------------------------------------------------------------------


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _iso(dt: datetime | None) -> str | None:
    value = as_utc(dt)
    return value.isoformat(timespec="seconds") if value else None


def site_parts(url: str | None) -> tuple[str | None, str | None]:
    """(canonical site url, domain without www) of a site address as people write it: «studio.example», «https://www.x.example/»."""
    raw = (url or "").strip().strip("<>\"'")
    # an e-mail («x@y.example», «mailto:…») or another scheme without «//» (tel:, skype:) is not a site
    if not raw or "@" in raw or re.match(r"^[a-z][a-z0-9+.-]*:(?!//)", raw, re.IGNORECASE):
        return None, None
    if not re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.IGNORECASE):
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        return None, None
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or "." not in host or " " in host:
        return None, None
    domain = host[4:] if host.startswith("www.") else host
    return f"{parts.scheme}://{host}", domain


# Addresses that are not a studio's own site: social networks, job boards and applicant tracking systems, link hubs
# and shorteners, forms, media. hh.ru cards carry them in `site_url` too (vk.com, instagram.com), and the domain is
# the dedupe key of studios: two employers whose «site» is a social network would become one studio.
# Public mailboxes (PUBLIC_MAIL_DOMAINS) are no site either.
NOT_A_SITE = (
    "t.me",
    "telegram.me",
    "telegram.org",
    "vk.com",
    "vk.ru",
    "vkvideo.ru",
    "ok.ru",
    "max.ru",
    "dzen.ru",
    "rutube.ru",
    "hh.ru",
    "headhunter.ru",
    "docs.google.com",
    "forms.gle",
    "forms.yandex.ru",
    "notion.so",
    "notion.site",
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "tiktok.com",
    "pinterest.com",
    "linkedin.com",
    "habr.com",
    "career.habr.com",
    "github.com",
    "bit.ly",
    "clck.ru",
    # job boards and applicant tracking systems: a vacancy page, not the studio
    "typeform.com",
    "careers-page.com",
    "lever.co",
    "ashbyhq.com",
    "greenhouse.io",
    "workable.com",
    "smartrecruiters.com",
    "bamboohr.com",
    "recruitee.com",
    "teamtailor.com",
    "personio.de",
    "huntflow.ru",
    "potok.io",
    "getmatch.ru",
    "geekjob.ru",
    "superjob.ru",
    "rabota.ru",
    "zarplata.ru",
    "trudvsem.ru",
    "avito.ru",
    "hh.kz",
    "rabota.by",
    "careerlaboratory.ru",
    "facancy.ru",
    "cryptojobslist.com",
    "cjl.ist",
    "hashtagweb3.com",
    # social networks, media, link hubs, forms and files
    "lnkd.in",
    "x.com",
    "twitter.com",
    "facebook.com",
    "whatsapp.com",
    "wa.me",
    "viber.com",
    "vc.ru",
    "dtf.ru",
    "tproger.ru",
    "luma.com",
    "mail.ru",
    "yandex.ru",
    "google.com",
    "forms.office.com",
    "airtable.com",
    "calendly.com",
    "zoom.us",
    "behance.net",
    "dribbble.com",
    "taplink.cc",
    "linktr.ee",
    "vk.cc",
    "t.co",
    "goo.gl",
    "tinyurl.com",
)
# a studio's page on one of these is kept as its contact page when it has no site of its own
SOCIAL_PAGES = (
    "vk.com",
    "vk.ru",
    "ok.ru",
    "t.me",
    "telegram.me",
    "instagram.com",
    "facebook.com",
    "dzen.ru",
    "youtube.com",
    "behance.net",
    "dribbble.com",
    "taplink.cc",
    "linktr.ee",
)
# a careers subdomain belongs to the company: careers.studio.example → studio.example
CAREER_SUBDOMAINS = ("careers.", "career.", "jobs.", "job.", "hr.")


def _under(domain: str, parents: tuple[str, ...] | set[str]) -> bool:
    return any(domain == p or domain.endswith("." + p) for p in parents)


def foreign_site(domain: str | None) -> bool:
    """A social network, a job board, a public mailbox … — not a studio's own site."""
    return bool(domain) and (_under(str(domain), NOT_A_SITE) or _under(str(domain), PUBLIC_MAIL_DOMAINS))


def studio_site(url: str | None) -> tuple[str | None, str | None]:
    """site_parts() of a studio's own site: a careers subdomain gives the company's site (careers.studio.example →
    studio.example), a foreign_site() gives (None, None)."""
    site, domain = site_parts(url)
    if site is None or domain is None or foreign_site(domain):
        return None, None
    for prefix in CAREER_SUBDOMAINS:
        if domain.startswith(prefix) and domain.count(".") >= 2:
            domain = domain[len(prefix) :]
            site = f"{urlsplit(site).scheme}://{domain}"
            break
    return site, domain


def social_page(url: str | None) -> str | None:
    """The link itself when it is a page of the studio in a social network (SOCIAL_PAGES), else None."""
    _, domain = site_parts(url)
    if domain is None or not _under(domain, SOCIAL_PAGES):
        return None
    raw = (url or "").strip().strip("<>\"'")
    return (raw if re.match(r"^https?://", raw, re.IGNORECASE) else "https://" + raw)[:500]


def digital_profile(text: str, industries: list[str] | None = None) -> bool:
    """Whether the studio does digital work, the only kind the user's competences are for: see OFFLINE_WORK."""
    t = norm(text)
    strong = STRONG_DIGITAL.search(t) is not None
    if OFFLINE_WORK.search(t):
        return strong
    return strong or DIGITAL_WORK.search(t) is not None or bool(DEV_INDUSTRIES & set(industries or []))


def detect_services(text: str) -> list[str]:
    t = norm(text)
    return [label for label, words in SERVICES.items() if any(contains_term(t, w) for w in words)]


def studio_kind(text: str, industries: list[str] | None = None) -> str:
    t = norm(text)
    design = any(contains_term(t, w) for w in DESIGN_MARKERS) or "11.459" in (industries or [])
    dev = any(contains_term(t, w) for w in DEV_MARKERS) or bool(DEV_INDUSTRIES & set(industries or []))
    if design and dev:
        return "mixed"
    return "design" if design else "dev" if dev else "unknown"


def looks_like_studio(text: str) -> bool:
    """The text calls the company a studio or a development / design agency (STUDIO_PHRASES)."""
    return STUDIO_PHRASES.search(norm(text)) is not None


def studio_by_name(name: str) -> bool:
    return STUDIO_NAME.search(norm(name)) is not None


# ---- upsert ----------------------------------------------------------------------------------------------------------


async def upsert_studio(
    session: AsyncSession,
    *,
    name: str,
    source: str,
    site_url: str | None = None,
    hh_employer_id: str | None = None,
    area: str | None = None,
    description: str | None = None,
    open_vacancies: int | None = None,
    industries: list[str] | None = None,
    letter_kind: str | None = None,
    email: str | None = None,
    now: datetime | None = None,
) -> tuple[Studio, bool]:
    """Finds the studio by its hh id, its own site's domain or — for a studio without a site — an address of it
    (else creates it) and merges what the source knows. A social network in `site_url` is no site (studio_site):
    it becomes the contact page. A studio that got hh data keeps «application» as its letter kind.
    Returns (studio, created)."""
    now = now or utcnow()
    site, domain = studio_site(site_url)
    row: Studio | None = None
    if hh_employer_id:
        row = (
            await session.execute(select(Studio).where(Studio.hh_employer_id == hh_employer_id))
        ).scalar_one_or_none()
    if row is None and domain:
        row = (await session.execute(select(Studio).where(Studio.domain == domain))).scalar_one_or_none()
    if row is None and email and not domain:
        candidates = (
            await session.execute(select(Studio).where(Studio.domain.is_(None), Studio.emails_json.like(f"%{email}%")))
        ).scalars()
        row = next((c for c in candidates if email in _loads(c.emails_json, [])), None)
    created = row is None
    if row is None:
        row = Studio(name=name.strip()[:300] or (domain or email or "студия"), source=source, first_seen_at=now)
        session.add(row)
    if hh_employer_id and not row.hh_employer_id:
        row.hh_employer_id = hh_employer_id
    if domain and not row.domain:
        clash = (await session.execute(select(Studio.id).where(Studio.domain == domain))).scalar_one_or_none()
        if clash is None or clash == row.id:
            row.domain, row.site_url = domain, site
    if domain is None and not row.contact_url:
        row.contact_url = social_page(site_url)
    row.area = row.area or (area or None)
    if description and len(description) > len(row.description or ""):
        row.description = description[:3000]
    if open_vacancies is not None:
        row.open_vacancies = open_vacancies
    sources = _loads(row.sources_json, [])
    if source not in sources:
        sources.append(source)
    row.sources_json = _json(sources)
    text = " ".join(x for x in (row.name, row.description or "", row.site_summary or "") if x)
    kind = studio_kind(text, industries)
    if kind != "unknown" or row.kind in (None, "unknown"):
        row.kind = kind
    row.services_json = _json(detect_services(text))
    if row.hh_employer_id or any(s in HH_SOURCES for s in sources):
        row.letter_kind = "application"
    elif created and letter_kind in LETTER_KINDS:
        row.letter_kind = letter_kind
    row.last_seen_at = now
    await session.flush()
    return row, created


def has_hh_data(row: Studio) -> bool:
    return bool(row.hh_employer_id) or any(s in HH_SOURCES for s in _loads(row.sources_json, []))


# ---- fit -------------------------------------------------------------------------------------------------------------

# a vacancy that passed the rules or was evaluated: the employer hires people like the user
RELEVANT_STATUSES = (
    "prescored",
    "awaiting_claude",
    "evaluated",
    "suitable",
    "maybe",
    "needs_manual",
    "queued",
    "sent",
    "already_applied",
)


async def relevant_vacancies(session: AsyncSession, row: Studio, limit: int = 5) -> list[dict[str, Any]]:
    if not row.hh_employer_id:
        return []
    rows = (
        await session.execute(
            select(Vacancy.id, Vacancy.name, Vacancy.pipeline_status, Vacancy.score)
            .where(Vacancy.employer_id == row.hh_employer_id, Vacancy.pipeline_status.in_(RELEVANT_STATUSES))
            .order_by(desc(func.coalesce(Vacancy.score, Vacancy.prescore, 0)))
            .limit(limit)
        )
    ).all()
    return [{"id": r.id, "name": r.name, "status": r.pipeline_status, "score": r.score} for r in rows]


def fit_score(row: Studio, prefs: Preferences, vacancies: list[dict[str, Any]]) -> tuple[int, list[str]]:
    """How much the studio needs what the user does: the questionnaire's skills (by weight) and target keywords found
    in what the studio says about itself, plus open vacancies and vacancies in the panel that suit the user."""
    text = norm(
        " ".join(
            [row.name, row.description or "", row.site_summary or "", " ".join(_loads(row.services_json, []))]
            + [str(v["name"]) for v in vacancies]
        )
    )
    points = 0
    matched: list[str] = []
    for skill in prefs.skills_weights:
        if skill.name.strip() and contains_term(text, skill.name):
            points += 4 * skill.weight
            matched.append(skill.name)
    for word in prefs.target_keywords:
        if word.strip() and word not in matched and contains_term(text, word):
            points += 8
            matched.append(word)
    points = min(points, 70)
    notes = [f"совпадает: {', '.join(matched[:8])}"] if matched else []
    if row.open_vacancies:
        points += 10
        notes.append(f"открытых вакансий на hh: {row.open_vacancies}")
    if vacancies:
        points += 20
        notes.append(f"подходящих вакансий в панели: {len(vacancies)}")
    return min(points, 100), notes


async def refresh_fit(session: AsyncSession, row: Studio, prefs: Preferences | None = None) -> None:
    prefs = prefs or await get_preferences(session)
    score, notes = fit_score(row, prefs, await relevant_vacancies(session, row))
    row.fit_score, row.fit_notes_json = score, _json(notes)


# ---- views -----------------------------------------------------------------------------------------------------------


def studio_row(row: Studio) -> dict[str, Any]:
    emails = _loads(row.emails_json, [])
    return {
        "id": row.id,
        "name": row.name,
        "site_url": row.site_url,
        "domain": row.domain,
        "hh_employer_id": row.hh_employer_id,
        "hh_url": f"https://hh.ru/employer/{row.hh_employer_id}" if row.hh_employer_id else None,
        "area": row.area,
        "kind": row.kind,
        "kind_label": KIND_LABELS.get(row.kind, row.kind),
        "source": row.source,
        "sources": _loads(row.sources_json, []),
        "letter_kind": row.letter_kind,
        "offer_allowed": not has_hh_data(row),
        "open_vacancies": row.open_vacancies,
        "services": _loads(row.services_json, []),
        "emails": emails,
        "telegram": row.telegram,
        "contact_url": row.contact_url,
        "contacts_checked_at": _iso(row.contacts_checked_at),
        "contacts_error": row.contacts_error,
        "fit_score": row.fit_score,
        "fit_notes": _loads(row.fit_notes_json, []),
        "status": row.status,
        "status_label": STATUS_LABELS.get(row.status, row.status),
        "sent_at": _iso(row.sent_at),
        "sent_to": row.sent_to,
        "sent_via": row.sent_via,
        "replied_at": _iso(row.replied_at),
        "reply_subject": row.reply_subject,
        "first_seen_at": _iso(row.first_seen_at),
        "notes": row.notes,
    }


def letter_row(letter: StudioLetter, signature: str = "") -> dict[str, Any]:
    return {
        "id": letter.id,
        "version": letter.version,
        "kind": letter.kind,
        "style": letter.style,
        "style_label": STYLE_LABELS.get(letter.style, letter.style),
        "subject": letter.subject,
        "text": letter.text,
        "full_text": with_signature(letter.text, signature),
        "origin": letter.origin,
        "batch_id": letter.batch_id,
        "needs_clarification": _loads(letter.needs_clarification_json, []),
        "warnings": _loads(letter.warnings_json, []),
        "is_current": letter.is_current,
        "created_at": _iso(letter.created_at),
    }


def with_signature(text: str, signature: str) -> str:
    signature = (signature or "").strip()
    return f"{text.rstrip()}\n\n{signature}" if signature else text


async def current_letter(session: AsyncSession, studio_id: int) -> StudioLetter | None:
    return (
        await session.execute(
            select(StudioLetter)
            .where(StudioLetter.studio_id == studio_id, StudioLetter.is_current.is_(True))
            .order_by(desc(StudioLetter.version))
            .limit(1)
        )
    ).scalar_one_or_none()


def _variants(letters: Sequence[StudioLetter]) -> list[StudioLetter]:
    """The latest version of each style (`letters` go from the newest), known styles first."""
    latest: dict[str, StudioLetter] = {}
    for letter in letters:
        latest.setdefault(letter.style, letter)
    order = {style: n for n, style in enumerate(LETTER_STYLES)}
    return sorted(latest.values(), key=lambda x: order.get(x.style, len(order)))


async def studio_detail(session: AsyncSession, row: Studio) -> dict[str, Any]:
    settings = await studio_settings(session)
    letters = (
        (
            await session.execute(
                select(StudioLetter).where(StudioLetter.studio_id == row.id).order_by(desc(StudioLetter.version))
            )
        )
        .scalars()
        .all()
    )
    request = (
        await session.execute(
            select(StudioLetterRequest)
            .where(StudioLetterRequest.studio_id == row.id)
            .order_by(desc(StudioLetterRequest.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    data = studio_row(row)
    current = next((x for x in letters if x.is_current), None)
    data.update(
        {
            "description": row.description,
            "site_summary": row.site_summary,
            "letter": letter_row(current, settings["signature"]) if current else None,
            # the latest version of each style, in LETTER_STYLES order: the drafts the user picks from
            "variants": [letter_row(x, settings["signature"]) for x in _variants(letters)],
            "letters": [letter_row(x, settings["signature"]) for x in letters],
            "request": None
            if request is None
            else {
                "id": request.id,
                "status": request.status,
                "batch_id": request.batch_id,
                "error": request.error,
                "kind": request.kind,
            },
            "vacancies": await relevant_vacancies(session, row, limit=10),
        }
    )
    return data


async def list_studios(
    session: AsyncSession,
    *,
    group: str = "work",
    source: str | None = None,
    kind: str | None = None,
    q: str | None = None,
    with_contacts: bool = False,
    sort: str = "fit",
    page: int = 0,
    per_page: int = 50,
) -> dict[str, Any]:
    stmt = select(Studio)
    statuses = GROUPS.get(group, GROUPS["work"])
    if statuses is not None:
        stmt = stmt.where(Studio.status.in_(statuses))
    if source:
        stmt = stmt.where(Studio.sources_json.like(f'%"{source}"%'))
    if kind:
        stmt = stmt.where(Studio.kind == kind)
    if with_contacts:
        stmt = stmt.where(or_(Studio.emails_json.like('%"%@%'), Studio.telegram.isnot(None)))
    rows = list((await session.execute(stmt)).scalars().all())
    if q and q.strip():
        needle = norm(q)
        rows = [r for r in rows if needle in norm(" ".join([r.name, r.domain or "", r.description or ""]))]
    if sort == "name":
        rows.sort(key=lambda r: norm(r.name))
    elif sort == "recent":
        rows.sort(key=lambda r: (as_utc(r.first_seen_at) or utcnow(), r.id), reverse=True)
    else:
        rows.sort(key=lambda r: (r.fit_score or 0, r.id), reverse=True)
    total = len(rows)
    per_page = max(1, min(per_page, 200))
    chunk = rows[page * per_page : (page + 1) * per_page]
    return {
        "items": [studio_row(r) for r in chunk],
        "total": total,
        "page": page,
        "per_page": per_page,
        "counts": await status_counts(session),
    }


async def status_counts(session: AsyncSession) -> dict[str, Any]:
    rows = (await session.execute(select(Studio.status, func.count(Studio.id)).group_by(Studio.status))).all()
    by_status = {str(s): int(n) for s, n in rows}
    counts: dict[str, Any] = {"by_status": by_status}
    for name, statuses in GROUPS.items():
        counts[name] = sum(by_status.values()) if statuses is None else sum(by_status.get(s, 0) for s in statuses)
    return counts


# ---- actions ---------------------------------------------------------------------------------------------------------


class StudioError(Exception):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def contact_status(row: Studio) -> str:
    """Where a studio without a letter stands: its contacts decide."""
    if row.contacts_checked_at is None:
        return "new"
    return "ready" if _loads(row.emails_json, []) or row.telegram else "no_contacts"


async def set_status(session: AsyncSession, row: Studio, action: str) -> None:
    """skip | hide | restore | replied | declined."""
    if action in ("skip", "hide"):
        row.status = "skipped" if action == "skip" else "hidden"
    elif action == "restore":
        if await current_letter(session, row.id) is not None:
            row.status = "sent" if row.sent_at else "drafted"
        else:
            row.status = contact_status(row)
    elif action == "replied":
        if row.status not in ("sent", "replied", "declined"):
            raise StudioError("Ответ отмечают у студии, которой письмо уже отправлено")
        row.status, row.replied_at = "replied", row.replied_at or utcnow()
    elif action == "declined":
        row.status = "declined"
    else:
        raise StudioError(f"неизвестное действие: {action}", 422)
    await audit(
        session, "studios", f"Студия «{row.name}»: {STATUS_LABELS.get(row.status, row.status)}", studio_id=row.id
    )


async def update_studio(
    session: AsyncSession,
    row: Studio,
    *,
    emails: list[str] | None = None,
    telegram: str | None = None,
    notes: str | None = None,
    letter_kind: str | None = None,
    site_url: str | None = None,
) -> None:
    if letter_kind is not None:
        if letter_kind not in LETTER_KINDS:
            raise StudioError("вид письма: application | offer", 422)
        if letter_kind == "offer" and has_hh_data(row):
            raise StudioError(
                "Студия найдена через hh.ru: по соглашению hh её данные можно использовать только для поиска работы, "
                "поэтому ей — инициативный отклик, не коммерческое предложение"
            )
        row.letter_kind = letter_kind
    if emails is not None:
        clean = [e.strip().lower() for e in emails if EMAIL_RE.fullmatch(e.strip())]
        row.emails_json = _json(list(dict.fromkeys(clean))[:5])
    if telegram is not None:
        row.telegram = telegram.strip()[:100] or None
    if notes is not None:
        row.notes = notes.strip()[:2000] or None
    if site_url is not None:
        site, domain = studio_site(site_url)
        if site_url.strip() and domain is None:
            if foreign_site(site_parts(site_url)[1]):
                raise StudioError(
                    "это соцсеть, доска вакансий или почтовый сервис, а не сайт студии — "
                    "ссылку на страницу можно оставить в заметках",
                    422,
                )
            raise StudioError("не похоже на адрес сайта", 422)
        if domain and domain != row.domain:
            clash = (await session.execute(select(Studio.id).where(Studio.domain == domain))).scalar_one_or_none()
            if clash is not None and clash != row.id:
                raise StudioError("студия с этим сайтом уже есть в списке")
            row.site_url, row.domain = site, domain
            row.contacts_checked_at = None
    if row.status in ("new", "ready", "no_contacts") and (emails is not None or telegram is not None):
        # a contact typed in by hand counts like one found on the site
        has_contacts = bool(row.telegram or _loads(row.emails_json, []))
        row.status = "ready" if has_contacts else ("no_contacts" if row.contacts_checked_at else "new")
    await session.flush()


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}")


async def _clear_current(session: AsyncSession, studio_id: int) -> None:
    for old in (
        await session.execute(
            select(StudioLetter).where(StudioLetter.studio_id == studio_id, StudioLetter.is_current.is_(True))
        )
    ).scalars():
        old.is_current = False


async def save_letter(
    session: AsyncSession, row: Studio, subject: str, text: str, style: str | None = None
) -> StudioLetter:
    """The user's edit of the draft in `style` (default: the current one's) becomes a new current version."""
    subject, text = " ".join(subject.split())[:200], text.replace("\r\n", "\n").strip()
    if not subject or not text:
        raise StudioError("нужны тема и текст письма", 422)
    if style is not None and style not in LETTER_STYLES:
        raise StudioError(f"стиль письма: {' | '.join(LETTER_STYLES)}", 422)
    previous = await current_letter(session, row.id)
    style = style or (previous.style if previous else LETTER_STYLES[0])
    same_style = (
        await session.execute(
            select(StudioLetter)
            .where(StudioLetter.studio_id == row.id, StudioLetter.style == style)
            .order_by(desc(StudioLetter.version))
            .limit(1)
        )
    ).scalar_one_or_none()
    version = int(
        (await session.execute(select(func.max(StudioLetter.version)).where(StudioLetter.studio_id == row.id))).scalar()
        or 0
    )
    await _clear_current(session, row.id)
    base = same_style or previous
    letter = StudioLetter(
        studio_id=row.id,
        version=version + 1,
        kind=base.kind if base else row.letter_kind,
        style=style,
        subject=subject,
        text=text,
        origin="edited",
        is_current=True,
    )
    session.add(letter)
    if row.status in ("new", "ready", "no_contacts"):
        row.status = "drafted"
    await session.flush()
    return letter


def local_day_start(settings: Settings, now: datetime | None = None) -> datetime:
    local = (now or utcnow()).astimezone(ZoneInfo(settings.timezone))
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


async def sent_today(session: AsyncSession, settings: Settings, now: datetime | None = None) -> int:
    # SQLite keeps the UTC wall time without the zone and binds a parameter's own wall time: the local midnight
    # must be turned to UTC first, or the letters of 00:00–03:00 Moscow time fall out of «today»
    start = local_day_start(settings, now).astimezone(UTC)
    return int((await session.execute(select(func.count(Studio.id)).where(Studio.sent_at >= start))).scalar() or 0)


async def mark_sent(
    session: AsyncSession,
    settings: Settings,
    row: Studio,
    *,
    to: str,
    via: str,
    letter_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The user sent the letter from their own mailbox (or messenger): `letter_id` — the draft they sent (it becomes
    the current one), else the current one. Returns today's count and the soft limit."""
    if via not in SENT_VIA:
        raise StudioError("способ отправки: email | telegram | form | other", 422)
    if row.status in ("sent", "replied", "declined"):
        raise StudioError("Этой студии письмо уже отправлено")
    if letter_id is not None:
        chosen = await session.get(StudioLetter, letter_id)
        if chosen is None or chosen.studio_id != row.id:
            raise StudioError("такого письма у этой студии нет", 404)
        if not chosen.is_current:
            await _clear_current(session, row.id)
            chosen.is_current = True
            await session.flush()
    if await current_letter(session, row.id) is None:
        raise StudioError("Письма ещё нет — сначала подготовьте его")
    now = now or utcnow()
    row.status, row.sent_at, row.sent_to, row.sent_via = "sent", now, to.strip()[:200] or None, via
    await audit(
        session, "studios", f"Письмо студии «{row.name}» отправлено ({via}: {row.sent_to or '—'})", studio_id=row.id
    )
    await session.flush()
    count = await sent_today(session, settings, now)
    return {
        "sent_today": count,
        "daily_limit": settings.studios_daily_limit,
        "over_limit": count > settings.studios_daily_limit,
    }


# ---- letters through Claude ------------------------------------------------------------------------------------------


async def request_letters(
    session: AsyncSession, settings: Settings, ids: list[int], notes: str | None = None
) -> tuple[int, list[str], str | None]:
    """Queues a `studio_letter` task per studio and tries to build a packet right away.
    Returns (queued, skipped reasons, first packet id or None)."""
    from app.exchange.packets import PacketError, build_batches

    queued = 0
    skipped: list[str] = []
    for studio_id in dict.fromkeys(ids):
        row = await session.get(Studio, studio_id)
        if row is None:
            skipped.append(f"#{studio_id}: не найдена")
            continue
        if row.status not in LETTER_STATUSES:
            skipped.append(f"«{row.name}»: {STATUS_LABELS.get(row.status, row.status)}")
            continue
        for old in (
            await session.execute(
                select(StudioLetterRequest).where(
                    StudioLetterRequest.studio_id == row.id, StudioLetterRequest.status == "queued"
                )
            )
        ).scalars():
            old.status, old.error = "cancelled", "заменён новым запросом"
        kind = "application" if has_hh_data(row) else row.letter_kind
        session.add(StudioLetterRequest(studio_id=row.id, kind=kind, user_notes=(notes or "").strip() or None))
        row.status = "drafting"
        queued += 1
    await session.flush()
    if queued:
        await audit(session, "studios", f"Запрошены письма студиям через Claude: {queued}")
    batch_id: str | None = None
    if queued:
        try:
            created = await build_batches(session, settings, vacancy_ids=[], include_revisions=True)
            batch_id = created[0].batch_id if created else None
        except PacketError as exc:
            skipped.append(str(exc))
    return queued, skipped, batch_id


async def finish_letter(
    session: AsyncSession,
    request: StudioLetterRequest,
    *,
    subject: str,
    text: str,
    batch_id: str,
    needs_clarification: list[str],
    warnings: list[str],
    style: str = LETTER_STYLES[0],
    current: bool = True,
) -> StudioLetter:
    """Importer: Claude's draft in `style` becomes a new version; `current` — the one shown first (the first style
    of the result), the other drafts wait for the user's pick."""
    row = await session.get(Studio, request.studio_id)
    version = int(
        (
            await session.execute(
                select(func.max(StudioLetter.version)).where(StudioLetter.studio_id == request.studio_id)
            )
        ).scalar()
        or 0
    )
    if current:
        await _clear_current(session, request.studio_id)
    letter = StudioLetter(
        studio_id=request.studio_id,
        version=version + 1,
        kind=request.kind,
        style=style,
        subject=subject,
        text=text,
        origin="claude",
        batch_id=batch_id,
        needs_clarification_json=_json(needs_clarification),
        warnings_json=_json(warnings),
        is_current=current,
    )
    session.add(letter)
    request.status, request.error = "done", None
    if row is not None and row.status == "drafting":
        row.status = "drafted"
    await session.flush()
    return letter


async def fail_letter(session: AsyncSession, request: StudioLetterRequest, error: str) -> None:
    """Importer: no letter — the request is closed with the reason, the studio goes back to where it was."""
    request.status, request.error = "cancelled", error[:500]
    row = await session.get(Studio, request.studio_id)
    if row is not None and row.status == "drafting":
        row.status = "drafted" if await current_letter(session, row.id) is not None else contact_status(row)
    await session.flush()


def packet_studio(row: Studio, vacancies: list[dict[str, Any]], settings: dict[str, Any], kind: str) -> dict[str, Any]:
    """What Claude gets about the studio (and, for an offer, the user's notes on formats and rates)."""
    data = {
        "name": row.name,
        "site": row.site_url,
        "area": row.area,
        "kind": KIND_LABELS.get(row.kind, row.kind),
        "services": _loads(row.services_json, []),
        "about": (row.description or "")[:1500],
        "site_summary": (row.site_summary or "")[:800],
        "open_vacancies": row.open_vacancies,
        "vacancies_in_panel": [v["name"] for v in vacancies],
        "found_via": [SOURCE_LABELS.get(s, s) for s in _loads(row.sources_json, [])],
    }
    if kind == "offer" and settings.get("offer_notes"):
        data["offer_notes"] = settings["offer_notes"]
    return data


# ---- the user's own list ---------------------------------------------------------------------------------------------


async def import_list(session: AsyncSession, text: str, letter_kind: str = "application") -> dict[str, Any]:
    """Lines «site», «name; site», «name; site; e-mail» (a comma or a tab works too) → studios from the list. The key
    of a studio is its own site; without one — the site of its e-mail domain, and for a public mailbox (gmail.com …)
    the address itself. A social network page is kept as the contact page, it is no site."""
    if letter_kind not in LETTER_KINDS:
        raise StudioError("вид письма: application | offer", 422)
    summary: dict[str, Any] = {"added": 0, "updated": 0, "errors": []}
    prefs = await get_preferences(session)
    for n, raw_line in enumerate(text.splitlines()[:500], start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in re.split(r"[;\t]|,(?=\s*\S+\.\S)", line) if p.strip()]
        links = [p for p in parts if site_parts(p)[1] and "@" not in p]
        site = next((p for p in links if studio_site(p)[1]), None)
        email = next((p.lower() for p in parts if EMAIL_RE.fullmatch(p)), None)
        name = next((p for p in parts if p not in (*links, email) and not EMAIL_RE.fullmatch(p)), None)
        if site is None and email is not None and studio_site(email.split("@", 1)[1])[1]:
            site = email.split("@", 1)[1]
        _, domain = studio_site(site)
        if domain is None and email is None:
            foreign = links[0] if links else None
            summary["errors"].append(
                f"строка {n}: {foreign} — не сайт студии (соцсеть или доска вакансий), добавьте сайт или почту"
                if foreign
                else f"строка {n}: нет адреса сайта"
            )
            continue
        row, created = await upsert_studio(
            session,
            name=name or domain or str(email),
            source="list",
            site_url=site or next(iter(links), None),
            letter_kind=letter_kind,
            email=email if domain is None else None,
        )
        if email:
            emails = _loads(row.emails_json, [])
            if email not in emails:
                row.emails_json = _json([email, *emails][:5])
            if row.status in ("new", "no_contacts"):
                row.status = "ready"
        await refresh_fit(session, row, prefs)
        summary["added" if created else "updated"] += 1
    if summary["added"] or summary["updated"]:
        await audit(
            session, "studios", f"Студии из списка: добавлено {summary['added']}, обновлено {summary['updated']}"
        )
    return summary


# ---- replies (mail sync) -----------------------------------------------------------------------------------------

# a letter to one of these domains is matched by the full address, not by «anyone from the domain»
PUBLIC_MAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "mail.ru",
    "bk.ru",
    "inbox.ru",
    "list.ru",
    "internet.ru",
    "yandex.ru",
    "ya.ru",
    "yandex.com",
    "rambler.ru",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "icloud.com",
    "me.com",
    "yahoo.com",
    "proton.me",
    "protonmail.com",
}


async def reply_targets(session: AsyncSession) -> list[tuple[int, str, datetime]]:
    """(studio id, IMAP FROM needle, sent at) for letters sent by e-mail that have no reply yet."""
    rows = (
        (
            await session.execute(
                select(Studio).where(Studio.status == "sent", Studio.sent_via == "email", Studio.sent_to.isnot(None))
            )
        )
        .scalars()
        .all()
    )
    out: list[tuple[int, str, datetime]] = []
    for row in rows:
        address = (row.sent_to or "").strip().lower()
        if not EMAIL_RE.fullmatch(address) or row.sent_at is None:
            continue
        domain = address.split("@", 1)[1]
        needle = address if domain in PUBLIC_MAIL_DOMAINS else f"@{domain}"
        out.append((row.id, needle, as_utc(row.sent_at) or utcnow()))
    return out


async def record_replies(
    session: AsyncSession, settings: Settings, replies: list[tuple[int, str, datetime | None]]
) -> int:
    """Mail sync found an answer from the studio: `replied` + the event `studio_reply` (feed and Telegram)."""
    from app.services.notify import emit_event

    recorded = 0
    for studio_id, subject, when in replies:
        row = await session.get(Studio, studio_id)
        if row is None or row.status != "sent":
            continue
        row.status, row.replied_at, row.reply_subject = "replied", when or utcnow(), (subject or "")[:300] or None
        await audit(session, "studios", f"Студия «{row.name}» ответила на письмо", studio_id=row.id)
        await emit_event(
            session, settings, "studio_reply", {"name": row.name, "subject": row.reply_subject, "site": row.site_url}
        )
        recorded += 1
    await session.flush()
    return recorded
