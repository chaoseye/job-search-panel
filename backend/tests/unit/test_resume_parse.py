"""Structure recognised in an hh.ru resume export (PDF text + layout text) — services/resume_parse.py."""

from __future__ import annotations

from app.exchange.profile_md import render_profile_md
from app.services.preferences import Preferences
from app.services.resume_parse import parse_resume_text, total_months

PLAIN = """Иванов Иван
Петрович
Мужчина, 31 год, родился 1 января 1995
+7 (900) 0000000 — предпочитаемый способ связи
Проживает: Казань, м. Кремлёвская
Гражданство: Россия, есть разрешение на работу: Россия
Готов к переезду: Москва, готов к командировкам
Желаемая должность и зарплата
Python-разработчик
180 000 ₽ на руки
Специализации:
— Программист, разработчик
— Аналитик
Тип занятости: полная занятость, частичная занятость
Формат работы: удалённо, гибрид
Опыт работы — 5 лет 3 месяца
Январь 2021 —
настоящее время
ООО Ромашка
Разработчик
Навыки
Знание языков Русский — Родной
Английский — B1 — Средний
Навыки Python FastAPI PostgreSQL Docker Разработка API
Дополнительная информация
Обо мне Пишу сервисы.
"""

LAYOUT = """Навыки

Знание языков         Русский — Родной
                      Английский      — B1 — Средний


Навыки                 Python        FastAPI       PostgreSQL       Docker        Разработка API
                       Git           Claude Code


Дополнительная информация
"""


def test_total_months_variants() -> None:
    assert total_months("Опыт работы — 5 лет 3 месяца") == 63
    assert total_months("Опыт работы — 1 год") == 12
    assert total_months("Опыт работы — 8 месяцев") == 8
    assert total_months("Опыт работы — нет") is None
    assert total_months("без строки") is None


def test_parse_hh_export() -> None:
    r = parse_resume_text(PLAIN, LAYOUT)
    assert r is not None and r.origin == "file" and r.parser_version
    assert r.full_name == "Иванов Иван Петрович" and r.age == 31
    assert r.area == "Казань" and r.citizenship == ["Россия"]
    assert r.title == "Python-разработчик" and r.salary_amount == 180000 and r.salary_currency == "RUR"
    assert r.total_experience_months == 63
    # skills come from the layout text where every skill is a separate run of text
    assert r.skills == ["Python", "FastAPI", "PostgreSQL", "Docker", "Разработка API", "Git", "Claude Code"]
    assert r.languages == ["Русский — Родной", "Английский — B1 — Средний"]
    assert r.relocation == "готов к переезду: Москва" and r.business_trips == "готов к командировкам"
    assert r.work_formats == ["удалённо", "гибрид"] and r.employment_forms == [
        "полная занятость",
        "частичная занятость",
    ]
    assert r.professional_roles == ["Программист, разработчик", "Аналитик"]


def test_parse_without_layout_does_not_guess_skills() -> None:
    r = parse_resume_text(PLAIN, None)
    assert (
        r is not None and r.skills == [] and r.total_experience_months == 63
    )  # a bare space-separated list is ambiguous
    r2 = parse_resume_text("Ключевые навыки: Python, Docker; SQL\nОпыт работы — 2 года", None)
    assert r2 is not None and r2.skills == ["Python", "Docker", "SQL"] and r2.total_experience_months == 24
    assert parse_resume_text("просто текст без структуры", None) is None
    assert parse_resume_text("", None) is None


def test_profile_md_uses_file_structure() -> None:
    r = parse_resume_text(PLAIN, LAYOUT)
    md = render_profile_md(
        version=1,
        generated_at="t",
        resume=r,
        file_text=PLAIN,
        file_name="cv.pdf",
        prefs=Preferences(skills_weights=[{"name": "Python", "weight": 5}]),
    )
    assert "**Опыт всего:** 5 лет 3 мес" in md and "**Имя:** Иванов Иван Петрович" in md
    assert "распознаны из загруженного файла" in md
    assert "Python (5)" in md and "Claude Code (3)" in md  # resume skills get the default weight
    assert "Языки: Русский — Родной; Английский — B1 — Средний" in md
    assert "_См. полный текст резюме ниже._" in md
