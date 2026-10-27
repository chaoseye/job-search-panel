from __future__ import annotations

from datetime import date

from app.exchange.profile_md import FILE_TEXT_MAX_CHARS, render_profile_md
from app.hh.resume import normalize_resume
from app.services.preferences import LetterStyle, Preferences, SkillWeight
from tests.unit.test_resume_norm import load_fixture


def _prefs(**kw) -> Preferences:
    return Preferences(**kw)


def test_full_profile_sections() -> None:
    resume = normalize_resume(load_fixture())
    prefs = _prefs(
        target_titles=["Backend developer"],
        salary_min=300000,
        work_formats=["REMOTE"],
        employment_forms=["FULL"],
        stop_words_title=["1С"],
        excluded_employers=["ООО Плохие"],
        extra_facts="Могу выйти через 2 недели",
        skills_weights=[SkillWeight(name="Python", weight=5), SkillWeight(name="Kafka", weight=4)],
        letter_style=LetterStyle(must_mention=["опыт с платежами"], examples=["Здравствуйте!\nПример письма."]),
    )
    md = render_profile_md(
        version=3,
        generated_at="2026-09-11T10:00:00+00:00",
        resume=resume,
        file_text=None,
        file_name=None,
        prefs=prefs,
        today=date(2026, 9, 11),
    )
    assert md.startswith("---\nprofile_version: 3\n")
    assert "resume_id: 0123456789abcdef0123456789abcdef012345" in md
    assert "**Должность:** Python-разработчик (backend) · **Опыт всего:** 7 лет 4 мес · **Регион:** Москва" in md
    assert "**Зарплата (нетто):** от 300 000 ₽" in md  # preferences override the resume salary
    assert "**Формат:** удалённо · **Занятость:** полная" in md
    # skills: weights first, resume skills default 3
    assert "Python (5), Kafka (4), Docker (3), FastAPI (3), PostgreSQL (3)" in md
    assert "### Рога и копыта — Senior Python Developer (03.2022 – н.в., 4 года 6 мес)" in md
    assert "### ООО Старт — Python Developer (07.2018 – 02.2022, 3 года 7 мес)" in md
    assert "Сократил p95 latency с 800 до 120 мс." in md
    assert "- МГУ, ВМК, Прикладная математика (2017)" in md
    assert "- Курс по Kubernetes, Slurm (2021) [доп. образование]" in md
    assert "Сертификаты: AWS Certified Developer (2023)" in md
    assert "Языки: Русский — Родной; Английский — B2 — Средне-продвинутый" in md
    assert "## О себе (из резюме)\nЛюблю строить" in md
    assert "- Хочу: целевые должности: Backend developer" in md
    assert "- Не хочу: стоп-слова: 1С" in md
    assert "- Исключённые работодатели/отрасли: работодатели: ООО Плохие" in md
    assert "- Факты вне резюме: Могу выйти через 2 недели" in md
    assert "Обязательно упоминать: опыт с платежами" in md
    assert "**Пример 1:**\n> Здравствуйте!\n> Пример письма." in md
    assert "Полный текст резюме" not in md


def test_file_only_profile_and_truncation() -> None:
    text = "СТРОКА " * 4000  # > FILE_TEXT_MAX_CHARS
    md = render_profile_md(
        version=1,
        generated_at="t",
        resume=None,
        file_text=text,
        file_name="cv.pdf",
        prefs=_prefs(target_titles=["QA"]),
    )
    assert "resume_id: none" in md and "resume_file: cv.pdf" in md
    assert "**Должность:** QA · **Опыт всего:** —" in md
    assert "Структурные данные резюме hh.ru недоступны" in md
    # the questionnaire can supply the total experience a file resume lacks
    md2 = render_profile_md(
        version=1,
        generated_at="t",
        resume=None,
        file_text="cv",
        file_name="cv.pdf",
        prefs=_prefs(target_titles=["QA"], own_experience_years=3.7),
    )
    assert "**Опыт всего:** 3 года 8 мес" in md2
    assert "## Полный текст резюме (из файла cv.pdf)" in md
    assert "[… обрезано, всего" in md
    body = md.split("## Полный текст резюме (из файла cv.pdf)")[1]
    assert len(body) < FILE_TEXT_MAX_CHARS + 200


def test_render_is_deterministic() -> None:
    resume = normalize_resume(load_fixture())
    a = render_profile_md(
        version=1,
        generated_at="t",
        resume=resume,
        file_text=None,
        file_name=None,
        prefs=_prefs(),
        today=date(2026, 1, 1),
    )
    b = render_profile_md(
        version=1,
        generated_at="t",
        resume=resume,
        file_text=None,
        file_name=None,
        prefs=_prefs(),
        today=date(2026, 1, 1),
    )
    assert a == b


def test_preferences_validation() -> None:
    p = Preferences(
        work_formats=["remote", "bogus"],
        employment_forms=["full"],
        target_titles=[" A ", "A", "", "B"],
        skills_weights=[SkillWeight(name="Python", weight=2), SkillWeight(name="python", weight=5)],
        salary_currency="rur",
    )
    assert p.work_formats == ["REMOTE"]
    assert p.employment_forms == ["FULL"]
    assert p.target_titles == ["A", "B"]
    assert [s.name for s in p.skills_weights] == ["Python"]
    assert p.salary_currency == "RUR"
    merged = p.merged_skills(["Python", "Go"])
    assert [(s.name, s.weight) for s in merged] == [("Go", 3), ("Python", 2)]
