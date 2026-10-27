from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.hh.resume import (
    format_month,
    months_between,
    months_to_text,
    normalize_resume,
    plural_ru,
    resume_to_text,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "hh" / "resume_full.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_normalize_full_resume() -> None:
    n = normalize_resume(load_fixture())
    assert n.hh_id == "0123456789abcdef0123456789abcdef012345"
    assert n.title == "Python-разработчик (backend)"
    assert n.full_name == "Иванов Иван Иванович"
    assert n.age == 31
    assert n.area == "Москва"
    assert n.salary_amount == 250000 and n.salary_currency == "RUR"
    assert n.total_experience_months == 88
    assert n.skills == ["Python", "FastAPI", "PostgreSQL", "Docker"]
    assert n.about.startswith("Люблю")
    assert len(n.experience) == 2
    first, second = n.experience
    assert first.company == "Рога и копыта" and first.position == "Senior Python Developer"
    assert first.start == "2022-03-01" and first.end is None
    assert first.industries == ["Интернет-компания"] and first.area == "Москва"
    assert second.company == "ООО Старт"  # falls back to employer.name
    assert n.education_level == "Высшее"
    assert [e.kind for e in n.education] == ["primary", "additional"]
    assert n.education[0].year == 2017
    assert n.languages == ["Русский — Родной", "Английский — B2 — Средне-продвинутый"]
    assert n.professional_roles == ["Программист, разработчик"]
    assert n.relocation == "не готов к переезду"
    assert n.business_trips == "готов к редким командировкам"
    assert n.work_formats == ["Удалённо", "Гибрид"]
    assert n.employment_forms == ["Полная"]
    assert n.certificates == ["AWS Certified Developer (2023)"]
    assert n.sites == ["https://github.com/ivanov"]
    assert n.citizenship == ["Россия"]
    assert n.status == "published"
    assert n.can_publish_or_update is True
    assert n.alternate_url.endswith("/resume/0123456789abcdef0123456789abcdef012345")


def test_normalize_tolerates_garbage() -> None:
    n = normalize_resume(
        {"id": 5, "salary": "none", "experience": [None, "x", {"position": " dev "}], "skill_set": [{"name": "Go"}]}
    )
    assert n.hh_id == "5"
    assert n.salary_amount is None
    assert [e.position for e in n.experience] == ["dev"]
    assert n.skills == ["Go"]
    assert normalize_resume({}).title is None


def test_plural_and_months() -> None:
    assert plural_ru(1, "год", "года", "лет") == "год"
    assert plural_ru(3, "год", "года", "лет") == "года"
    assert plural_ru(5, "год", "года", "лет") == "лет"
    assert plural_ru(11, "год", "года", "лет") == "лет"
    assert plural_ru(21, "год", "года", "лет") == "год"
    assert months_to_text(88) == "7 лет 4 мес"
    assert months_to_text(24) == "2 года"
    assert months_to_text(0) == "0 мес"
    assert months_to_text(None) == "—"


def test_dates() -> None:
    assert format_month("2022-03-01") == "03.2022"
    assert format_month(None) == "н.в."
    assert format_month("garbage") == "garbage"
    assert months_between("2022-03-01", "2022-06-01") == 3
    assert months_between("2022-03-01", None, today=date(2026, 9, 11)) == 54
    assert months_between(None, None) is None


def test_resume_to_text_contains_key_parts() -> None:
    text = resume_to_text(normalize_resume(load_fixture()))
    assert "Python-разработчик (backend)" in text
    assert "Навыки: Python, FastAPI" in text
    assert "Рога и копыта — Senior Python Developer (03.2022 – н.в.)" in text
    assert "Образование:" in text and "МГУ" in text
