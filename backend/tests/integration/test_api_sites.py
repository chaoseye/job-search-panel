"""Career sites of chosen companies: the adapters on the sites' real markup (Avito HTML, Sber JSON API, Merlion HTML,
ВкусВилл Bitrix AJAX + map JSON, X5 API for Пятёрочка/Перекрёсток), the location rule (city or remote) with the
questionnaire stop words, the first read as a backfill, later reads → new matches in the feed and in Telegram, vacancies
gone from the site, mass hiring grouped by position, the feed actions and the site settings."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from app.config import Settings
from app.scheduler import build_scheduler
from app.services.company_sites import (
    ADAPTERS,
    SiteVacancyItem,
    is_match,
    location_matches,
    mass_title,
    slugify,
    sync_sites,
)
from app.services.telegram import deliver_pending
from tests.integration.test_api_m6 import TG, api  # noqa: F401

__all__ = ["api"]


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The city chosen for the sites in these tests; Москва (the default SITES_CITIES) is the other city."""
    return settings.model_copy(update={"sites_cities": "Ростов-на-Дону"})


SBER = "https://rabota.sber.ru/public/app-candidate-public-api-gateway/api/v1"
CITY_ID = "4b1f7c2e-9d3a-4e5b-8c6d-0a1b2c3d4e5f"
OLD_SITES = ["avito", "sber", "merlion"]
NEW_SITES = ["vkusvill", "vkusvill_retail", "pyaterochka", "pyaterochka_retail", "perekrestok"]


def avito_html(rows: list[tuple[str, str, str, str, str]]) -> str:
    """rows: (id, title, section, geo, remote «Да»/«Нет») in the markup career.avito.com serves (checked 2026-09-16)."""
    items = "".join(
        f'<div class="vacancies-section__item" data-vacancy-id="{vid}" data-vacancy-section="{section}" '
        f'data-vacancy-geo="{geo}" data-vacancy-team="Команда" data-vacancy-remote="{remote}" data-vacancy-intern="Нет">'
        f'<a href="/vacancies/x/{vid}/" class="vacancies-section__item-link"></a>'
        f'<div class="vacancies-section__item-content"><a href="/vacancies/x/{vid}/" '
        f'class="vacancies-section__item-name">{title}</a></div></div>'
        for vid, title, section, geo, remote in rows
    )
    return f'<html><body><div class="vacancies-section__items">{items}</div></body></html>'


def sber_page(items: list[dict[str, Any]], total: int | None = None) -> dict[str, Any]:
    return {"data": {"vacancies": items, "total": total if total is not None else len(items)}, "success": True}


def sber_item(internal_id: str, title: str, *, city: str = "г Москва", schedule: int = 1) -> dict[str, Any]:
    return {
        "internalId": internal_id,
        "title": title,
        "company": "ПАО Сбербанк",
        "publicationDate": "2026-09-16T11:27:37.000Z",
        "introduction": "Мы занимаемся разработкой платформы. Ищем специалиста.",
        "city": city,
        "region": "",
        "workScheduleId": schedule,
        "publicationId": f"pub-{internal_id}",
    }


def merlion_html(blocks: dict[str, list[tuple[str, str]]]) -> str:
    """blocks: region id → [(vacancy id, title)]; region names come from the filter links above."""
    names = {"1": "Москва", "2": "Ростов&#8209;на&#8209;Дону", "5": "Казань"}
    filters = "".join(f'<a href="#" class="tt6" data-region="{rid}">{names.get(rid, rid)}</a>' for rid in blocks)
    body = "".join(
        f'<div class="ttinfo" data-region="{rid}">'
        + "".join(f'<a href="/vacancy/{vid}" class="mainlink">{title}</a>' for vid, title in rows)
        + "</div>"
        for rid, rows in blocks.items()
    )
    return f"<html><body>{filters}<div class='clear44'></div>{body}</body></html>"


