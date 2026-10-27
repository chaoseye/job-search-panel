"""Typed search on the «Вакансии» page ignores the case of Russian letters. SQLite's lower() and LIKE fold ASCII
letters only: «аналитик» and «Аналитик» found different vacancies and the region
«москва» found nothing. The panel's connections get a Unicode lower() (db/session.unicode_lower), the filters use
icontains with autoescape, so «%» and «_» of the input are matched literally."""

from __future__ import annotations

import json
from typing import Any

import httpx
from sqlalchemy import text

from app.db.base import utcnow
from app.db.models import Application, Vacancy
from app.db.session import unicode_lower
from app.runtime import Runtime
from tests.integration.test_api_m2 import api  # noqa: F401

__all__ = ["api"]

# hh_id, name, employer, region, work formats
VACANCIES = [
    ("1", "Аналитик данных", "Альфа-Банк", "Москва", ["REMOTE"]),
    ("2", "Старший АНАЛИТИК", "Яндекс", "Нижний Новгород", ["HYBRID"]),
    ("3", "BI-разработчик", "ООО Аналитика", "Москва", ["ON_SITE"]),
    ("4", "Python Developer", "Tinkoff", "Москва", ["REMOTE"]),
    ("5", "Маркетолог 50% удалённо", "Рога_и_Копыта", "Самара", ["REMOTE"]),
    ("6", "Frontend/React разработчик", "Сбер", "Казань", ["HYBRID"]),
]


async def _seed(runtime: Runtime) -> None:
    async with runtime.session_factory() as session:
        for hh_id, name, employer, area, formats in VACANCIES:
            session.add(
                Vacancy(
                    hh_id=hh_id,
                    name=name,
                    employer_name=employer,
                    area_name=area,
                    work_format_ids_json=json.dumps(formats),
                    pipeline_status="prescored",
                )
            )
        await session.commit()


async def _found(client: httpx.AsyncClient, **params: Any) -> set[str]:
    r = await client.get("/api/vacancies", params={"group": "all", "per_page": 200, **params})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == len(body["items"])  # the counter goes through the same filters
    return {v["hh_id"] for v in body["items"]}


async def test_search_and_region_ignore_the_case_of_russian_letters(api) -> None:
    client, runtime = api
    await _seed(runtime)
    # the title («Аналитик данных», «Старший АНАЛИТИК») or the employer («ООО Аналитика»), whatever the case typed
    for q in ("аналитик", "Аналитик", "АНАЛИТИК", "аНаЛиТиК", "  аналитик "):
        assert await _found(client, q=q) == {"1", "2", "3"}, q
    assert await _found(client, q="яндекс") == {"2"}
    assert await _found(client, q="РОГА") == {"5"}
    assert await _found(client, q="удалённо") == {"5"}
    for area in ("москва", "Москва", "МОСКВА"):
        assert await _found(client, area=area) == {"1", "3", "4"}, area
    assert await _found(client, area="новгород") == {"2"}
    assert await _found(client, q="аналитик", area="нижний") == {"2"}
    assert await _found(client, q="  ", area=" ") == {v[0] for v in VACANCIES}  # blank input is no filter


async def test_latin_search_works_as_before(api) -> None:
    client, runtime = api
    await _seed(runtime)
    for q in ("python", "PYTHON", "Python Developer", "developer"):
        assert await _found(client, q=q) == {"4"}, q
    assert await _found(client, q="tinkoff") == {"4"}
    assert await _found(client, q="bi-") == {"3"}
    assert await _found(client, q="frontend/react") == {"6"}  # «/» is the escape character of autoescape


async def test_percent_and_underscore_in_the_input_are_literal(api) -> None:
    client, runtime = api
    await _seed(runtime)
    # «%» used to find every vacancy and «_» any one character
    assert await _found(client, q="%") == {"5"}
    assert await _found(client, q="50%") == {"5"}
    assert await _found(client, q="_") == {"5"}
    assert await _found(client, q="рога_и_") == {"5"}
    assert await _found(client, q="Python_Developer") == set()
    assert await _found(client, q="/") == {"6"}
    assert await _found(client, area="%") == set()
    assert await _found(client, area="_") == set()
    # hh work format ids have «_» of their own (ON_SITE)
    assert await _found(client, work_format="ON_SITE") == {"3"}
    assert await _found(client, work_format="REMOTE") == {"1", "4", "5"}
    assert await _found(client, work_format="%") == set()
    assert await _found(client, work_format="ON%") == set()


async def test_sorting_by_name_ignores_the_case_of_russian_letters(api) -> None:
    """«Отклики» sort by func.lower(employer) — the same lower(): byte order put every capital Cyrillic letter before
    every small one, so «Яндекс» came before «альфа-банк»."""
    client, runtime = api
    async with runtime.session_factory() as session:
        for hh_id, employer in (("1", "Яндекс"), ("2", "альфа-банк"), ("3", "Бета")):
            vacancy = Vacancy(hh_id=hh_id, name=f"Вакансия {hh_id}", employer_name=employer)
            session.add(vacancy)
            await session.flush()
            session.add(Application(vacancy_id=vacancy.id, mode="assist", status="sent", sent_at=utcnow()))
        await session.commit()
    r = await client.get("/api/applications", params={"sort": "employer", "order": "asc"})
    assert r.status_code == 200, r.text
    assert [a["vacancy"]["employer_name"] for a in r.json()["items"]] == ["альфа-банк", "Бета", "Яндекс"]


async def test_lower_on_the_panel_connections(api) -> None:
    _client, runtime = api
    async with runtime.session_factory() as session:
        row = (await session.execute(text("SELECT lower('ПРИВЕТ, Мир! Ёж'), lower(NULL), lower(12)"))).one()
    assert tuple(row) == ("привет, мир! ёж", None, "12")
    assert unicode_lower("Нижний Новгород") == "нижний новгород"
    assert unicode_lower(None) is None
    assert unicode_lower(1.5) == "1.5"
    assert unicode_lower(b"AB") == "ab"
