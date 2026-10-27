"""Light structure from an uploaded resume file (hh.ru PDF/DOCX export in Russian): the panel gets skills,
total experience, title, city, languages and the like without the hh API. Anything not recognised is left
empty — the full text still goes into profile.md, so nothing is lost; only what was found is trusted."""

from __future__ import annotations

import re

from app.hh.resume import NormalizedResume

PARSER_VERSION = 1

_GENDER = re.compile(r"^(Мужчина|Женщина),?\s")
_AGE = re.compile(r"(\d{1,2})\s+(?:год|года|лет)")
_YEARS = re.compile(r"(\d{1,2})\s+(?:год|года|лет)")
_MONTHS = re.compile(r"(\d{1,2})\s+(?:месяц|месяца|месяцев)")
# «Опыт работы — 3 года», «Опыт работы: 3 года» or, without a dash, «Опыт работы 10 лет 1 месяц» (a digit must follow
# then: «Опыт работы в продажах 5 лет» from the «О себе» text is not the total)
_TOTAL = re.compile(r"Опыт работы(?:\s*[—–:-]\s*|[ \t]+(?=\d))([^\n]+)")
_MONEY = re.compile(r"(\d[\d\s ]{2,})\s*(₽|руб|\$|€|USD|EUR|KZT|BYR|UZS)", re.IGNORECASE)
_SECTION_HEADS = (
    "Дополнительная информация",
    "Образование",
    "Опыт работы",
    "Навыки",
    "Желаемая должность",
    "Повышение квалификации",
    "Резюме обновлено",
)
_CURRENCY = {"₽": "RUR", "руб": "RUR", "$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR", "kzt": "KZT"}


def total_months(text: str) -> int | None:
    """`Опыт работы — 3 года 8 месяцев` → 44; `— 8 месяцев` → 8; nothing → None."""
    m = _TOTAL.search(text)
    if not m:
        return None
    tail = m.group(1)
    years = _YEARS.search(tail)
    months = _MONTHS.search(tail)
    if not years and not months:
        return None
    return (int(years.group(1)) if years else 0) * 12 + (int(months.group(1)) if months else 0)


def _after(text: str, label: str) -> str | None:
    """Value on the same line after `label:`."""
    m = re.search(rf"^{re.escape(label)}\s*:?\s*(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _skills_from_layout(layout: str) -> list[str]:
    """In the hh export every skill is a separate text run; the layout extraction keeps them apart by runs of
    spaces, so a skills block looks like `Навыки   Python   Claude Code   Разработка нового продукта`."""
    lines = layout.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^\s*Навыки\s{2,}(\S.*)$", line)
        if not m:
            i += 1
            continue
        block = [m.group(1)]
        j = i + 1
        while j < len(lines) and lines[j].startswith(" ") and lines[j].strip():
            block.append(lines[j].strip())
            j += 1
        for part in block:
            for item in re.split(r"\s{2,}", part):
                item = item.strip(" ,;")
                if 1 < len(item) <= 60 and item.lower() not in {s.lower() for s in out}:
                    out.append(item)
        break
    return out


def _skills_from_plain(text: str) -> list[str]:
    """Fallback for DOCX/TXT: a `Навыки` line with commas or semicolons; a bare space-separated list is
    ambiguous (multi-word skills) and is not split."""
    m = re.search(r"^\s*(?:Ключевые навыки|Навыки)\s*:?\s*(.+)$", text, re.MULTILINE)
    if not m:
        return []
    value = m.group(1)
    if not any(sep in value for sep in (",", ";", "•", "|")):
        return []
    out: list[str] = []
    for item in re.split(r"[,;•|]", value):
        item = item.strip(" .")
        if 1 < len(item) <= 60 and item.lower() not in {s.lower() for s in out}:
            out.append(item)
    return out


def _languages(text: str) -> list[str]:
    m = re.search(r"Знание языков\s*(.*?)(?:\n\s*Навыки\b|\n\s*Дополнительная информация|\Z)", text, re.DOTALL)
    if not m:
        return []
    out: list[str] = []
    for line in m.group(1).split("\n"):
        line = re.sub(r"\s{2,}", " ", line).strip()
        if line and ("—" in line or "-" in line):
            out.append(line)
    return out[:8]


def _first_lines_name(text: str) -> str | None:
    """The export starts with the name (possibly wrapped to two lines) followed by `Мужчина, 24 года, …`."""
    lines = [ln.strip() for ln in text.split("\n")[:5]]
    for idx, line in enumerate(lines):
        if _GENDER.match(line):
            name = " ".join(lines[:idx]).strip()
            return name or None
    return None


def parse_resume_text(text: str, layout: str | None = None) -> NormalizedResume | None:
    """Returns a partial NormalizedResume (origin=file) or None when nothing useful was recognised."""
    if not text or not text.strip():
        return None
    resume = NormalizedResume(origin="file", parser_version=PARSER_VERSION)
    resume.total_experience_months = total_months(text)
    resume.skills = _skills_from_layout(layout) if layout else []
    if not resume.skills:
        resume.skills = _skills_from_plain(text)
    resume.full_name = _first_lines_name(text)
    gender_line = next((ln for ln in text.split("\n")[:6] if _GENDER.match(ln.strip())), None)
    if gender_line:
        age = _AGE.search(gender_line)
        resume.age = int(age.group(1)) if age else None
    lives = _after(text, "Проживает")
    if lives:
        resume.area = lives.split(",")[0].strip() or None
    citizenship = _after(text, "Гражданство")
    if citizenship:
        resume.citizenship = [citizenship.split(",")[0].strip()]
    relocation = _after(text, "Готов к переезду") or _after(text, "Готова к переезду")
    if relocation:
        resume.relocation = "готов к переезду: " + relocation.split(", готов")[0].strip()
        if "командировк" in relocation:
            resume.business_trips = "готов к командировкам"
    elif re.search(r"^Не готов[а]? к переезду", text, re.MULTILINE):
        resume.relocation = "не готов к переезду"
    formats = _after(text, "Формат работы")
    if formats:
        resume.work_formats = [f.strip() for f in formats.split(",") if f.strip()]
    employment = _after(text, "Тип занятости")
    if employment:
        resume.employment_forms = [f.strip() for f in employment.split(",") if f.strip()]
    m = re.search(r"Желаемая должность и зарплата\s*\n\s*(.+)", text)
    if m:
        resume.title = m.group(1).strip() or None
        following = text[m.end() : m.end() + 200]
        money = _MONEY.search(following)
        if money:
            digits = re.sub(r"\D", "", money.group(1))
            if digits:
                resume.salary_amount = int(digits)
                resume.salary_currency = _CURRENCY.get(money.group(2).lower(), money.group(2).upper())
    resume.languages = _languages(text)
    roles = re.search(r"Специализации:\s*\n((?:\s*[—–-]\s*.+\n?)+)", text)
    if roles:
        resume.professional_roles = [
            re.sub(r"^\s*[—–-]\s*", "", ln).strip() for ln in roles.group(1).split("\n") if ln.strip()
        ][:10]
    found = any(
        [
            resume.total_experience_months is not None,
            resume.skills,
            resume.title,
            resume.full_name,
            resume.languages,
        ]
    )
    return resume if found else None