def _mock_sites(
    m: respx.MockRouter,
    *,
    avito: str,
    merlion: str,
    sber_city: list[dict[str, Any]],
    sber_remote: list[dict[str, Any]],
) -> None:
    m.get("https://career.avito.com/vacancies/").mock(return_value=httpx.Response(200, text=avito))
    m.get("https://job.merlion.com/vacancy/").mock(return_value=httpx.Response(200, text=merlion))
    m.get(f"{SBER}/locations", params={"search": "Ростов-на-Дону"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {"id": CITY_ID, "name": "г Ростов-на-Дону", "parents": None},
                    {"id": "other", "name": "автодорога Ростов-на-Дону — Таганрог, 12 км", "parents": "x"},
                ],
            },
        )
    )
    m.get(f"{SBER}/publications", params={"schedules": "4"}).mock(
        return_value=httpx.Response(200, json=sber_page(sber_remote))
    )
    m.get(f"{SBER}/publications", params={"locations": CITY_ID}).mock(
        return_value=httpx.Response(200, json=sber_page(sber_city))
    )


def test_rules_and_slug() -> None:
    cities = ["Ростов-на-Дону"]
    assert location_matches("Москва, Ростов-на-Дону", False, cities)
    assert location_matches("г Ростов-на-Дону", False, cities)
    assert not location_matches("Москва", False, cities) and location_matches("Москва", True, cities)
    assert not location_matches(None, False, cities) and location_matches(None, True, cities)
    item = SiteVacancyItem("1", "Специалист по продажам", "u", "Ростов-на-Дону", None, False)
    assert is_match(item, city_list=cities, stop_words=[], keywords=[])
    assert not is_match(item, city_list=cities, stop_words=["продаж"], keywords=[])  # questionnaire stop word
    dev = SiteVacancyItem("2", "Python-разработчик", "u", "Казань", "удалённо", True)
    assert is_match(dev, city_list=cities, stop_words=["продаж"], keywords=["python"])
    assert not is_match(dev, city_list=cities, stop_words=[], keywords=["java"])
    assert slugify("Системный аналитик") == "sistemnyy-analitik"
    assert slugify("TechLead DevOps/SRE (г. Москва)") == "techlead-devops-sre-g-moskva"
    assert list(ADAPTERS) == OLD_SITES + NEW_SITES
    assert mass_title("Бариста (ул. Пушкинская)") == "Бариста"
    assert mass_title("Водитель курьер (Брянск, ул. Емлютина) (личный автомобиль)") == "Водитель курьер"
    assert mass_title("Сборщик.Пятёрочка") == "Сборщик.Пятёрочка" and mass_title("(x)") == "(x)"
    # the e-mail hh.ru wants in the User-Agent never leaves for other services
    assert Settings(hh_user_agent="JobSearchPanel/1.0 (me@example.com)").public_user_agent == "JobSearchPanel/1.0"
    assert Settings(hh_user_agent="").public_user_agent == "JobSearchPanel/1.0"


