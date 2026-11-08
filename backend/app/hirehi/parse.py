"""Pages of hirehi.ru → the panel's data (checked on the live site 2026-11-08).

- A listing page (the root, a section hub /vacancies/<slug>, a city hub /<city>-jobs) carries its 51 newest vacancies
  as JSON in <script id="__SSR_JOBS__">: id, title, company, company_icon, created_at, format («удалённо по РФ»,
  «гибрид Москва», «офис Минск»), level, category, salary, plus total_count and has_more. The links of the cards
  (/<category>/<slug>-<id>) are in the <a class="job-card"> elements and in the JSON-LD ItemList. Filters go to the
  address (format=…, level=…, page=N); an unknown parameter or value is a 404.
- A vacancy card has JSON-LD JobPosting (title, description, datePosted, validThrough, employmentType, jobLocation,
  experienceRequirements.monthsOfExperience), a BreadcrumbList (category, subcategory), <script id="vacancy-data-json">
  (format, location, level, salary) and the apply button. The original link of the vacancy (hh.ru, the company's site,
  Telegram) is not on the page: HireHi gives it to a signed-in user only (POST /contact-opening), the panel never asks
  for it. The button says «Отклик на hh» when the vacancy came from hh.ru.
- The logo of a company HireHi took from hh.ru carries its hh.ru employer number: «company_hh5674346_…webp» is employer
  5674346 (checked: the same name on both sites). With it a HireHi vacancy and an hh.ru one compare by employer exactly.
- HireHi often rewrites a title (in English, with the domain: «DevOps engineer for high-load applications») and
  restructures the description («Описание: … Задачи: … Требования: … Условия: …»).

Pure functions: no I/O (services/hirehi.py reads the site).
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.base import as_utc, utcnow
from app.hh.textclean import sanitize_html
from app.hh.vacancy import VacancyData
from app.services.textmatch import contains_term, norm

BASE = "https://hirehi.ru"
HOST = "hirehi.ru"
SOURCE = "hirehi"
LABEL = "HireHi"

# the sections of the site: a category or a subcategory, each with its hub /vacancies/<slug> (seen on the site
# 2026-11-08: the category menu and the subcategory links of the listing)
CATEGORIES: dict[str, str] = {
    "development": "Разработка",
    "devops": "DevOps",
    "qa": "QA",
    "analytics": "Аналитика",
    "design": "Дизайн",
    "management": "Менеджмент",
    "marketing": "Маркетинг",
    "sales": "Продажи",
    "finance": "Финансы",
    "recruiting": "Рекрутинг",
}
SUBCATEGORIES: dict[str, str] = {
    "1c": "1C",
    "account-management": "Account Management",
    "accounting": "Бухгалтерия",
    "affiliate-manager": "Affiliate Manager",
    "android": "Android",
    "art-director": "Арт-директор",
    "aso-manager": "ASO-специалист",
    "backend": "Backend",
    "business-analyst": "Бизнес-аналитик",
    "business-development": "Business Development",
    "cco": "Коммерческий директор",
    "cfo": "Финансовый директор",
    "ci-cd": "CI/CD",
    "cloud": "Cloud",
    "content-creative": "Контент и креатив",
    "cpp": "C++",
    "crm-manager": "CRM-менеджер",
    "customer-success": "Customer Success",
    "customer-support": "Поддержка клиентов",
    "data-analyst": "Аналитик данных",
    "data-engineer": "Data Engineer",
    "delivery-manager": "Delivery-менеджер",
    "dotnet": ".NET/C#",
    "editor": "Редактор",
    "finance-management": "Управление финансами",
    "financial-analysis": "Финансовый анализ",
    "financial-controller": "Финансовый контролёр",
    "fpna": "Финансовое планирование",
    "frontend": "Frontend",
    "fullstack": "Fullstack",
    "game-artist": "Игровой арт и 3D",
    "game-design": "Game Design",
    "game-developer": "Разработчик игр",
    "general-marketing": "Общий маркетинг",
    "go": "Go",
    "graphic-design": "Графический дизайн",
    "growth-manager": "Growth Manager",
    "hr": "HR",
    "hr-administrator": "Специалист по кадрам",
    "hr-business-partner": "HR бизнес-партнёр",
    "iac": "IaC",
    "illustration": "Иллюстрация",
    "infrastructure": "Infrastructure",
    "ios": "iOS",
    "it-recruiter": "IT-рекрутер",
    "java": "Java",
    "kotlin": "Kotlin",
    "kubernetes": "Kubernetes",
    "level-designer": "Level Designer",
    "manual-qa": "Ручной тестировщик",
    "ml-ai": "ML/AI",
    "mobile": "Mobile",
    "motion-design": "Motion-дизайн",
    "nodejs": "Node.js",
    "observability": "Observability",
    "performance-marketing": "Платный трафик",
    "php": "PHP",
    "pr-manager": "PR и коммуникации",
    "product-analyst": "Продуктовый аналитик",
    "product-design": "Продуктовый дизайн",
    "product-manager": "Продакт-менеджер",
    "program-manager": "Program-менеджер",
    "project-manager": "Менеджер проектов",
    "python": "Python",
    "qa-automation": "QA Automation",
    "retention-manager": "Удержание клиентов",
    "rust": "Rust",
    "sales-management": "Sales",
    "scrum-master": "Scrum-мастер",
    "security": "Security",
    "seo": "SEO",
    "serm-orm": "Управление репутацией",
    "smm": "SMM",
    "software-architect": "Архитектор ПО",
    "sourcing": "Sourcing",
    "sre-platform": "SRE/Platform",
    "system-analyst": "Системный аналитик",
    "talent-acquisition": "Talent Acquisition",
    "team-lead": "Тимлид",
    "ux-ui": "UX/UI",
    "video-production": "Видеопродакшн",
    "web-design": "Web-дизайн",
}
# city hubs /<slug>-jobs of the Russian cities with a million people and more (sitemap-cities.xml, 2026-11-08;
# Nizhny Novgorod has no hub). A city hub filters by the city only: the section then works as a category filter of
# the panel (the listing names the category of every vacancy, not the subcategory)
CITIES: dict[str, str] = {
    "moscow": "Москва",
    "spb": "Санкт-Петербург",
    "novosibirsk": "Новосибирск",
    "ekaterinburg": "Екатеринбург",
    "kazan": "Казань",
    "krasnoyarsk": "Красноярск",
    "chelyabinsk": "Челябинск",
    "samara": "Самара",
    "ufa": "Уфа",
    "rostov": "Ростов-на-Дону",
    "krasnodar": "Краснодар",
    "omsk": "Омск",
    "voronezh": "Воронеж",
    "perm": "Пермь",
    "volgograd": "Волгоград",
}
# the `format` filter of the site: the work format and the kind of remote work (several values = any of them)
FORMATS: dict[str, str] = {
    "remote_russia": "Удалённо по РФ",
    "удалённо": "Удалённо, любая страна",
    "гибрид": "Гибрид",
    "офис": "Офис",
}
LEVELS: dict[str, str] = {
    "intern": "Стажёр",
    "junior": "Junior",
    "middle": "Middle",
    "senior": "Senior",
    "lead": "Lead",
    "head": "Head",
}
# the same values as types: the schema of the API names them (a body with another value is a 422 by the schema)
SectionSlug = Literal[
    "",
    "development",
    "devops",
    "qa",
    "analytics",
    "design",
    "management",
    "marketing",
    "sales",
    "finance",
    "recruiting",
    "1c",
    "account-management",
    "accounting",
    "affiliate-manager",
    "android",
    "art-director",
    "aso-manager",
    "backend",
    "business-analyst",
    "business-development",
    "cco",
    "cfo",
    "ci-cd",
    "cloud",
    "content-creative",
    "cpp",
    "crm-manager",
    "customer-success",
    "customer-support",
    "data-analyst",
    "data-engineer",
    "delivery-manager",
    "dotnet",
    "editor",
    "finance-management",
    "financial-analysis",
    "financial-controller",
    "fpna",
    "frontend",
    "fullstack",
    "game-artist",
    "game-design",
    "game-developer",
    "general-marketing",
    "go",
    "graphic-design",
    "growth-manager",
    "hr",
    "hr-administrator",
    "hr-business-partner",
    "iac",
    "illustration",
    "infrastructure",
    "ios",
    "it-recruiter",
    "java",
    "kotlin",
    "kubernetes",
    "level-designer",
    "manual-qa",
    "ml-ai",
    "mobile",
    "motion-design",
    "nodejs",
    "observability",
    "performance-marketing",
    "php",
    "pr-manager",
    "product-analyst",
    "product-design",
    "product-manager",
    "program-manager",
    "project-manager",
    "python",
    "qa-automation",
    "retention-manager",
    "rust",
    "sales-management",
    "scrum-master",
    "security",
    "seo",
    "serm-orm",
    "smm",
    "software-architect",
    "sourcing",
    "sre-platform",
    "system-analyst",
    "talent-acquisition",
    "team-lead",
    "ux-ui",
    "video-production",
    "web-design",
]
CitySlug = Literal[
    "",
    "moscow",
    "spb",
    "novosibirsk",
    "ekaterinburg",
    "kazan",
    "krasnoyarsk",
    "chelyabinsk",
    "samara",
    "ufa",
    "rostov",
    "krasnodar",
    "omsk",
    "voronezh",
    "perm",
    "volgograd",
]
FormatValue = Literal[
    "remote_russia",
    "удалённо",
    "гибрид",
    "офис",
]
LevelValue = Literal[
    "intern",
    "junior",
    "middle",
    "senior",
    "lead",
    "head",
]
KEYWORDS_MAX = 30
KEYWORD_MAX_CHARS = 60

WORK_FORMAT_NAMES = {"REMOTE": "Удалённо", "HYBRID": "Гибрид", "ON_SITE": "На месте работодателя"}
EXPERIENCE_NAMES = {
    "noExperience": "Нет опыта",
    "between1And3": "От 1 года до 3 лет",
    "between3And6": "От 3 до 6 лет",
    "moreThan6": "Более 6 лет",
}
EMPLOYMENT = {
    "FULL_TIME": ("FULL", "Полная"),
    "PART_TIME": ("PART", "Частичная"),
    "CONTRACTOR": ("PROJECT", "Проект или разовое задание"),
    "TEMPORARY": ("PROJECT", "Проект или разовое задание"),
}

_SSR = re.compile(r'<script[^>]*\bid="__SSR_JOBS__"[^>]*>(.*?)</script>', re.S)
_LD = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_VDATA = re.compile(r'<script[^>]*\bid="vacancy-data-json"[^>]*>(.*?)</script>', re.S)
_CARD_LINK = re.compile(r'<a\b[^>]*\bclass="job-card"[^>]*>', re.S)
_HREF = re.compile(r'\bhref="([^"]+)"')
_DATA_ID = re.compile(r'\bdata-id="(\d+)"')
_APPLY = re.compile(
    r'<a\b[^>]*\bclass="[^"]*\b(?:sidebar-apply-btn|mobile-apply-btn)\b[^"]*"[^>]*>(.*?)</a>', re.S | re.I
)
_TAG = re.compile(r"<[^>]+>")
# /<category>/<slug>-<id>: the path of a vacancy card (lower-case latin slugs, the id at the end)
_CARD_PATH = re.compile(r"^/[a-z0-9-]+/[a-z0-9-]*?-?(\d{1,12})$")
_HH_LOGO = re.compile(r"_hh(\d{1,12})_")
_FROM_HH = re.compile(r"(?<![a-zа-я])hh(?![a-zа-я])", re.I)
_SECTION_HEAD = re.compile(r"^(Описание|Задачи|Обязанности|Требования|Будет плюсом|Условия|Мы предлагаем)\s*:\s*")


class LayoutChanged(ValueError):
    """A page of HireHi without the data it always had: the site changed its markup (or answered something else)."""


def _s(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_datetime(value: Any) -> datetime | None:
    """«2026-11-08T07:26:58Z» of the listing, «2026-11-08T06:39:39.838407» of the card (UTC without a zone)."""
    text = _s(value)
    if not text:
        return None
    try:
        return as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def card_path(url: str | None) -> str | None:
    """`/<category>/<slug>-<id>` of a vacancy card (an absolute link of hirehi.ru or a path), None for anything else."""
    text = _s(url)
    if not text:
        return None
    parts = urlsplit(text)
    if parts.netloc and parts.netloc.lower() not in (HOST, "www." + HOST):
        return None
    path = parts.path.rstrip("/")
    return path if _CARD_PATH.match(path) else None


def card_url(path: str | None) -> str | None:
    return BASE + path if path else None


def hh_employer_id(icon: str | None) -> str | None:
    """The hh.ru employer number in the logo file of a company HireHi took from hh.ru (`company_hh5674346_…`)."""
    text = _s(icon)
    if not text or text.startswith("data:"):
        return None
    m = _HH_LOGO.search(text.rsplit("/", 1)[-1])
    return m.group(1) if m else None


# ---- the listing -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ListingItem:
    """One vacancy of a listing page, as the page gives it (stored as `vacancies.raw_short_json`)."""

    id: str
    title: str
    company: str | None = None
    company_icon: str | None = None
    created_at: str | None = None
    format: str | None = None
    level: str | None = None
    category: str | None = None
    salary: str | None = None
    industry: str | None = None
    from_recruiter: bool = False
    path: str | None = None

    def raw(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> ListingItem:
        return cls(
            id=str(raw.get("id") or ""),
            title=str(raw.get("title") or ""),
            company=_s(raw.get("company")),
            company_icon=_s(raw.get("company_icon")),
            created_at=_s(raw.get("created_at")),
            format=_s(raw.get("format")),
            level=_s(raw.get("level")),
            category=_s(raw.get("category")),
            salary=_s(raw.get("salary")),
            industry=_s(raw.get("industry")),
            from_recruiter=bool(raw.get("from_recruiter")),
            path=card_path(raw.get("path")),
        )

    @property
    def published_at(self) -> datetime | None:
        return parse_datetime(self.created_at)


@dataclass(frozen=True)
class Listing:
    items: list[ListingItem]
    total: int | None = None
    has_more: bool = False


def _json_blocks(page: str) -> list[Any]:
    out: list[Any] = []
    for block in _LD.findall(page):
        try:
            out.append(json.loads(block))
        except ValueError:
            continue
    return out


def _ld_nodes(page: str) -> list[dict[str, Any]]:
    """Every JSON-LD object of the page: plain, in a list or in @graph."""
    nodes: list[dict[str, Any]] = []
    for data in _json_blocks(page):
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph")
            if isinstance(graph, list):
                nodes.extend(n for n in graph if isinstance(n, dict))
            else:
                nodes.append(node)
    return nodes


def listing_paths(page: str) -> dict[str, str]:
    """id → card path of the vacancies of a listing page: the card links first, then the JSON-LD ItemList."""
    out: dict[str, str] = {}
    for tag in _CARD_LINK.findall(page):
        href, did = _HREF.search(tag), _DATA_ID.search(tag)
        path = card_path(html_lib.unescape(href.group(1))) if href else None
        if path and did:
            out.setdefault(did.group(1), path)
    for node in _ld_nodes(page):
        if node.get("@type") != "ItemList":
            continue
        for el in node.get("itemListElement") or []:
            path = card_path(_dict(el).get("url"))
            m = _CARD_PATH.match(path) if path else None
            if path and m:
                out.setdefault(m.group(1), path)
    return out


def parse_listing(page: str) -> Listing:
    """The vacancies of a listing page, newest first (LayoutChanged when the page has no list)."""
    m = _SSR.search(page)
    if not m:
        raise LayoutChanged("в странице выдачи нет списка вакансий (__SSR_JOBS__)")
    try:
        data = json.loads(m.group(1))
    except ValueError as exc:
        raise LayoutChanged("список вакансий в странице выдачи не разобран") from exc
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise LayoutChanged("в списке вакансий страницы выдачи нет поля jobs")
    paths = listing_paths(page)
    items: list[ListingItem] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        jid = _s(job.get("id"))
        if not jid or not jid.isdigit():
            continue
        icon = _s(job.get("company_icon"))
        items.append(
            ListingItem(
                id=jid,
                title=_s(job.get("title")) or "",
                company=_s(job.get("company")),
                company_icon=icon if icon and not icon.startswith("data:") else None,
                created_at=_s(job.get("created_at")),
                format=_s(job.get("format")),
                level=_s(job.get("level")),
                category=_s(job.get("category")),
                salary=_s(job.get("salary")),
                industry=_s(job.get("industry")),
                from_recruiter=bool(job.get("is_from_recruiter")),
                path=paths.get(jid),
            )
        )
    total = data.get("total_count")
    return Listing(
        items=items,
        total=total if isinstance(total, int) and not isinstance(total, bool) else None,
        has_more=bool(data.get("has_more")),
    )


# ---- the card ----------------------------------------------------------------------------------------------------

VDATA_KEYS = ("id", "title", "company", "company_icon", "category", "format", "location", "level", "salary")


@dataclass(frozen=True)
class Card:
    """A vacancy card (stored as `vacancies.raw_full_json`): the JobPosting, the text of the apply button, the
    breadcrumbs and the page's own vacancy data. `gone_at` — when the check found the page removed."""

    posting: dict[str, Any]
    apply_label: str = ""
    breadcrumbs: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    gone_at: str | None = None

    @property
    def from_hh(self) -> bool:
        """The button «Отклик на hh»: HireHi took the vacancy from hh.ru."""
        return bool(_FROM_HH.search(self.apply_label))

    @property
    def path(self) -> str | None:
        return card_path(_s(self.posting.get("url")))

    @property
    def valid_through(self) -> datetime | None:
        return parse_datetime(self.posting.get("validThrough"))

    def raw(self) -> dict[str, Any]:
        out = asdict(self)
        if not self.gone_at:
            out.pop("gone_at")
        return out

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> Card:
        crumbs = raw.get("breadcrumbs")
        return cls(
            posting=_dict(raw.get("posting")),
            apply_label=str(raw.get("apply_label") or ""),
            breadcrumbs=[str(c) for c in crumbs if c] if isinstance(crumbs, list) else [],
            data=_dict(raw.get("data")),
            gone_at=_s(raw.get("gone_at")),
        )


