"""Rendering of data/exchange/profile.md from the resume and preferences."""

from __future__ import annotations

from datetime import date

from app.hh.resume import NormalizedResume, format_month, months_between, months_to_text
from app.services.preferences import (
    BUSINESS_TRIP_LABELS,
    EMPLOYMENT_FORM_LABELS,
    WORK_FORMAT_LABELS,
    Preferences,
)

FILE_TEXT_MAX_CHARS = 15_000
CURRENCY_SYMBOLS = {"RUR": "₽", "RUB": "₽", "USD": "$", "EUR": "€", "KZT": "₸", "BYR": "Br", "BYN": "Br"}


def _money(amount: int | None, currency: str | None) -> str | None:
    if not amount:
        return None
    formatted = f"{amount:,}".replace(",", " ")
    return f"{formatted} {CURRENCY_SYMBOLS.get((currency or 'RUR').upper(), currency or '')}".strip()


def _join(values: list[str], sep: str = ", ") -> str:
    return sep.join(v for v in values if v) or "—"


def render_profile_md(
    *,
    version: int,
    generated_at: str,
    resume: NormalizedResume | None,
    file_text: str | None,
    file_name: str | None,
    prefs: Preferences,
    today: date | None = None,
) -> str:
    lines: list[str] = [
        "---",
        f"profile_version: {version}",
        f"generated_at: {generated_at}",
        f"resume_id: {resume.hh_id if resume and resume.hh_id else 'none'}",
        f"resume_file: {file_name or 'none'}",
        "---",
        "# Кандидат",
    ]

    title = (resume.title if resume and resume.title else None) or _join(prefs.target_titles)
    months = resume.total_experience_months if resume else None
    if prefs.own_experience_months is not None:
        months = prefs.own_experience_months
    experience_total = months_to_text(months)
    area = _join(prefs.areas) if prefs.areas else (resume.area if resume and resume.area else "—")
    salary = _money(prefs.salary_min, prefs.salary_currency) if prefs.salary_min else None
    if salary is None and resume:
        salary = _money(resume.salary_amount, resume.salary_currency)
    lines.append(
        f"**Должность:** {title} · **Опыт всего:** {experience_total} · **Регион:** {area} · "
        f"**Зарплата (нетто):** {('от ' + salary) if salary else '—'}"
    )

    formats = [WORK_FORMAT_LABELS[f] for f in prefs.work_formats] or (resume.work_formats if resume else [])
    employment = [EMPLOYMENT_FORM_LABELS[f] for f in prefs.employment_forms] or (
        resume.employment_forms if resume else []
    )
    relocation = "да" if prefs.relocation else (resume.relocation if resume and resume.relocation else "нет")
    trips = BUSINESS_TRIP_LABELS[prefs.business_trips]
    lines.append(
        f"**Формат:** {_join(formats)} · **Занятость:** {_join(employment)} · **Переезд:** {relocation} · "
        f"**Командировки:** {trips}"
    )
    if resume and resume.full_name:
        lines.append(f"**Имя:** {resume.full_name}")
    if resume and resume.professional_roles:
        lines.append(f"**Профессиональные роли (hh):** {_join(resume.professional_roles)}")
    if not resume:
        lines.append("")
        lines.append("_Структурные данные резюме hh.ru недоступны; полный текст резюме приведён в конце файла._")
    elif resume.origin == "file":
        lines.append("")
        lines.append(
            "_Резюме hh.ru недоступно через API; навыки, стаж и должность распознаны из загруженного файла, "
            "полный текст приведён в конце._"
        )

    # skills
    lines += ["", "## Ключевые навыки (с весами для отбора)"]
    skills = prefs.merged_skills(resume.skills if resume else [])
    lines.append(_join([f"{s.name} ({s.weight})" for s in skills]) if skills else "—")

    # experience
    lines += ["", "## Опыт работы"]
    if resume and resume.experience:
        for item in resume.experience:
            head = " — ".join(p for p in (item.company, item.position) if p) or "—"
            period = f"{format_month(item.start)} – {format_month(item.end)}"
            duration = months_between(item.start, item.end, today)
            duration_text = f", {months_to_text(duration)}" if duration is not None else ""
            lines.append(f"### {head} ({period}{duration_text})")
            meta = []
            if item.industries:
                meta.append("Отрасль: " + ", ".join(item.industries))
            if item.area:
                meta.append("Регион: " + item.area)
            if meta:
                lines.append("_" + " · ".join(meta) + "_")
            if item.description:
                lines.append(item.description.strip())
            lines.append("")
    else:
        lines.append("—" if not file_text else "_См. полный текст резюме ниже._")

    # education / certificates / languages
    lines += ["", "## Образование, сертификаты, языки"]
    if resume:
        if resume.education_level:
            lines.append(f"Уровень: {resume.education_level}")
        for e in resume.education:
            desc = ", ".join(p for p in (e.name, e.organization, e.result) if p)
            year = f" ({e.year})" if e.year else ""
            kind = "" if e.kind == "primary" else f" [{'доп. образование' if e.kind == 'additional' else 'аттестация'}]"
            if desc:
                lines.append(f"- {desc}{year}{kind}")
        if resume.certificates:
            lines.append("Сертификаты: " + ", ".join(resume.certificates))
    langs = prefs.languages or (resume.languages if resume else [])
    if langs:
        lines.append("Языки: " + "; ".join(langs))
    if resume and resume.citizenship:
        lines.append("Гражданство: " + ", ".join(resume.citizenship))
    if len(lines) and lines[-1] == "## Образование, сертификаты, языки":
        lines.append("—")

    # about
    if resume and resume.about:
        lines += ["", "## О себе (из резюме)", resume.about.strip()]

    # preferences
    lines += ["", "## Предпочтения и ограничения"]
    wants = [w for w in (prefs.wants.strip(),) if w]
    if prefs.target_titles:
        wants.append("целевые должности: " + ", ".join(prefs.target_titles))
    if prefs.target_keywords:
        wants.append("ключевые слова: " + ", ".join(prefs.target_keywords))
    lines.append("- Хочу: " + ("; ".join(wants) if wants else "—"))
    avoids = [a for a in (prefs.avoids.strip(),) if a]
    stop = prefs.stop_words_title + [s for s in prefs.stop_words_description if s not in prefs.stop_words_title]
    if stop:
        avoids.append("стоп-слова: " + ", ".join(stop))
    lines.append("- Не хочу: " + ("; ".join(avoids) if avoids else "—"))
    excluded = []
    if prefs.excluded_employers:
        excluded.append("работодатели: " + ", ".join(prefs.excluded_employers))
    if prefs.excluded_industries:
        excluded.append("отрасли: " + ", ".join(prefs.excluded_industries))
    lines.append("- Исключённые работодатели/отрасли: " + ("; ".join(excluded) if excluded else "—"))
    constraints = []
    if prefs.salary_min:
        constraints.append(
            f"зарплата от {_money(prefs.salary_min, prefs.salary_currency)} нетто"
            + (" (вакансии без зарплаты пропускать)" if prefs.salary_missing == "skip" else "")
        )
    if prefs.experience_min_years is not None or prefs.experience_max_years is not None:
        lo = f"от {prefs.experience_min_years:g}" if prefs.experience_min_years is not None else ""
        hi = f"до {prefs.experience_max_years:g}" if prefs.experience_max_years is not None else ""
        constraints.append("требуемый опыт " + " ".join(p for p in (lo, hi) if p) + " лет")
    if prefs.no_agencies:
        constraints.append("без кадровых агентств")
    if prefs.accredited_it_only:
        constraints.append("только аккредитованные IT-компании")
    lines.append("- Условия: " + ("; ".join(constraints) if constraints else "—"))
    lines.append("- Факты вне резюме: " + (prefs.extra_facts.strip() or "—"))

    # letter style
    ls = prefs.letter_style
    lines += [
        "",
        "## Стиль писем",
        f"Тон: {ls.tone or '—'} · Длина: {ls.min_chars}–{ls.max_chars} символов · Обращение: {ls.greeting or '—'} · "
        f"Обязательно упоминать: {_join(ls.must_mention)} · Избегать: {_join(ls.avoid)}",
    ]
    if ls.examples:
        lines += ["### Примеры писем, которые нравятся"]
        for i, example in enumerate(ls.examples, 1):
            lines.append(f"**Пример {i}:**")
            lines.extend("> " + line for line in example.strip().splitlines())
            lines.append("")

    # full text from file
    if file_text:
        text = file_text.strip()
        truncated = ""
        if len(text) > FILE_TEXT_MAX_CHARS:
            truncated = f"\n\n[… обрезано, всего {len(text)} символов]"
            text = text[:FILE_TEXT_MAX_CHARS]
        lines += ["", f"## Полный текст резюме (из файла {file_name or ''})".rstrip(), "", text + truncated]

    return "\n".join(lines).rstrip() + "\n"
