from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from app.hh.textclean import html_to_text, sanitize_html
from app.hh.vacancy import merge_full, normalize_vacancy, parse_hh_datetime, to_rur

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "hh" / "vacancy_full.json"
ADDRESS = {
    "city": "Москва",
    "street": "улица Маросейка",
    "building": "2",
    "raw": "Москва, улица Маросейка, 2",
    "metro": {"station_name": "Китай-город", "line_name": "Калужско-Рижская", "station_id": "1.101"},
    "metro_stations": [
        {"station_name": "Китай-город", "line_name": "Калужско-Рижская", "station_id": "1.101"},
        {"station_name": "Лубянка", "line_name": "Сокольническая", "station_id": "1.102"},
    ],
}


def load_full() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def short_item(**overrides) -> dict:
    base = {
        "id": "555001",
        "name": "Senior Python Developer (FastAPI)",
        "area": {"id": "1", "name": "Москва"},
        "salary": {"from": 300000, "to": 400000, "currency": "RUR", "gross": True},
        "employer": {"id": "1455", "name": "Рога и копыта", "trusted": True, "accredited_it_employer": True},
        "published_at": "2026-09-10T10:00:00+0300",
        "archived": False,
        "type": {"id": "open"},
        "experience": {"id": "between3And6", "name": "От 3 до 6 лет"},
        "employment_form": {"id": "FULL", "name": "Полная"},
        "work_format": [{"id": "REMOTE", "name": "Удалённо"}],
        "response_letter_required": True,
        "has_test": False,
        "alternate_url": "https://hh.ru/vacancy/555001",
        "apply_alternate_url": "https://hh.ru/applicant/vacancy_response?vacancyId=555001",
        "relations": [],
        "counters": {"responses": 42},
        "snippet": {"requirement": "Опыт с <highlighttext>Python</highlighttext>", "responsibility": "Писать код"},
    }
    base.update(overrides)
    return base


def test_normalize_full() -> None:
    v = normalize_vacancy(load_full(), full=True)
    assert v.hh_id == "555001" and v.name.startswith("Senior Python")
    assert v.employer_id == "1455" and v.employer_accredited_it is True and v.employer_blacklisted is False
    assert v.area_name == "Москва"
    assert (v.salary_from, v.salary_to, v.salary_currency, v.salary_gross) == (300000, 400000, "RUR", True)
    assert v.salary_net_estimate() == 348000  # 400000 * 0.87
    assert v.experience_id == "between3And6"
    assert v.employment_form_id == "FULL"
    assert v.work_format_ids == ["REMOTE"] and v.is_remote
    assert v.published_at == datetime(2026, 9, 10, 7, 0, tzinfo=UTC)
    assert v.response_letter_required is True and v.has_test is False and v.test_required is False
    assert not v.is_direct
    assert v.key_skills == ["Python", "FastAPI", "PostgreSQL", "Kafka"]
    assert v.professional_roles == ["Программист, разработчик"]
    assert "<script>" not in (v.description_html or "")
    assert "javascript:" not in (v.description_html or "")
    assert "<strong>Python</strong>" in (v.description_html or "")
    assert "• FastAPI, PostgreSQL, Docker" in (v.description_text or "")
    assert "alert(1)" not in (v.description_text or "")
    assert v.responses_count == 42 and v.is_full


def test_normalize_short_and_merge() -> None:
    s = normalize_vacancy(short_item(), full=False)
    assert s.snippet_requirement == "Опыт с Python"
    assert s.description_text is None and not s.is_full
    f = normalize_vacancy(load_full(), full=True)
    f.responses_count = None
    m = merge_full(s, f)
    assert m.is_full and m.snippet_requirement == "Опыт с Python" and m.responses_count == 42
    assert "Python" in m.searchable_text() and "Писать код" in m.searchable_text()