def _text_of(fragment: str) -> str:
    return " ".join(html_lib.unescape(_TAG.sub(" ", fragment)).split())


def parse_card(page: str) -> Card:
    """The card of a vacancy (LayoutChanged when the page has no JobPosting)."""
    nodes = _ld_nodes(page)
    posting = next((n for n in nodes if n.get("@type") == "JobPosting"), None)
    if posting is None:
        raise LayoutChanged("в карточке вакансии нет данных JobPosting")
    crumbs: list[str] = []
    for node in nodes:
        if node.get("@type") == "BreadcrumbList":
            names = [_s(_dict(el).get("name")) for el in node.get("itemListElement") or []]
            # «Все вакансии» › category › subcategory › the vacancy itself
            crumbs = [n for n in names[1:-1] if n]
            break
    label_m = _APPLY.search(page)
    data: dict[str, Any] = {}
    vdata_m = _VDATA.search(page)
    if vdata_m:
        try:
            raw = json.loads(vdata_m.group(1))
        except ValueError:
            raw = None
        if isinstance(raw, dict):
            data = {k: raw[k] for k in VDATA_KEYS if raw.get(k) not in (None, "")}
    return Card(
        posting={k: v for k, v in posting.items() if k != "@context"},
        apply_label=_text_of(label_m.group(1)) if label_m else "",
        breadcrumbs=crumbs,
        data=data,
    )


