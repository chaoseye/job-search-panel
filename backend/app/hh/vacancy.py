"""Normalization of hh.ru vacancy JSON (short search item or full vacancy) into VacancyData."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.db.base import as_utc
from app.hh.textclean import html_to_text, sanitize_html, strip_highlight

# A vacancy link on an hh site: hh.ru with its city subdomains, hh.kz, hh.uz, rabota.by, headhunter.ge/kg (one id
# space, all served by api.hh.ru). `/vacancy/<n>` on another domain — job.merlion.com, rabota5ka.ru, a lookalike
# «hh.ru.example.com» or «myhh.ru» — is not an hh id and used to fetch an unrelated hh vacancy
HH_VACANCY_LINK = re.compile(
    r"(?<![\w.-])(?:[\w-]+\.)*(?:hh\.(?:ru|kz|uz)|rabota\.by|headhunter\.(?:ge|kg))/vacancy/(\d{1,12})(?!\w)",
    re.IGNORECASE,
)

# experience id -> (min_years, max_years); max 99 = open-ended
EXPERIENCE_RANGES: dict[str, tuple[float, float]] = {
    "noExperience": (0.0, 1.0),
    "between1And3": (1.0, 3.0),
    "between3And6": (3.0, 6.0),
    "moreThan6": (6.0, 99.0),
}
NDFL_NET_FACTOR = 0.87
RUR_CODES = {"RUR", "RUB"}
# fields of the full vacancy the panel never reads and does not store: `branded_description` is the employer's styled
# page (HTML with CSS and scripts; `description` always comes with it) — it can take most of the stored vacancy JSON
# and of every backup copy
UNUSED_FULL_FIELDS = ("branded_description",)


class VacancyData(BaseModel):
    hh_id: str
    name: str = ""
    employer_id: str | None = None
    employer_name: str | None = None
    employer_accredited_it: bool | None = None
    employer_trusted: bool | None = None
    employer_blacklisted: bool | None = None
    area_id: str | None = None
    area_name: str | None = None
    salary_from: int | None = None
    salary_to: int | None = None
    salary_currency: str | None = None
    salary_gross: bool | None = None
    experience_id: str | None = None
    experience_name: str | None = None
    employment_form_id: str | None = None
    employment_form_name: str | None = None
    work_format_ids: list[str] = Field(default_factory=list)
    work_format_names: list[str] = Field(default_factory=list)
    schedule_id: str | None = None
    published_at: datetime | None = None
    archived: bool = False
    type_id: str | None = None
    response_letter_required: bool = False
    has_test: bool = False
    test_required: bool | None = None
    response_url: str | None = None
    alternate_url: str | None = None
    apply_alternate_url: str | None = None
    key_skills: list[str] = Field(default_factory=list)
    professional_roles: list[str] = Field(default_factory=list)
    description_html: str | None = None
    description_text: str | None = None
    snippet_requirement: str | None = None
    snippet_responsibility: str | None = None
    relations: list[str] = Field(default_factory=list)
    responses_count: int | None = None
    # workplace: «улица, дом» without the city (area_name has it) and metro stations, the nearest first
    address: str | None = None
    metro_stations: list[str] = Field(default_factory=list)
    is_full: bool = False

    # ---- derived -----------------------------------------------------------

    @property
    def is_remote(self) -> bool:
        return "REMOTE" in self.work_format_ids or self.schedule_id == "remote"

    @property
    def is_direct(self) -> bool:
        return self.type_id == "direct" or bool(self.response_url)

    def salary_net_estimate(self) -> int | None:
        amount = self.salary_to or self.salary_from
        if not amount:
            return None
        if self.salary_gross and (self.salary_currency or "RUR").upper() in RUR_CODES:
            return int(round(amount * NDFL_NET_FACTOR))
        return int(amount)

    def searchable_text(self) -> str:
        parts = [
            self.name,
            " ".join(self.key_skills),
            self.description_text or "",
            self.snippet_requirement or "",
            self.snippet_responsibility or "",
        ]
        return "\n".join(p for p in parts if p)


def storable_full(raw: dict[str, Any]) -> dict[str, Any]:
    """The full vacancy as it goes to `vacancies.raw_full_json`: without UNUSED_FULL_FIELDS."""
    return {k: v for k, v in raw.items() if k not in UNUSED_FULL_FIELDS}


def to_rur(amount: int | float | None, currency: str | None, rates: dict[str, float]) -> int | None:
    """Converts an amount to RUR using hh dictionary rates (rate = units of currency per 1 RUR)."""
    if amount is None:
        return None
    code = (currency or "RUR").upper()
    if code in RUR_CODES:
        return int(round(amount))
    rate = rates.get(code)
    if not rate:
        return None
    return int(round(amount / rate))


def parse_hh_datetime(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _s(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _dict(raw: dict[str, Any], key: str) -> dict[str, Any]:
    """`raw[key]` when it is a dict, else {} (typed narrowing for tolerant parsers)."""
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    """`value` when it is a list, else [] — hh sometimes changes a field's shape; a scalar where a list is expected
    must not take the whole vacancy (and the search page it came with) down."""
    return value if isinstance(value, list) else []


def _obj_id(obj: Any) -> str | None:
    return _s(obj.get("id")) if isinstance(obj, dict) else None


def _obj_name(obj: Any) -> str | None:
    return _s(obj.get("name")) if isinstance(obj, dict) else None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _address(raw: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Street with the building and the metro stations of `address`; hh puts the nearest station into `metro`
    and all of them into `metro_stations`. The city is dropped (the vacancy area already names it); hh sometimes
    leaves `city` empty and keeps it only at the start of `raw`, so the area name is the fallback."""
    address = _dict(raw, "address")
    if not address:
        return None, []
    street = ", ".join(x for x in (_s(address.get("street")), _s(address.get("building"))) if x)
    if not street:
        text = _s(address.get("raw")) or ""
        city = _s(address.get("city")) or _obj_name(raw.get("area")) or ""
        if city and text.lower().startswith(city.lower()):
            text = text[len(city) :].lstrip(" ,")
        street = text
    stations: list[str] = []
    for m in [address.get("metro"), *_list(address.get("metro_stations"))]:
        name = _s(m.get("station_name")) if isinstance(m, dict) else None
        if name and name not in stations:
            stations.append(name)
    return street or None, stations


