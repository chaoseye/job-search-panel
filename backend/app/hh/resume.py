"""Normalization of an hh.ru resume JSON into a stable structure."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from app.hh.vacancy import _dict, _list


class ExperienceItem(BaseModel):
    company: str | None = None
    position: str | None = None
    start: str | None = None  # YYYY-MM-DD
    end: str | None = None  # None = present
    description: str | None = None
    industries: list[str] = Field(default_factory=list)
    area: str | None = None


class EducationItem(BaseModel):
    kind: str = "primary"  # primary | additional | attestation
    name: str | None = None
    organization: str | None = None
    result: str | None = None
    year: int | None = None


class NormalizedResume(BaseModel):
    hh_id: str | None = None
    title: str | None = None
    full_name: str | None = None
    age: int | None = None
    area: str | None = None
    salary_amount: int | None = None
    salary_currency: str | None = None
    total_experience_months: int | None = None
    skills: list[str] = Field(default_factory=list)
    about: str | None = None
    experience: list[ExperienceItem] = Field(default_factory=list)
    education_level: str | None = None
    education: list[EducationItem] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    professional_roles: list[str] = Field(default_factory=list)
    relocation: str | None = None
    business_trips: str | None = None
    work_formats: list[str] = Field(default_factory=list)
    employment_forms: list[str] = Field(default_factory=list)
    schedules: list[str] = Field(default_factory=list)
    employments: list[str] = Field(default_factory=list)
    certificates: list[str] = Field(default_factory=list)
    sites: list[str] = Field(default_factory=list)
    citizenship: list[str] = Field(default_factory=list)
    status: str | None = None
    updated_at: str | None = None
    alternate_url: str | None = None
    can_publish_or_update: bool | None = None
    next_publish_at: str | None = None
    # hh = from the API, file = recognised in an uploaded export (services/resume_parse.py)
    origin: str = "hh"
    parser_version: int | None = None


def _s(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _name(obj: Any) -> str | None:
    if isinstance(obj, dict):
        return _s(obj.get("name")) or _s(obj.get("title")) or _s(obj.get("id"))
    return _s(obj)


def _names(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for item in items:
        name = _name(item)
        if name and name not in out:
            out.append(name)
    return out


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def normalize_resume(raw: dict[str, Any]) -> NormalizedResume:
    """Tolerant to missing/extra keys; never raises on shape differences."""
    salary = _dict(raw, "salary")
    total = _dict(raw, "total_experience")
    education = _dict(raw, "education")
    relocation = _dict(raw, "relocation")

    name_parts = (_s(raw.get("last_name")), _s(raw.get("first_name")), _s(raw.get("middle_name")))
    full_name = " ".join(p for p in name_parts if p)

    skills: list[str] = []
    for key in ("skill_set", "key_skills"):
        for name in _names(raw.get(key)):
            if name not in skills:
                skills.append(name)

    experience: list[ExperienceItem] = []
    for item in _list(raw.get("experience")):
        if not isinstance(item, dict):
            continue
        employer = _dict(item, "employer")
        experience.append(
            ExperienceItem(
                company=_s(item.get("company")) or _name(employer),
                position=_s(item.get("position")),
                start=_s(item.get("start")),
                end=_s(item.get("end")),
                description=_s(item.get("description")),
                industries=_names(item.get("industries")),
                area=_name(item.get("area")),
            )
        )

    edu_items: list[EducationItem] = []
    for kind in ("primary", "additional", "attestation"):
        for item in _list(education.get(kind)):
            if not isinstance(item, dict):
                continue
            edu_items.append(
                EducationItem(
                    kind=kind,
                    name=_s(item.get("name")),
                    organization=_s(item.get("organization")),
                    result=_s(item.get("result")),
                    year=_int(item.get("year")),
                )
            )

    languages: list[str] = []
    for item in _list(raw.get("language")):
        if not isinstance(item, dict):
            continue
        lang_name = _name(item)
        level = _name(item.get("level"))
        if lang_name:
            languages.append(f"{lang_name} — {level}" if level else lang_name)

    certificates: list[str] = []
    for item in _list(raw.get("certificate")):
        if not isinstance(item, dict):
            continue
        title = _s(item.get("title"))
        achieved = _s(item.get("achieved_at"))
        if title:
            certificates.append(f"{title} ({achieved[:4]})" if achieved else title)

    sites: list[str] = []
    for item in _list(raw.get("site")):
        if isinstance(item, dict) and _s(item.get("url")):
            sites.append(str(item["url"]).strip())
    for item in _list(raw.get("portfolio")):
        if isinstance(item, dict) and _s(item.get("url")):
            sites.append(str(item["url"]).strip())

    status: dict[str, Any] | None = _dict(raw, "status") or None

    return NormalizedResume(
        hh_id=_s(raw.get("id")),
        title=_s(raw.get("title")),
        full_name=full_name or None,
        age=_int(raw.get("age")),
        area=_name(raw.get("area")),
        salary_amount=_int(salary.get("amount")),
        salary_currency=_s(salary.get("currency")),
        total_experience_months=_int(total.get("months")),
        skills=skills,
        about=_s(raw.get("skills")),
        experience=experience,
        education_level=_name(education.get("level")),
        education=edu_items,
        languages=languages,
        professional_roles=_names(raw.get("professional_roles")),
        relocation=_name(relocation.get("type")),
        business_trips=_name(raw.get("business_trip_readiness")),
        work_formats=_names(raw.get("work_format")),
        employment_forms=_names(raw.get("employment_form")),
        schedules=_names(raw.get("schedules")),
        employments=_names(raw.get("employments")),
        certificates=certificates,
        sites=sites,
        citizenship=_names(raw.get("citizenship")),
        status=_s(status.get("id")) if status else None,
        updated_at=_s(raw.get("updated_at")),
        alternate_url=_s(raw.get("alternate_url")),
        can_publish_or_update=raw.get("can_publish_or_update")
        if isinstance(raw.get("can_publish_or_update"), bool)
        else None,
        next_publish_at=_s(raw.get("next_publish_at")),
    )


# ---- formatting helpers shared with profile.md rendering --------------------


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    n_abs = abs(n) % 100
    n1 = n_abs % 10
    if 10 < n_abs < 20:
        return many
    if n1 == 1:
        return one
    if 2 <= n1 <= 4:
        return few
    return many


def months_to_text(months: int | None) -> str:
    if months is None:
        return "—"
    years, rest = divmod(max(0, months), 12)
    parts: list[str] = []
    if years:
        parts.append(f"{years} {plural_ru(years, 'год', 'года', 'лет')}")
    if rest or not years:
        parts.append(f"{rest} мес")
    return " ".join(parts)


def format_month(value: str | None) -> str:
    if not value:
        return "н.в."
    try:
        d = date.fromisoformat(value[:10])
    except ValueError:
        return value
    return f"{d.month:02d}.{d.year}"


def months_between(start: str | None, end: str | None, today: date | None = None) -> int | None:
    if not start:
        return None
    try:
        s = date.fromisoformat(start[:10])
        e = date.fromisoformat(end[:10]) if end else (today or date.today())
    except ValueError:
        return None
    return max(0, (e.year - s.year) * 12 + (e.month - s.month))


def resume_to_text(resume: NormalizedResume) -> str:
    """Plain-text rendering of a structured resume (used for previews and search)."""
    lines: list[str] = []
    if resume.title:
        lines.append(resume.title)
    if resume.area:
        lines.append(f"Регион: {resume.area}")
    if resume.total_experience_months is not None:
        lines.append(f"Опыт: {months_to_text(resume.total_experience_months)}")
    if resume.salary_amount:
        lines.append(f"Зарплата: {resume.salary_amount} {resume.salary_currency or ''}".strip())
    if resume.skills:
        lines.append("Навыки: " + ", ".join(resume.skills))
    for item in resume.experience:
        head = " — ".join(p for p in (item.company, item.position) if p)
        lines.append(f"\n{head} ({format_month(item.start)} – {format_month(item.end)})")
        if item.description:
            lines.append(item.description)
    if resume.about:
        lines.append("\nО себе:\n" + resume.about)
    if resume.education:
        lines.append("\nОбразование:")
        for e in resume.education:
            lines.append(", ".join(p for p in (e.name, e.organization, e.result, str(e.year) if e.year else None) if p))
    if resume.languages:
        lines.append("Языки: " + "; ".join(resume.languages))
    return "\n".join(lines).strip()