# ---- data of the panel ---------------------------------------------------------------------------------------------


def parse_format(text: str | None) -> tuple[str | None, str | None]:
    """«гибрид Москва» → (HYBRID, Москва); «удалённо по РФ», «удалённо» → (REMOTE, None); «офис» → (ON_SITE, None)."""
    raw = (text or "").strip()
    low = raw.lower().replace("ё", "е")
    for prefix, kind in (("удаленно", "REMOTE"), ("гибрид", "HYBRID"), ("офис", "ON_SITE")):
        if low.startswith(prefix):
            if kind == "REMOTE":
                return kind, None
            city = raw[len(prefix) :].strip(" ,")
            return kind, city or None
    return None, None


def format_matches(text: str | None, wanted: str) -> bool:
    """A listing format against a value of the `format` filter (FORMATS)."""
    low = (text or "").lower().replace("ё", "е")
    if wanted == "remote_russia":
        return low.startswith("удаленно") and "по рф" in low
    return low.startswith(wanted.lower().replace("ё", "е"))


def experience_of(months: Any) -> str | None:
    """monthsOfExperience → the hh.ru experience id the rules and the pre-score know."""
    if isinstance(months, bool) or not isinstance(months, (int, float)):
        return None
    if months < 12:
        return "noExperience"
    if months < 36:
        return "between1And3"
    if months < 72:
        return "between3And6"
    return "moreThan6"