def test_salary_variants() -> None:
    v = normalize_vacancy(
        short_item(salary=None, salary_range={"from": 1000, "to": None, "currency": "USD", "gross": False}), full=False
    )
    assert (v.salary_from, v.salary_currency) == (1000, "USD")
    assert v.salary_net_estimate() == 1000  # no NDFL adjustment for non-RUR
    assert to_rur(1000, "USD", {"USD": 0.0125}) == 80000
    assert to_rur(1000, "XXX", {}) is None
    assert to_rur(500, "RUB", {}) == 500
    v2 = normalize_vacancy(
        short_item(salary={"from": 100000, "to": None, "currency": "RUR", "gross": False}), full=False
    )
    assert v2.salary_net_estimate() == 100000


def test_direct_and_garbage() -> None:
    v = normalize_vacancy(short_item(type={"id": "direct"}, response_url="https://ext.example/apply"), full=False)
    assert v.is_direct
    g = normalize_vacancy(
        {"id": 7, "employer": "bad", "salary": [], "work_format": "x", "published_at": "nope"}, full=True
    )
    assert g.hh_id == "7" and g.employer_name is None and g.work_format_ids == [] and g.published_at is None
    assert parse_hh_datetime("2026-01-01T00:00:00Z") is not None


def test_workplace_address_and_metro() -> None:
    """hh `address`: street with the building (the city is the area), the nearest station first, no duplicates;
    a raw-only address drops the city prefix; remote vacancies without an address give nothing."""
    v = normalize_vacancy(short_item(address=ADDRESS), full=False)
    assert v.address == "улица Маросейка, 2" and v.metro_stations == ["Китай-город", "Лубянка"]
    raw_only = normalize_vacancy(short_item(address={"city": "Казань", "raw": "Казань, улица Баумана, 10"}), full=False)
    assert raw_only.address == "улица Баумана, 10" and raw_only.metro_stations == []
    # hh may leave `city` empty and keep the city only in `raw`: the vacancy area is the fallback
    no_city = normalize_vacancy(
        short_item(area={"id": "88", "name": "Казань"}, address={"city": None, "raw": "Казань, улица Пушкина, 5"}),
        full=False,
    )
    assert no_city.address == "улица Пушкина, 5"
    assert normalize_vacancy(short_item(address=None), full=False).address is None
    # the full card without an address keeps the workplace of the search item
    f = normalize_vacancy(load_full(), full=True)
    assert f.address is None
    m = merge_full(v, f)
    assert m.address == "улица Маросейка, 2" and m.metro_stations == ["Китай-город", "Лубянка"]


def test_migration_backfill_reads_the_place_like_the_parser() -> None:
    """Migration 0019 fills address/metro from the stored hh JSON with its own copy of the reading (a migration must
    not import application code); both must give the same result."""
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "app" / "db" / "migrations" / "versions" / "0019_vacancy_place.py"
    spec = importlib.util.spec_from_file_location("migration_0019", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    samples = [
        short_item(address=ADDRESS),
        short_item(address={"city": "Казань", "raw": "Казань, улица Баумана, 10"}),
        short_item(area={"id": "88", "name": "Казань"}, address={"city": None, "raw": "Казань, улица Пушкина, 5"}),
        short_item(address={"street": "Тверская улица", "building": None, "metro": None, "metro_stations": []}),
        short_item(address=None),
        short_item(address="garbage"),
    ]
    for item in samples:
        v = normalize_vacancy(item, full=False)
        assert module._place(json.dumps(item, ensure_ascii=False)) == (v.address, v.metro_stations), item["address"]
    assert module._place(None) == (None, []) and module._place("not json") == (None, [])


def test_sanitize_and_text() -> None:
    html = '<div onclick="x()">A<br>B</div><a href="https://ok.example">l</a><style>p{}</style><highlighttext>hl</highlighttext>'
    clean = sanitize_html(html)
    assert "onclick" not in clean and "<style>" not in clean and "highlighttext" not in clean
    assert 'rel="noopener noreferrer nofollow"' in clean
    assert html_to_text(html) == "A\nB\nlhl"  # inline elements are joined, blocks and <br> break lines