@respx.mock(assert_all_called=False)
async def test_sites_backfill_then_new_matches_go_to_the_bot(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    prefs = (await client.get("/api/profile/preferences")).json()
    prefs["stop_words_title"] = ["продаж"]
    assert (await client.put("/api/profile/preferences", json=prefs)).status_code == 200
    _mock_sites(
        respx_mock,
        avito=avito_html(
            [
                ("100", "Backend-разработчик", "Разработка", "Москва, Ростов-на-Дону", "Нет"),
                ("101", "Специалист по продажам", "Продажи", "Ростов-на-Дону", "Да"),
                ("102", "Data Scientist", "Data Science", "Москва", "Нет"),
            ]
        ),
        merlion=merlion_html({"1": [("500", "Бухгалтер")], "2": [("501", "Инженер технической поддержки")]}),
        sber_city=[sber_item("4551052", "Системный аналитик", city="г Ростов-на-Дону")],
        sber_remote=[sber_item("4570309", "Ведущий специалист по обучению нейросетей", city="г Волжский", schedule=4)],
    )

    listing = (await client.get("/api/sites")).json()
    assert [s["key"] for s in listing["items"]] == OLD_SITES + NEW_SITES and listing["cities"] == ["Ростов-на-Дону"]
    assert {"job.goldapple.ru", "career.ozon.ru", "job.wb.ru"} <= set(listing["unsupported"])
    for key in NEW_SITES:  # this test is about the first three sites
        assert (await client.put(f"/api/sites/{key}", json={"enabled": False})).status_code == 200

    # the first read is a backfill: everything stored as seen, matches flagged, nothing queued for the bot
    r = await client.post("/api/sites/sync")
    assert r.status_code == 200, r.text
    sites = {s["key"]: s for s in r.json()["items"]}
    assert sites["avito"]["vacancies_count"] == 3 and sites["avito"]["matches"] == 1  # 101 is a stop word, 102 Moscow
    assert sites["sber"]["vacancies_count"] == 2 and sites["sber"]["matches"] == 2
    assert sites["merlion"]["vacancies_count"] == 2 and sites["merlion"]["matches"] == 1
    assert all(
        sites[k]["last_error"] is None and sites[k]["last_new"] == 0 and sites[k]["first_synced_at"] for k in OLD_SITES
    )
    feed = (await client.get("/api/sites/vacancies")).json()
    assert feed["total"] == 4 and feed["new_count"] == 0 and all(v["status"] == "seen" for v in feed["items"])
    everything = (await client.get("/api/sites/vacancies", params={"only_match": "false"})).json()
    assert everything["total"] == 7
    sber_remote = next(v for v in feed["items"] if v["external_id"] == "4570309")
    assert sber_remote["remote"] is True and sber_remote["work_format"] == "удалённо"
    assert sber_remote["url"] == "https://rabota.sber.ru/search/veduschiy-specialist-po-obucheniyu-neyrosetey-4570309/"
    assert (await client.get("/api/notifications", params={"channel": "telegram"})).json() == []

    # a later read: a new remote vacancy at Avito and a new Sber vacancy in Rostov-on-Don → new + Telegram;
    # the Moscow one is not a match; Merlion's 500 disappeared from the site → gone
    _mock_sites(
        respx_mock,
        avito=avito_html(
            [
                ("100", "Backend-разработчик", "Разработка", "Москва, Ростов-на-Дону", "Нет"),
                ("101", "Специалист по продажам", "Продажи", "Ростов-на-Дону", "Да"),
                ("102", "Data Scientist", "Data Science", "Москва", "Нет"),
                ("103", "ML-инженер", "Data Science", "", "Да"),
                ("104", "Юрист", "Юриспруденция и GR", "Москва", "Нет"),
            ]
        ),
        merlion=merlion_html({"2": [("501", "Инженер технической поддержки")]}),
        sber_city=[
            sber_item("4551052", "Системный аналитик", city="г Ростов-на-Дону"),
            sber_item("4600000", "Python-разработчик", city="г Ростов-на-Дону"),
        ],
        sber_remote=[sber_item("4570309", "Ведущий специалист по обучению нейросетей", city="г Волжский", schedule=4)],
    )
    r = await client.post("/api/sites/sync")
    sites = {s["key"]: s for s in r.json()["items"]}
    assert sites["avito"]["last_new"] == 1 and sites["sber"]["last_new"] == 1 and sites["merlion"]["last_new"] == 0
    assert sites["merlion"]["vacancies_count"] == 1
    new = (await client.get("/api/sites/vacancies", params={"only_new": "true"})).json()
    assert {v["title"] for v in new["items"]} == {"ML-инженер", "Python-разработчик"} and new["new_count"] == 2
    assert (await client.get("/api/sites/summary")).json()["new_matches"] == 2
    gone = (await client.get("/api/sites/vacancies", params={"only_match": "false", "include_gone": "true"})).json()
    assert next(v for v in gone["items"] if v["external_id"] == "500")["status"] == "gone"
    assert (await client.get("/api/sites/vacancies", params={"only_match": "false"})).json()["total"] == 9  # no gone

    rows = [
        r
        for r in (await client.get("/api/notifications", params={"channel": "telegram"})).json()
        if r["event_type"] == "site_vacancy"
    ]
    assert len(rows) == 2 and all(r["status"] == "queued" for r in rows)
    assert {r["payload"]["company"] for r in rows} == {"Авито", "ПАО Сбербанк"}
    sent: list[dict[str, Any]] = []
    respx_mock.post(f"{TG}/sendMessage").mock(
        side_effect=lambda request: (sent.append(json.loads(request.content)), httpx.Response(200, json={"ok": True}))[
            1
        ]
    )
    async with runtime.session_factory() as session:
        delivered = await deliver_pending(session, runtime.settings, runtime.http, force=True)
        await session.commit()
    assert delivered["messages"] == 1
    text = sent[-1]["text"]
    assert text.startswith("🏢 <b>Новые вакансии на сайтах компаний: 2</b>")
    assert (
        "ML-инженер</a> — Авито · удалённо" in text
        and "Python-разработчик</a> — ПАО Сбербанк · г Ростов-на-Дону" in text
    )

    # feed actions and site settings
    ml = next(v for v in new["items"] if v["title"] == "ML-инженер")
    assert (await client.post(f"/api/sites/vacancies/{ml['id']}/seen")).json()["status"] == "seen"
    assert (await client.post(f"/api/sites/vacancies/{ml['id']}/hide")).json()["status"] == "hidden"
    assert (await client.get("/api/sites/vacancies")).json()["total"] == 5  # hidden one is out
    assert (await client.post(f"/api/sites/vacancies/{ml['id']}/restore")).json()["status"] == "seen"
    assert (await client.post("/api/sites/vacancies/mark-seen", json={})).json()["marked"] == 2  # + «Юрист» (no match)
    assert (await client.get("/api/sites/summary")).json()["new_matches"] == 0
    r = await client.put("/api/sites/sber", json={"keywords": "python, analyst"})
    assert r.status_code == 200 and r.json()["keywords"] == "python, analyst" and r.json()["matches"] == 1
    r = await client.put("/api/sites/merlion", json={"enabled": False, "notify": False})
    assert r.json()["enabled"] is False and r.json()["notify"] is False
    assert (await client.post("/api/sites/merlion/sync")).status_code == 409
    assert (await client.put("/api/sites/nope", json={"enabled": True})).status_code == 404
    assert (await sync_sites(runtime, site_key="merlion"))["sites"] == 1  # by key regardless of the switch

    # an unreadable site is reported, not raised; the scheduler registers the job
    respx_mock.get("https://career.avito.com/vacancies/").mock(
        return_value=httpx.Response(503, text="<html>js challenge validation</html>")
    )
    r = await client.post("/api/sites/avito/sync")
    assert r.status_code == 200 and "защитой от роботов" in r.json()["last_error"]
    assert "sites_sync" in {j.id for j in build_scheduler(runtime).get_jobs()}


# ---- ВкусВилл (Bitrix), Пятёрочка and Перекрёсток (X5 API) ------------------------------------

VV = "https://vkusvill.ru"
VV_CITY_BOX = "arrFilter_vacancies_1153_2400000001"
X5 = "https://rabota5ka.ru/gw/api/vacancy"
X5_CITY = "692434fa2de34b352c78ff01"
X5_MSK = "692434fa2de34b352c78ff02"


def vv_office_html(rows: list[tuple[str, str, str, str]]) -> str:
    """rows: (id, title, team, city) — the markup /ajax/job/hh_list_filter.php returns (checked 2026-09-16)."""
    return "".join(
        f'<a href="/job/vacancys/{slugify(title)}_{vid}.html" class="VV24_VakansiiOfis_Role__Item js-job-office-vacancy">'
        f'<div class="VV24_VakansiiOfis_Role__Item_Name rtext">{title}</div>'
        f'<div class="VV24_VakansiiOfis_Role__Item_Props"><div class="_team">{team}</div>'
        f'<div class="_city"><span class="_dot">•</span><div>{city}</div></div></div></a>'
        for vid, title, team, city in rows
    )


def vv_map(points: list[tuple[str, str, str, str]]) -> dict[str, Any]:
    """points: (id, title, address, salary) as the map view of the filtered list returns them."""
    return {
        "filtersTagsHtml": "",
        "points": json.dumps(
            [
                {
                    "type": "Feature",
                    "id": int(pid),
                    "properties": {
                        "title": title,
                        "address": address,
                        "salary": salary,
                        "url": f"/job/vacancys/{slugify(title)}_{pid}.html",
                    },
                }
                for pid, title, address, salary in points
            ],
            ensure_ascii=False,
        ),
    }


def x5_office(vid: str, title: str, *, city: str = "Москва", fmt: str = "Гибрид") -> dict[str, Any]:
    return {
        "id": vid,
        "externalId": f"ext-{vid}",
        "name": title,
        "businessUnit": "ТС Пятерочка",
        "city": {"id": X5_CITY if city == "Ростов-на-Дону" else X5_MSK, "name": city},
        "mainResponsobilities": "•\tОбеспечивать регулярный процесс.\n•\tКоммуникация с поставщиками.",
        "workFormat": fmt,
        "schedule": "Пятидневный",
        "createdAt": "2026-03-16T00:00:00.000Z",
        "jobDirection": "Логистика",
    }


def x5_hire(
    rid: str, vacancy_id: str, name: str, *, city: str = "Ростов-на-Дону", salary: tuple[int, int] = (0, 0)
) -> dict[str, Any]:
    return {
        "id": rid,
        "name": name,
        "city": {"id": X5_CITY if city == "Ростов-на-Дону" else X5_MSK, "name": city},
        "vacancy": {"id": vacancy_id, "name": name, "jobDirection": "stores", "syncType": "x5"},
        "address": f"Россия, г {city}",
        "salaryFrom": salary[0],
        "salaryTo": salary[1],
        "schedule": ["full-time"],
    }


def _mock_mass_sites(
    m: respx.MockRouter,
    *,
    vv_office: str,
    vv_points: dict[str, Any],
    x5_office_pages: list[list[dict[str, Any]]],
    x5_hire_pages: dict[str | None, list[list[dict[str, Any]]]],
    seen_agents: list[str],
) -> None:
    def office_page(request: httpx.Request) -> httpx.Response:
        seen_agents.append(request.headers["user-agent"])
        return httpx.Response(
            200,
            text='<html><input type="hidden" name="sessid" id="sessid" value="s3ss1d" /></html>',
            headers={"set-cookie": "PHPSESSID=abc123; path=/"},
        )

    def office_list(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "sessid=s3ss1d" in body and "action=filterVacancies" in body, body
        assert "PHPSESSID=abc123" in request.headers.get("cookie", "")
        assert request.headers.get("x-requested-with") == "XMLHttpRequest"
        return httpx.Response(200, json={"success": "Y", "data": {"vacancies": vv_office, "nav": "", "cities": []}})

    m.get(f"{VV}/job/office/").mock(side_effect=office_page)
    m.post(f"{VV}/ajax/job/hh_list_filter.php").mock(side_effect=office_list)
    m.get(f"{VV}/job/vacancys/", params={VV_CITY_BOX: "Y", "set_filter": "Y", "tab": "map"}).mock(
        return_value=httpx.Response(200, json=vv_points)
    )
    m.get(f"{VV}/job/vacancys/").mock(
        return_value=httpx.Response(
            200,
            text='<form><input type="checkbox" name="arrFilter_vacancies_1153_2400000002" data-title="Москва">'
            f'<input type="checkbox" name="{VV_CITY_BOX}" data-title="Ростов-на-Дону">'
            '<input type="checkbox" name="arrFilter_vacancies_1154_1" data-title="Нахичевань"></form>',
        )
    )

    def x5_office_route(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        items = x5_office_pages[page - 1] if page <= len(x5_office_pages) else []
        return httpx.Response(200, json={"items": items, "totalPages": len(x5_office_pages), "page": page, "limit": 10})

    def x5_hire_route(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert params.get("cityId") == X5_CITY and params.get("radius") == "30000" and params.get("limit") == "100"
        pages = x5_hire_pages[params.get("portals")]
        page = int(params.get("page", "1"))
        items = pages[page - 1] if page <= len(pages) else []
        return httpx.Response(200, json={"items": items, "total": 1000, "totalPages": 10, "page": page, "limit": 100})

    m.get(f"{X5}/office-vacancy").mock(side_effect=x5_office_route)
    m.get(f"{X5}/cities").mock(
        return_value=httpx.Response(
            200, json={"cities": [{"id": X5_MSK, "name": "Москва"}, {"id": X5_CITY, "name": "Ростов-на-Дону"}]}
        )
    )
    m.get(f"{X5}/hire-request").mock(side_effect=x5_hire_route)


@respx.mock(assert_all_called=False)
async def test_office_lists_and_mass_hiring_grouped_by_position(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    prefs = (await client.get("/api/profile/preferences")).json()
    prefs["stop_words_title"] = ["продаж"]
    assert (await client.put("/api/profile/preferences", json=prefs)).status_code == 200
    assert (await client.get("/api/sites")).status_code == 200
    for key in OLD_SITES:
        assert (await client.put(f"/api/sites/{key}", json={"enabled": False})).status_code == 200
    agents: list[str] = []
    hundred = [x5_hire(f"h{n}", "v-kassir", "Продавец-кассир", salary=(60000, 80000)) for n in range(100)]
    _mock_mass_sites(
        respx_mock,
        vv_office=vv_office_html(
            [
                ("100000101", "Lead Product Analyst (удаленно)", "Аналитики", "Москва"),
                ("100000102", "Специалист по санитарной безопасности", "Другие вакансии", "Ростов-на-Дону"),
                ("100000103", "Fullstack-разработчик (Python/FastAPI)", "Другие вакансии", "Москва"),
                ("100000104", "Менеджер по продажам", "Продажи", "Ростов-на-Дону"),
            ]
        ),
        vv_points=vv_map(
            [
                ("1", "Бариста (ул. Пушкинская)", "Ростов-на-Дону", "от 57&nbsp;000&nbsp;₽"),
                ("2", "Бариста (пр. Ворошиловский)", "Ростов-на-Дону", "от 65&nbsp;000&nbsp;₽"),
                ("3", "Бариста (ул. Садовая)", "Ростов-на-Дону", "от 57&nbsp;000&nbsp;₽"),
                ("4", "Ведущий инженер по эксплуатации склада (РЦ)", "Ростов-на-Дону", "до 150&nbsp;000&nbsp;₽"),
            ]
        ),
        x5_office_pages=[
            [x5_office("o1", "Специалист по планированию маршрутов", city="Ростов-на-Дону", fmt="Офис")],
            [x5_office("o2", "Менеджер по планированию", city="Челябинск", fmt="Удалённо")],
        ],
        x5_hire_pages={
            # Пятёрочка: a full page of one position, then the rest of the city and the first foreign row
            None: [
                hundred,
                [
                    x5_hire("h100", "v-kassir", "Продавец-кассир", salary=(65000, 90000)),
                    x5_hire("h101", "v-pekar", "Пекарь.Пятёрочка", salary=(56100, 106500)),
                    x5_hire("h102", "v-pekar", "Пекарь.Пятёрочка", city="Москва"),
                ],
                [x5_hire("h103", "v-sborschik", "Сборщик", city="Москва")],  # never read: the city ended above
            ],
            "cross": [[x5_hire("c1", "v-sklad", "Оператор склада"), x5_hire("c2", "v-sklad", "Оператор склада")]],
        },
        seen_agents=agents,
    )

    r = await client.post("/api/sites/sync")
    assert r.status_code == 200, r.text
    sites = {s["key"]: s for s in r.json()["items"]}
    assert all(sites[k]["last_error"] is None for k in NEW_SITES), {k: sites[k]["last_error"] for k in NEW_SITES}
    assert all(sites[k]["last_checked_at"] is None for k in OLD_SITES)
    assert agents and all("@" not in ua and "(" not in ua for ua in agents), agents  # no e-mail leaves the panel
    # office lists: every city, the rule decides (one in the city, one «удаленно» in the title; sales is a stop word)
    assert sites["vkusvill"]["vacancies_count"] == 4 and sites["vkusvill"]["matches"] == 2
    vv = (await client.get("/api/sites/vacancies", params={"site": "vkusvill"})).json()["items"]
    assert {v["title"] for v in vv} == {"Lead Product Analyst (удаленно)", "Специалист по санитарной безопасности"}
    lead = next(v for v in vv if v["remote"])
    assert lead["external_id"] == "100000101" and lead["snippet"] == "Аналитики" and lead["location"] == "Москва"
    assert lead["url"] == f"{VV}/job/vacancys/lead-product-analyst-udalenno_100000101.html"
    assert sites["pyaterochka"]["vacancies_count"] == 2 and sites["pyaterochka"]["matches"] == 2
    x5 = (await client.get("/api/sites/vacancies", params={"site": "pyaterochka"})).json()["items"]
    chel = next(v for v in x5 if v["location"] == "Челябинск")
    assert chel["remote"] and chel["work_format"] == "Удалённо · Пятидневный" and chel["company"] == "ТС Пятерочка"
    assert chel["url"].startswith("https://rabota5ka.ru/office-vacancies?search=%D0%9C") and chel["published_at"]
    assert chel["snippet"].startswith("• Обеспечивать регулярный процесс. • Коммуникация")
    # mass hiring: one row per position and city, addresses counted, salaries summarised
    assert sites["vkusvill_retail"]["vacancies_count"] == 2 and sites["vkusvill_retail"]["matches"] == 2
    listing = (await client.get("/api/sites/vacancies", params={"site": "vkusvill_retail"})).json()["items"]
    retail = {v["title"]: v for v in listing}
    assert retail["Бариста"]["snippet"] == "массовый найм: адресов 3 · от 57 000 ₽ / от 65 000 ₽"
    assert retail["Бариста"]["url"] == f"{VV}/job/vacancys/barista-ul-pushkinskaya_1.html"
    assert retail["Бариста"]["location"] == "Ростов-на-Дону" and len(retail["Бариста"]["external_id"]) <= 64
    assert retail["Ведущий инженер по эксплуатации склада"]["snippet"] == "массовый найм: адресов 1 · до 150 000 ₽"
    assert sites["pyaterochka_retail"]["vacancies_count"] == 2  # the third page (Moscow only) was never requested
    listing = (await client.get("/api/sites/vacancies", params={"site": "pyaterochka_retail"})).json()["items"]
    five = {v["title"]: v for v in listing}
    assert five["Продавец-кассир"]["snippet"] == "массовый найм: адресов 101 · 60 000 – 90 000 ₽"
    assert five["Продавец-кассир"]["url"] == "https://rabota5ka.ru/vacancy/h0"
    assert five["Пекарь.Пятёрочка"]["snippet"] == "массовый найм: адресов 1 · 56 100 – 106 500 ₽"
    assert five["Пекарь.Пятёрочка"]["external_id"] == f"{X5_CITY}:v-pekar"
    assert sites["perekrestok"]["vacancies_count"] == 1
    perek = (await client.get("/api/sites/vacancies", params={"site": "perekrestok"})).json()["items"][0]
    assert perek["title"] == "Оператор склада" and perek["snippet"] == "массовый найм: адресов 2"
    assert perek["url"] == "https://rabota.perekrestok.ru/vacancies/c1" and perek["company"] == "Перекрёсток"
    assert (await client.get("/api/sites/summary")).json()["new_matches"] == 0  # backfill

    # a later read: a new position appears at Перекрёсток in the city, one address of «Бариста» closes → still one row
    respx_mock.get(f"{X5}/hire-request").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={
                "items": [
                    x5_hire("c1", "v-sklad", "Оператор склада"),
                    x5_hire("c3", "v-kassir", "Кассир", salary=(50000, 70000)),
                ]
                if request.url.params.get("portals") == "cross"
                else [],
                "page": 1,
            },
        )
    )
    respx_mock.get(f"{VV}/job/vacancys/", params={VV_CITY_BOX: "Y", "set_filter": "Y", "tab": "map"}).mock(
        return_value=httpx.Response(
            200,
            json=vv_map(
                [
                    ("2", "Бариста (пр. Ворошиловский)", "Ростов-на-Дону", "от 65&nbsp;000&nbsp;₽"),
                    ("4", "Ведущий инженер по эксплуатации склада (РЦ)", "Ростов-на-Дону", "до 150&nbsp;000&nbsp;₽"),
                ]
            ),
        )
    )
    r = await client.post("/api/sites/sync")
    sites = {s["key"]: s for s in r.json()["items"]}
    assert sites["perekrestok"]["last_new"] == 1 and sites["perekrestok"]["vacancies_count"] == 2
    assert sites["vkusvill_retail"]["last_new"] == 0 and sites["vkusvill_retail"]["vacancies_count"] == 2
    listing = (await client.get("/api/sites/vacancies", params={"site": "vkusvill_retail"})).json()["items"]
    retail = {v["title"]: v for v in listing}
    assert retail["Бариста"]["snippet"] == "массовый найм: адресов 1 · от 65 000 ₽"
    assert retail["Бариста"]["status"] == "seen"
    gone = (
        await client.get("/api/sites/vacancies", params={"site": "pyaterochka_retail", "include_gone": "true"})
    ).json()
    assert {v["status"] for v in gone["items"]} == {"gone"}  # the 5ka portal answered with nothing this time
    new = (await client.get("/api/sites/vacancies", params={"only_new": "true"})).json()
    assert [v["title"] for v in new["items"]] == ["Кассир"] and new["items"][0]["site"].startswith("Перекрёсток")
    rows = [
        r
        for r in (await client.get("/api/notifications", params={"channel": "telegram"})).json()
        if r["event_type"] == "site_vacancy"
    ]
    assert len(rows) == 1 and rows[0]["payload"]["url"] == "https://rabota.perekrestok.ru/vacancies/c3"