_NUM = r"\d[\d   ]*"


def _int(digits: str) -> int | None:
    value = re.sub(r"\D", "", digits)
    return int(value) if value else None


def parse_salary(text: str | None) -> tuple[int | None, int | None, str | None]:
    """«от 300 000 до 1 000 000 ₽», «до 646 000 ₽», «~ 441 756 ₽», «287 000 ₽», «зп не указана» → (from, to, cur)."""
    raw = (text or "").strip()
    low = raw.lower()
    if not raw or "не указ" in low or not re.search(r"\d", raw):
        return None, None, None
    currency = "USD" if "$" in raw else "EUR" if "€" in raw else "RUR"
    lo_m = re.search(r"от\s*(" + _NUM + ")", low)
    hi_m = re.search(r"до\s*(" + _NUM + ")", low)
    if lo_m or hi_m:
        return (_int(lo_m.group(1)) if lo_m else None), (_int(hi_m.group(1)) if hi_m else None), currency
    first = re.search(_NUM, raw)
    amount = _int(first.group(0)) if first else None
    return amount, amount, currency


def _base_salary(posting: dict[str, Any]) -> tuple[int | None, int | None, str | None]:
    """schema.org MonetaryAmount of the card, when HireHi gives one (None on the cards seen so far)."""
    salary = _dict(posting.get("baseSalary"))
    value = _dict(salary.get("value"))
    lo, hi = value.get("minValue"), value.get("maxValue")
    if not isinstance(lo, (int, float)) and not isinstance(hi, (int, float)):
        single = value.get("value")
        lo = hi = single if isinstance(single, (int, float)) else None
    if lo is None and hi is None:
        return None, None, None
    code = (_s(salary.get("currency")) or "RUR").upper()
    return (
        (int(lo) if isinstance(lo, (int, float)) else None),
        (int(hi) if isinstance(hi, (int, float)) else None),
        ("RUR" if code == "RUB" else code),
    )