def normalize_vacancy(raw: dict[str, Any], *, full: bool) -> VacancyData:
    employer = _dict(raw, "employer")
    salary = _dict(raw, "salary") or _dict(raw, "salary_range")
    snippet = _dict(raw, "snippet")
    test: dict[str, Any] | None = _dict(raw, "test") or None
    counters = _dict(raw, "counters")

    work_formats = [w for w in _list(raw.get("work_format")) if isinstance(w, dict)]
    relations = [str(r) for r in _list(raw.get("relations")) if r]

    description_html: str | None = None
    description_text: str | None = None
    if full and isinstance(raw.get("description"), str):
        description_html = sanitize_html(raw["description"])
        description_text = html_to_text(raw["description"])

    key_skills = [_obj_name(k) or "" for k in _list(raw.get("key_skills")) if isinstance(k, dict) and _obj_name(k)]
    address, metro_stations = _address(raw)

    return VacancyData(
        hh_id=str(raw.get("id") or ""),
        name=_s(raw.get("name")) or "",
        employer_id=_obj_id(employer),
        employer_name=_obj_name(employer),
        employer_accredited_it=employer.get("accredited_it_employer")
        if isinstance(employer.get("accredited_it_employer"), bool)
        else None,
        employer_trusted=employer.get("trusted") if isinstance(employer.get("trusted"), bool) else None,
        employer_blacklisted=employer.get("blacklisted") if isinstance(employer.get("blacklisted"), bool) else None,
        area_id=_obj_id(raw.get("area")),
        area_name=_obj_name(raw.get("area")),
        salary_from=_int(salary.get("from")),
        salary_to=_int(salary.get("to")),
        salary_currency=_s(salary.get("currency")),
        salary_gross=salary.get("gross") if isinstance(salary.get("gross"), bool) else None,
        experience_id=_obj_id(raw.get("experience")),
        experience_name=_obj_name(raw.get("experience")),
        employment_form_id=_obj_id(raw.get("employment_form")) or _obj_id(raw.get("employment")),
        employment_form_name=_obj_name(raw.get("employment_form")) or _obj_name(raw.get("employment")),
        work_format_ids=[i for i in (_obj_id(w) for w in work_formats) if i],
        work_format_names=[n for n in (_obj_name(w) for w in work_formats) if n],
        schedule_id=_obj_id(raw.get("schedule")),
        published_at=parse_hh_datetime(raw.get("published_at")),
        archived=bool(raw.get("archived")),
        type_id=_obj_id(raw.get("type")),
        response_letter_required=bool(raw.get("response_letter_required")),
        has_test=bool(raw.get("has_test")),
        test_required=test.get("required") if test and isinstance(test.get("required"), bool) else None,
        response_url=_s(raw.get("response_url")),
        alternate_url=_s(raw.get("alternate_url")),
        apply_alternate_url=_s(raw.get("apply_alternate_url")),
        key_skills=key_skills,
        professional_roles=[n for n in (_obj_name(r) for r in _list(raw.get("professional_roles"))) if n],
        description_html=description_html,
        description_text=description_text,
        snippet_requirement=strip_highlight(_s(snippet.get("requirement"))) or None,
        snippet_responsibility=strip_highlight(_s(snippet.get("responsibility"))) or None,
        relations=relations,
        responses_count=_int(counters.get("responses")),
        address=address,
        metro_stations=metro_stations,
        is_full=full,
    )


def merge_full(short: VacancyData, full: VacancyData) -> VacancyData:
    """Full vacancy data wins; snippets and counters come only from the search item."""
    data = full.model_copy()
    data.snippet_requirement = short.snippet_requirement
    data.snippet_responsibility = short.snippet_responsibility
    if data.responses_count is None:
        data.responses_count = short.responses_count
    if not data.relations:
        data.relations = short.relations
    if not data.address and not data.metro_stations:
        data.address, data.metro_stations = short.address, list(short.metro_stations)
    return data