def description_html(text: str | None) -> str | None:
    """The plain description of the card as HTML for the panel: paragraphs, the section names of HireHi in bold."""
    if not text or not text.strip():
        return None
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text.strip()):
        para = para.strip()
        if not para:
            continue
        head = _SECTION_HEAD.match(para)
        body = html_lib.escape(para[head.end() :] if head else para).replace("\n", "<br>")
        out.append(f"<p><strong>{html_lib.escape(head.group(1))}:</strong> {body}</p>" if head else f"<p>{body}</p>")
    return sanitize_html("".join(out)) or None


def vacancy_data(
    short: dict[str, Any] | None, full: dict[str, Any] | None, *, now: datetime | None = None
) -> VacancyData:
    """VacancyData of a HireHi vacancy from its listing item and, when read, its card (the card wins). `hh_id` stays
    empty: a HireHi vacancy has no hh.ru number."""
    item = ListingItem.from_raw(short) if short else None
    card = Card.from_raw(full) if full else None
    posting = card.posting if card else {}
    data = card.data if card else {}
    title = _s(posting.get("title")) or _s(data.get("title")) or (item.title if item else "") or ""
    company = _s(_dict(posting.get("hiringOrganization")).get("name")) or _s(data.get("company"))
    company = company or (item.company if item else None)
    icon = (item.company_icon if item else None) or _s(data.get("company_icon"))
    fmt = _s(data.get("format")) or (item.format if item else None)
    kind, city = parse_format(fmt)
    address = _dict(_dict(posting.get("jobLocation")).get("address"))
    locality = _s(address.get("addressLocality")) or _s(data.get("location"))
    published = (item.published_at if item else None) or parse_datetime(posting.get("datePosted"))
    valid = card.valid_through if card else None
    moment = now or utcnow()
    months = _dict(posting.get("experienceRequirements")).get("monthsOfExperience")
    experience = experience_of(months)
    employment = posting.get("employmentType")
    employment_key = employment[0] if isinstance(employment, list) and employment else employment
    form = EMPLOYMENT.get(str(employment_key or "").upper())
    lo, hi, currency = _base_salary(posting)
    if lo is None and hi is None:
        lo, hi, currency = parse_salary(_s(data.get("salary")) or (item.salary if item else None))
    text = _s(posting.get("description"))
    path = (item.path if item else None) or (card.path if card else None)
    url = card_url(path)
    if card and card.breadcrumbs:
        roles = card.breadcrumbs[-1:]
    elif item and item.category:
        roles = [CATEGORIES.get(item.category, item.category)]
    else:
        roles = []
    names = []
    if kind:
        names = [WORK_FORMAT_NAMES[kind] + (" по РФ" if kind == "REMOTE" and "по рф" in (fmt or "").lower() else "")]
    return VacancyData(
        hh_id="",
        name=title,
        employer_id=hh_employer_id(icon),
        employer_name=company,
        area_name=city or locality,
        salary_from=lo,
        salary_to=hi,
        salary_currency=currency,
        experience_id=experience,
        experience_name=EXPERIENCE_NAMES.get(experience) if experience else None,
        employment_form_id=form[0] if form else None,
        employment_form_name=form[1] if form else None,
        work_format_ids=[kind] if kind else [],
        work_format_names=names,
        published_at=published,
        archived=bool(card and card.gone_at) or (valid is not None and valid <= moment),
        alternate_url=url,
        apply_alternate_url=url,
        professional_roles=roles,
        description_html=description_html(text),
        description_text=text,
        is_full=card is not None,
    )


# ---- searches ------------------------------------------------------------------------------------------------------


def _clean_list(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        s = str(v).strip()
        if s and s not in out:
            out.append(s)
    return out


class HireHiParams(BaseModel):
    """A search of HireHi: the filters the site allows (robots.txt Clean-param) go to the address of a listing page,
    the words are checked by the panel in the title. `search=` and `sort=` (closed in robots.txt) are never used."""

    model_config = ConfigDict(extra="ignore")

    # a category or a subcategory of the site: the hub /vacancies/<section>; with a city — a category only
    section: SectionSlug = ""
    # a city hub /<city>-jobs (CITIES)
    city: CitySlug = ""
    formats: list[FormatValue] = Field(default_factory=list)
    levels: list[LevelValue] = Field(default_factory=list)
    # the panel's own filter: one of the words in the title (empty = every vacancy of the page)
    keywords: list[str] = Field(default_factory=list)

    @field_validator("formats", "levels")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))

    @field_validator("keywords")
    @classmethod
    def _keywords(cls, v: list[str]) -> list[str]:
        words = _clean_list(v)
        if len(words) > KEYWORDS_MAX:
            raise ValueError(f"слов не больше {KEYWORDS_MAX}")
        if any(len(w) > KEYWORD_MAX_CHARS for w in words):
            raise ValueError(f"слово длиннее {KEYWORD_MAX_CHARS} символов")
        return words

    def problem(self) -> str | None:
        """A combination the site can not serve (the API answers it with a 422 phrase): a city hub and a
        subcategory — the listing of a city names the category of a vacancy, not its subcategory."""
        if self.city and self.section and self.section not in CATEGORIES:
            return "с городом раздел HireHi — только категория: подраздел страница города не различает"
        return None

    def path(self, page: int = 1) -> str:
        """The address of the listing page `page` (1 = the first)."""
        if self.city:
            base = f"/{self.city}-jobs"
        elif self.section:
            base = f"/vacancies/{self.section}"
        else:
            base = "/"
        query: list[tuple[str, str]] = [("format", f) for f in self.formats] + [("level", lv) for lv in self.levels]
        if page > 1:
            query.append(("page", str(page)))
        return base + ("?" + urlencode(query) if query else "")

    def summary(self) -> str:
        parts: list[str] = []
        if self.section:
            parts.append(CATEGORIES.get(self.section) or SUBCATEGORIES.get(self.section) or self.section)
        if self.city:
            parts.append(CITIES.get(self.city, self.city))
        if self.formats:
            # the first letter only: «Удалённо по РФ» → «удалённо по РФ»
            parts.append(", ".join(FORMATS[f][:1].lower() + FORMATS[f][1:] for f in self.formats))
        if self.levels:
            parts.append("/".join(LEVELS[lv] for lv in self.levels))
        if self.keywords:
            parts.append("слова: " + ", ".join(self.keywords))
        return " · ".join(parts) or "все вакансии"


def item_reason(item: ListingItem, params: HireHiParams) -> str | None:
    """Why a listing item is not this search's on the panel's side of the filters, None when it is. The site applies
    the section, the formats and the levels itself; they are checked again in case it ignored one."""
    if params.city and params.section and item.category != params.section:
        return f"раздел {item.category or '—'}"
    if params.formats and not any(format_matches(item.format, f) for f in params.formats):
        return f"формат {item.format or '—'}"
    if params.levels and (item.level or "") not in params.levels:
        return f"уровень {item.level or '—'}"
    if params.keywords:
        title = norm(item.title)
        if not any(contains_term(title, k) for k in params.keywords):
            return "в названии нет слов поиска"
    return None


# ---- the same vacancy on hh.ru -------------------------------------------------------------------------------------

# HireHi writes titles in English as often as in Russian: «DevOps engineer» for «DevOps-инженер»
_SYNONYMS = {
    "engineer": "инженер",
    "developer": "разработчик",
    "programmer": "разработчик",
    "программист": "разработчик",
    "dev": "разработчик",
    "manager": "менеджер",
    "analyst": "аналитик",
    "designer": "дизайнер",
    "tester": "тестировщик",
    "qa": "тестировщик",
    "lead": "лид",
    "ведущий": "лид",
    "teamlead": "лид",
    "тимлид": "лид",
    "head": "руководитель",
    "руководителя": "руководитель",
    "director": "директор",
    "директора": "директор",
    "product": "продукт",
    "продуктовый": "продукт",
    "продакт": "продукт",
    "продукта": "продукт",
    "project": "проект",
    "проектов": "проект",
    "проекта": "проект",
    "старший": "senior",
    "младший": "junior",
    "specialist": "специалист",
    "специалиста": "специалист",
    "architect": "архитектор",
    "administrator": "администратор",
    "sales": "продажи",
    "продаж": "продажи",
    "продажам": "продажи",
    "data": "данные",
    "данных": "данные",
    "support": "поддержка",
    "поддержки": "поддержка",
    "recruiter": "рекрутер",
    "marketing": "маркетинг",
    "маркетолог": "маркетинг",
}
_TITLE_STOP = frozenset(
    {"in", "for", "of", "the", "and", "a", "an", "at", "on", "with", "в", "для", "по", "и", "на", "с"}
)


def _title_words(title: str) -> set[str]:
    words: set[str] = set()
    for w in re.split(r"[^a-zа-я0-9+#]+", norm(title)):
        if len(w) < 2 or w in _TITLE_STOP:
            continue
        words.add(_SYNONYMS.get(w, w)[:7])  # «разработчик» and «developer» meet as «разрабо»
    return words


def title_similarity(hirehi_title: str, hh_title: str) -> float:
    """The share of the hh.ru title's words found in the HireHi one: HireHi keeps the role and adds the domain
    («Бизнес-аналитик» → «бизнес-аналитик в логистике»), often in English («DevOps-инженер» → «DevOps engineer for
    high-load applications»). 1.0 — all of them."""
    target = _title_words(hh_title)
    if not target:
        return 0.0
    return len(_title_words(hirehi_title) & target) / len(target)
