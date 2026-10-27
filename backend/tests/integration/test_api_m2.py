"""Searches, scanner job and vacancies tab through the API with mocked hh.ru."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from httpx import ASGITransport

from app.config import Settings
from app.main import app
from app.runtime import Runtime
from app.services import searches as svc
from tests.unit.test_vacancy_norm import load_full, short_item

BASE = "https://api.hh.ru"
DICTS = {
    "currency": [
        {"code": "RUR", "name": "Рубли", "rate": 1, "in_use": True},
        {"code": "USD", "name": "Доллары", "rate": 0.0125, "in_use": True},
    ],
    "experience": [{"id": "noExperience", "name": "Нет опыта"}, {"id": "between3And6", "name": "От 3 до 6 лет"}],
    "vacancy_search_employment_form": [{"id": "FULL", "name": "Полная"}],
    "work_format": [{"id": "REMOTE", "name": "Удалённо"}],
    "work_schedule_by_days": [],
    "vacancy_label": [{"id": "not_from_agency", "name": "Без агентств"}],
    "vacancy_search_fields": [{"id": "name", "name": "в названии"}],
}


@pytest.fixture
async def api(settings: Settings):
    settings = settings.model_copy(update={"scan_max_new_per_run": 3})
    runtime = await Runtime.create(settings)
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


async def _login(client: httpx.AsyncClient, m: respx.MockRouter) -> None:
    m.post(f"{BASE}/token").mock(
        return_value=httpx.Response(200, json={"access_token": "acc", "refresh_token": "ref", "expires_in": 100000})
    )
    m.get(f"{BASE}/me").mock(return_value=httpx.Response(200, json={"id": "1", "is_applicant": True}))
    r = await client.get("/api/auth/hh/login")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    await client.get("/api/auth/hh/callback", params={"code": "c", "state": state})


def _mock_dicts(m: respx.MockRouter) -> None:
    m.get(f"{BASE}/dictionaries").mock(return_value=httpx.Response(200, json=DICTS))
    m.get(f"{BASE}/industries").mock(return_value=httpx.Response(200, json=[{"id": "7", "name": "IT"}]))


def _items(ids: list[str], **overrides) -> list[dict]:
    out = []
    for i in ids:
        item = short_item(id=i, name=f"Python developer {i}", alternate_url=f"https://hh.ru/vacancy/{i}", **overrides)
        out.append(item)
    return out


def _full(i: str, **overrides) -> dict:
    raw = load_full()
    raw["id"] = i
    raw["name"] = f"Python developer {i}"
    raw.update(overrides)
    return raw


async def _create_search(client: httpx.AsyncClient, **params) -> dict:
    body = {
        "name": "Python remote",
        "params": {"text": "python", "work_format": ["REMOTE"], **params},
        "interval_min": 30,
    }
    r = await client.post("/api/searches", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def test_searches_crud_and_options(api) -> None:
    client, _ = api
    s = await _create_search(client, areas=[{"id": "1", "name": "Москва"}])
    assert s["summary"].startswith("«python»") and "Москва" in s["summary"]
    assert (await client.get("/api/searches")).json()[0]["id"] == s["id"]
    r = await client.put(
        f"/api/searches/{s['id']}", json={"name": "Renamed", "params": {"text": "python"}, "interval_min": 15}
    )
    assert r.json()["name"] == "Renamed" and r.json()["interval_min"] == 15
    r = await client.post("/api/searches", json={"name": "bad", "interval_min": 5})
    assert r.status_code == 422
    assert (await client.delete(f"/api/searches/{s['id']}")).json() == {"ok": True}
    assert (await client.get("/api/searches")).json() == []
    assert (await client.post(f"/api/searches/{s['id']}/run")).status_code == 404
    assert (await client.post("/api/jobs/scan")).status_code == 400  # not logged in


@respx.mock(assert_all_called=False)
async def test_options_and_preview(api, respx_mock: respx.MockRouter) -> None:
    client, _ = api
    await _login(client, respx_mock)
    _mock_dicts(respx_mock)
    r = await client.get("/api/searches/options")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["work_format"] == [{"id": "REMOTE", "name": "Удалённо"}]
    assert body["industry"] == [{"id": "7", "name": "IT"}] and body["education"][0]["id"]
    respx_mock.get(f"{BASE}/suggests/areas").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "1", "text": "Москва"}]})
    )
    assert (await client.get("/api/searches/suggest/areas", params={"text": "мос"})).json() == [
        {"id": "1", "name": "Москва"}
    ]
    route = respx_mock.get(f"{BASE}/vacancies").mock(
        return_value=httpx.Response(200, json={"found": 2, "pages": 1, "items": _items(["1", "2"])})
    )
    r = await client.post(
        "/api/searches/preview",
        json={"params": {"text": "python", "areas": [{"id": "1", "name": "Москва"}], "salary": 100000}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["found"] == 2 and len(r.json()["items"]) == 2
    q = parse_qs(urlparse(str(route.calls.last.request.url)).query)
    assert q["text"] == ["python"] and q["area"] == ["1"] and q["per_page"] == ["10"] and q["salary"] == ["100000"]
    assert q["order_by"] == ["publication_time"] and q["responses_count_enabled"] == ["true"] and q["period"] == ["7"]
    # API-level filters and paging: metro stations, length of the working day, a later page with more items
    respx_mock.get(f"{BASE}/vacancies").mock(
        return_value=httpx.Response(
            200,
            json={
                "found": 1,
                "pages": 3,
                "items": [
                    {
                        **_items(["3"])[0],
                        "working_hours": [{"id": "HOURS_8", "name": "8 часов"}],
                        "work_schedule_by_days": [{"id": "FIVE_ON_TWO_OFF", "name": "5/2"}],
                        "address": {
                            "street": "улица Маросейка",
                            "building": "2",
                            "metro_stations": [{"station_name": "Китай-город"}, {"station_name": "Лубянка"}],
                        },
                    }
                ],
            },
        )
    )
    r = await client.post(
        "/api/searches/preview",
        json={
            "params": {
                "text": "тестировщик",
                "areas": [{"id": "1", "name": "Москва"}],
                "metro": [{"id": "1.101", "name": "Китай-город"}, {"id": "1.102", "name": "Лубянка"}],
                "working_hours": ["HOURS_8", "HOURS_6"],
            },
            "page": 2,
            "per_page": 100,
        },
    )
    assert r.status_code == 200, r.text
    q = parse_qs(urlparse(str(route.calls.last.request.url)).query)
    assert q["metro"] == ["1.101", "1.102"] and q["working_hours"] == ["HOURS_8", "HOURS_6"]
    assert q["page"] == ["2"] and q["per_page"] == ["100"]
    item = r.json()["items"][0]
    assert item["address"] == "улица Маросейка 2" and item["metro"] == ["Китай-город", "Лубянка"]
    assert item["working_hours"] == ["8 часов"] and item["work_schedule_by_days"] == ["5/2"] and r.json()["page"] == 2
    assert (
        svc.SearchParams(
            text="тестировщик", metro=[{"id": "1.101", "name": "Китай-город"}], working_hours=["HOURS_8"]
        ).summary()
        == "«тестировщик» · м. Китай-город · HOURS_8"
    )


@respx.mock(assert_all_called=False)
async def test_scan_pipeline(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    _mock_dicts(respx_mock)
    # preferences: skills + stop word + salary
    prefs = (await client.get("/api/profile/preferences")).json()
    prefs.update(
        {
            "stop_words_title": ["1С"],
            "salary_min": 200000,
            "target_titles": ["Python developer"],
            "skills_weights": [{"name": "Python", "weight": 5}, {"name": "FastAPI", "weight": 4}],
        }
    )
    assert (await client.put("/api/profile/preferences", json=prefs)).status_code == 200
    s = await _create_search(client)

    # page 0 -> 3 items, page 1 -> 2 items (one 1С, one already applied); the full fetch is mocked per id
    items0 = _items(["1", "2", "3"])
    items1 = _items(["4", "5"])
    items1[0]["name"] = "Программист 1С"
    items1[1]["relations"] = ["got_response"]
    search_route = respx_mock.get(f"{BASE}/vacancies").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={
                "found": 5,
                "pages": 2,
                "items": items0 if parse_qs(request.url.query.decode()).get("page", ["0"])[0] == "0" else items1,
            },
        )
    )
    for i in ("1", "2", "3"):
        respx_mock.get(f"{BASE}/vacancies/{i}").mock(return_value=httpx.Response(200, json=_full(i)))
    respx_mock.get(f"{BASE}/vacancies/3").mock(
        return_value=httpx.Response(
            200, json=_full("3", salary={"from": 100000, "to": 150000, "currency": "RUR", "gross": False})
        )
    )

    r = await client.post(f"/api/searches/{s['id']}/run")
    assert r.status_code == 200, r.text
    assert (await client.post("/api/jobs/scan")).status_code == 409  # busy
    await runtime.jobs.wait("scan")

    jobs = (await client.get("/api/jobs")).json()
    assert jobs["runs"][0]["status"] == "ok", jobs["runs"][0]
    stats = jobs["runs"][0]["stats"]
    assert (
        stats["searches"][0]["found"] == 5
        and stats["searches"][0]["new"] == 5
        and stats["searches"][0]["requests"] == 2
    )
    assert stats["processed"]["processed"] == 3  # SCAN_MAX_NEW_PER_RUN=3 -> two vacancies stay `new`
    assert stats["processed"]["pending_new"] == 1
    q = parse_qs(urlparse(str(search_route.calls[0].request.url)).query)
    assert q["period"] == ["7"] and q["per_page"] == ["100"] and q["work_format"] == ["REMOTE"]

    listing = (await client.get("/api/vacancies")).json()
    names = {v["hh_id"]: v for v in listing["items"]}
    assert set(names) == {
        "1",
        "2",
    }  # 3 filtered by salary; 4/5 not processed yet (sorted by published desc: 1,2,3 first)
    assert names["1"]["prescore"] >= 35 and names["1"]["pipeline_status"] == "prescored"
    assert names["1"]["flags"] == ["letter_required"] and names["1"]["is_new"] is True
    assert listing["counts"]["candidates"] == 2 and listing["counts"]["unseen_candidates"] == 2
    assert listing["counts"]["by_status"].get("new") == 2

    filtered = (await client.get("/api/vacancies", params={"group": "filtered_out"})).json()
    assert [v["hh_id"] for v in filtered["items"]] == ["3"]
    assert "зарплата ниже минимума" in filtered["items"][0]["pipeline_reason"]

    # second run: incremental window uses date_from, dedup keeps 5 rows, remaining `new` get processed
    search = (await client.get("/api/searches")).json()[0]
    assert search["last_found"] == 5 and search["last_new"] == 5 and search["last_seen_published_at"]
    respx_mock.get(f"{BASE}/vacancies/4").mock(return_value=httpx.Response(200, json=_full("4")))
    respx_mock.get(f"{BASE}/vacancies/5").mock(return_value=httpx.Response(200, json=_full("5")))
    await client.post(f"/api/searches/{s['id']}/run")
    await runtime.jobs.wait("scan")
    q = parse_qs(urlparse(str(search_route.calls[-1].request.url)).query)
    assert "date_from" in q and "period" not in q
    assert (await client.get("/api/vacancies/summary")).json()["total"] == 5
    stats = (await client.get("/api/jobs")).json()["runs"][0]["stats"]
    assert stats["searches"][0]["new"] == 0
    listing = (await client.get("/api/vacancies", params={"group": "all"})).json()
    by_id = {v["hh_id"]: v["pipeline_status"] for v in listing["items"]}
    assert by_id["4"] == "filtered_out" and by_id["5"] == "already_applied"
    assert by_id["1"] == "prescored" and by_id["2"] == "prescored"

    # detail + actions
    vid = names["1"]["id"]
    detail = (await client.get(f"/api/vacancies/{vid}")).json()
    assert "<strong>Python</strong>" in detail["description_html"]
    assert {"name": "Python", "matched": True} in detail["key_skills"]
    assert {"name": "Kafka", "matched": False} in detail["key_skills"]
    assert detail["prescore_notes"][0].startswith("итого")
    assert (await client.post(f"/api/vacancies/{vid}/skip", json={"reason": "не моё"})).json()[
        "pipeline_status"
    ] == "skipped"
    assert (await client.get("/api/vacancies")).json()["total"] == 1
    assert (await client.post(f"/api/vacancies/{vid}/restore")).json()["pipeline_status"] == "prescored"
    r = await client.post(f"/api/vacancies/{vid}/snooze", json={"days": 2})
    assert r.json()["snoozed_until"]
    assert (await client.get("/api/vacancies")).json()["total"] == 1
    assert (await client.get("/api/vacancies", params={"include_snoozed": "true"})).json()["total"] == 2
    await client.post(f"/api/vacancies/{vid}/unsnooze")
    assert (await client.post("/api/vacancies/mark-seen", json={})).json()["marked"] == 2
    assert (await client.get("/api/vacancies/summary")).json()["unseen_candidates"] == 0

    # hide employer -> both candidates hidden, employer listed, unhide works
    r = await client.post(f"/api/vacancies/{vid}/hide-employer")
    assert r.json()["affected"] >= 1
    assert (await client.get("/api/vacancies")).json()["total"] == 0
    hidden = (await client.get("/api/vacancies/hidden-employers")).json()
    assert hidden[0]["employer_id"] == "1455"
    # unhiding brings back the vacancies hidden together with the employer (not those hidden one by one)
    assert (await client.delete(f"/api/vacancies/hidden-employers/{hidden[0]['id']}")).json() == {
        "ok": True,
        "restored": 2,
    }
    assert (await client.get("/api/vacancies")).json()["total"] == 2
    # restore both vacancies -> user_override protects them from rules on reprocess
    assert (await client.post(f"/api/vacancies/{vid}/restore")).json()["user_override"] is True
    assert (await client.post(f"/api/vacancies/{names['2']['id']}/restore")).json()["pipeline_status"] == "prescored"
    r = await client.post(f"/api/vacancies/{vid}/reprocess")
    assert r.status_code == 200 and r.json()["pipeline_status"] == "prescored"

    # filters
    assert (await client.get("/api/vacancies", params={"q": "developer 2"})).json()["total"] == 1
    assert (await client.get("/api/vacancies", params={"work_format": "REMOTE"})).json()["total"] == 2
    assert (await client.get("/api/vacancies", params={"salary_from": 400000})).json()["total"] == 0
    assert (await client.get("/api/vacancies", params={"salary_from": 300000})).json()["total"] == 2
    assert (await client.get("/api/vacancies", params={"search_id": s["id"]})).json()["total"] == 2
    # required experience: any of the listed hh levels (both fixtures ask for 3–6 years)
    assert (await client.get("/api/vacancies", params={"experience": "between3And6"})).json()["total"] == 2
    assert (await client.get("/api/vacancies", params={"experience": "noExperience,between1And3"})).json()["total"] == 0
    assert (await client.get("/api/vacancies", params={"experience": "between1And3,between3And6"})).json()["total"] == 2
    assert (await client.get("/api/vacancies", params={"experience": "bogus"})).status_code == 400
    assert (await client.get("/api/vacancies", params={"group": "bogus"})).status_code == 400


@respx.mock(assert_all_called=False)
async def test_watch_only_search_notifies_without_evaluation(api, respx_mock: respx.MockRouter) -> None:
    """A watch-only search tracks a role the questionnaire is not about: new vacancies are parked as `watched`
    (no rules — «стажёр» is a stop word — no prescore, no packets) and go to the bot as links."""
    client, runtime = api
    await _login(client, respx_mock)
    _mock_dicts(respx_mock)
    prefs = (await client.get("/api/profile/preferences")).json()
    prefs["stop_words_title"] = ["стажёр"]
    assert (await client.put("/api/profile/preferences", json=prefs)).status_code == 200
    r = await client.post(
        "/api/searches",
        json={
            "name": "QA: тестировщик",
            "params": {"text": "тестировщик", "areas": [{"id": "1", "name": "Москва"}]},
            "interval_min": 60,
            "watch_only": True,
        },
    )
    assert r.status_code == 200 and r.json()["watch_only"] is True
    s = r.json()
    respx_mock.get(f"{BASE}/vacancies").mock(
        return_value=httpx.Response(
            200,
            json={
                "found": 2,
                "pages": 1,
                "items": _items(
                    ["1", "2"],
                    salary={"from": 90000, "to": None, "currency": "RUR", "gross": False},
                    address={
                        "city": "Москва",
                        "street": "Покровка",
                        "building": "10",
                        "metro": {"station_name": "Чистые пруды"},
                        "metro_stations": [{"station_name": "Чистые пруды"}, {"station_name": "Тургеневская"}],
                    },
                ),
            },
        )
    )
    assert (await client.post(f"/api/searches/{s['id']}/run")).status_code == 200
    await runtime.jobs.wait("scan")
    rows = (await client.get("/api/vacancies", params={"group": "watched"})).json()
    assert rows["total"] == 2 and all(v["pipeline_status"] == "watched" for v in rows["items"])
    assert "QA: тестировщик" in rows["items"][0]["pipeline_reason"]
    # the workplace is shown next to the vacancy: street and metro stations, the nearest first
    assert rows["items"][0]["address"] == "Покровка, 10"
    assert rows["items"][0]["metro"] == ["Чистые пруды", "Тургеневская"]
    assert (await client.get("/api/vacancies")).json()["total"] == 0  # not a candidate: no rules, no packets
    assert (await client.get("/api/batches")).json()["items"] == []

    def search_events() -> list[dict]:
        return [e for e in ui_events if e["event_type"] == "search_new"]

    # the first run is a backfill: stored, nothing announced (the bot is not configured in this fixture, so the
    # events of later runs land in the UI feed — Telegram rows need a token)
    ui_events = (await client.get("/api/notifications", params={"channel": "ui"})).json()
    assert search_events() == []
    respx_mock.get(f"{BASE}/vacancies").mock(
        return_value=httpx.Response(
            200,
            json={
                "found": 3,
                "pages": 1,
                "items": _items(
                    ["1", "2", "3"],
                    salary={"from": 90000, "to": None, "currency": "RUR", "gross": False},
                    address={
                        "city": "Москва",
                        "raw": "Москва, Лубянка",
                        "metro": {"station_name": "Лубянка"},
                    },
                ),
            },
        )
    )
    assert (await client.post(f"/api/searches/{s['id']}/run")).status_code == 200
    await runtime.jobs.wait("scan")
    ui_events = (await client.get("/api/notifications", params={"channel": "ui"})).json()
    events = search_events()
    assert len(events) == 1 and events[0]["status"] == "sent"
    third = events[0]["payload"]
    assert third["title"] == "Python developer 3" and third["salary_from"] == 90000 and third["hh_id"] == "3"
    assert third["search"] == "QA: тестировщик" and third["url"] == "https://hh.ru/vacancy/3"
    assert third["metro"] == ["Лубянка"] and third["address"] == "Лубянка"  # the bot line says «м. Лубянка»
    assert (await client.get("/api/vacancies", params={"group": "watched"})).json()["total"] == 3
    # switching the flag off returns the search to the normal pipeline for what it finds next
    r = await client.put(f"/api/searches/{s['id']}", json={**s, "watch_only": False})
    assert r.status_code == 200 and r.json()["watch_only"] is False


@respx.mock(assert_all_called=False)
async def test_reprocess_keeps_verdicts_and_assist_flow_and_scopes_to_the_set(
    api, respx_mock: respx.MockRouter
) -> None:
    """A criteria change re-runs rules/prescore only for vacancies of the changed set and never overwrites a
    Claude verdict or an assist application waiting for the user (2026-09-17: a stop word added to a new set
    re-packed 39 base-set vacancies, three of them mid-application)."""
    from sqlalchemy import select

    from app.db.models import Application, Vacancy, VacancyEvaluation
    from app.services.scanner import reprocess_all

    client, runtime = api
    await _login(client, respx_mock)
    _mock_dicts(respx_mock)
    base_search = await _create_search(client)
    r = await client.post("/api/selection-profiles", json={"name": "Тестирование"})
    set_id = r.json()["id"]
    set_search = await _create_search(client, text="qa")
    r = await client.put(
        f"/api/searches/{set_search['id']}",
        json={"name": "QA", "params": {"text": "qa"}, "interval_min": 30, "selection_profile_id": set_id},
    )
    assert r.status_code == 200 and r.json()["selection_profile_id"] == set_id

    def items_for(request: httpx.Request) -> httpx.Response:
        ids = ["1", "2"] if "python" in str(request.url) else ["3"]
        return httpx.Response(200, json={"found": len(ids), "pages": 1, "items": _items(ids)})

    respx_mock.get(f"{BASE}/vacancies").mock(side_effect=items_for)
    for i in ("1", "2", "3"):
        respx_mock.get(f"{BASE}/vacancies/{i}").mock(return_value=httpx.Response(200, json=_full(i)))
    assert (await client.post("/api/jobs/scan")).status_code == 200
    await runtime.jobs.wait("scan")
    rows = {v["hh_id"]: v for v in (await client.get("/api/vacancies", params={"group": "all"})).json()["items"]}
    assert {rows[i]["pipeline_status"] for i in ("1", "2", "3")} == {"prescored"}
    assert rows["3"]["selection_profile_id"] == set_id and rows["1"]["selection_profile_id"] is None

    async with runtime.session_factory() as session:
        v1 = (await session.execute(select(Vacancy).where(Vacancy.hh_id == "1"))).scalar_one()
        v2 = (await session.execute(select(Vacancy).where(Vacancy.hh_id == "2"))).scalar_one()
        v3 = (await session.execute(select(Vacancy).where(Vacancy.hh_id == "3"))).scalar_one()
        # 1: Claude said skip → not_suitable comes from the verdict, not from the rules
        session.add(VacancyEvaluation(vacancy_id=v1.id, batch_id="b1", score=40, verdict="skip", summary="нет"))
        v1.score = 40
        v1.pipeline_status = "not_suitable"
        v1.pipeline_reason = "оценка Claude 40"
        # 2: the apply page is open, the user has not confirmed yet
        session.add(Application(vacancy_id=v2.id, mode="assist", status="needs_manual", prev_status="suitable"))
        v2.pipeline_status = "needs_manual"
        v2.pipeline_reason = "откройте страницу отклика на hh.ru и подтвердите отправку"
        # 3 (the set): parked below the threshold by old rules
        v3.pipeline_status = "not_suitable"
        v3.pipeline_reason = "старые правила"
        await session.commit()

    # the base questionnaire changed: only base vacancies are candidates, and both of them are kept
    summary = await reprocess_all(runtime, scoped=True, profile_id=None)
    assert summary["processed"] == 0 and summary["kept_evaluated"] == 1 and summary["kept_assist"] == 1
    rows = {v["hh_id"]: v for v in (await client.get("/api/vacancies", params={"group": "all"})).json()["items"]}
    assert rows["1"]["pipeline_status"] == "not_suitable" and rows["1"]["pipeline_reason"] == "оценка Claude 40"
    assert rows["2"]["pipeline_status"] == "needs_manual"
    assert rows["3"]["pipeline_status"] == "not_suitable"  # another set, out of scope
    # the set changed: its vacancy is re-evaluated, the base ones are not touched
    summary = await reprocess_all(runtime, scoped=True, profile_id=set_id)
    assert summary["processed"] == 1 and summary["kept_evaluated"] == 0
    rows = {v["hh_id"]: v for v in (await client.get("/api/vacancies", params={"group": "all"})).json()["items"]}
    assert rows["3"]["pipeline_status"] == "prescored" and rows["1"]["pipeline_status"] == "not_suitable"
    # the manual «переоценить всё» still spans every set, but keeps verdicts and open applications too
    summary = await reprocess_all(runtime)
    assert summary["kept_evaluated"] == 1 and summary["kept_assist"] == 1 and summary["processed"] == 1
    assert base_search["id"] != set_search["id"]


@respx.mock(assert_all_called=False)
async def test_scan_tests_and_direct_follow_apply_mode(api, respx_mock: respx.MockRouter) -> None:
    """A vacancy with a required test (or an external form) is parked in `needs_manual` only when the panel
    applies through the API; in «Ассистент» mode the user applies by hand anyway, so it goes to Claude."""
    client, runtime = api
    await _login(client, respx_mock)
    _mock_dicts(respx_mock)
    s = await _create_search(client)
    respx_mock.get(f"{BASE}/vacancies").mock(
        return_value=httpx.Response(200, json={"found": 2, "pages": 1, "items": _items(["1", "2"])})
    )
    respx_mock.get(f"{BASE}/vacancies/1").mock(
        return_value=httpx.Response(200, json=_full("1", test={"required": True}, has_test=True))
    )
    respx_mock.get(f"{BASE}/vacancies/2").mock(
        return_value=httpx.Response(
            200, json=_full("2", type={"id": "direct", "name": "Прямая"}, response_url="https://employer.example/x")
        )
    )
    # API mode: the test parks the vacancy, the external form is skipped (auto policy for API)
    await client.put("/api/settings", json={"apply_mode_override": "api"})
    await client.post(f"/api/searches/{s['id']}/run")
    await runtime.jobs.wait("scan")
    rows = {v["hh_id"]: v for v in (await client.get("/api/vacancies", params={"group": "all"})).json()["items"]}
    assert rows["1"]["pipeline_status"] == "needs_manual" and "тест" in rows["1"]["pipeline_reason"]
    assert (
        rows["2"]["pipeline_status"] == "filtered_out" and "direct" in rows["2"]["pipeline_reason"]
    )  # DIRECT_POLICY=auto
    # «Ассистент»: re-evaluation sends them into the normal pipeline, flags stay on the card
    await client.put("/api/settings", json={"apply_mode_override": "assist"})
    assert (await client.post("/api/vacancies/reprocess-all", json={})).status_code == 200
    await runtime.jobs.wait("reprocess")
    rows = {v["hh_id"]: v for v in (await client.get("/api/vacancies", params={"group": "all"})).json()["items"]}
    assert rows["1"]["pipeline_status"] == "prescored" and "test_required" in rows["1"]["flags"]
    assert rows["2"]["pipeline_status"] == "prescored" and "direct" in rows["2"]["flags"]


async def test_scan_records_search_error_and_continues(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    _mock_dicts(respx_mock)
    bad = await _create_search(client, text="bad")
    good = await _create_search(client, text="good")
    respx_mock.get(f"{BASE}/vacancies").mock(
        side_effect=lambda request: (
            httpx.Response(400, json={"errors": [{"type": "bad_argument", "value": "text"}]})
            if "bad" in request.url.query.decode()
            else httpx.Response(200, json={"found": 1, "pages": 1, "items": _items(["9"])})
        )
    )
    respx_mock.get(f"{BASE}/vacancies/9").mock(return_value=httpx.Response(200, json=_full("9")))
    await client.post("/api/jobs/scan")
    await runtime.jobs.wait("scan")
    rows = {s["id"]: s for s in (await client.get("/api/searches")).json()}
    assert "bad_argument" in rows[bad["id"]]["last_error"]
    assert rows[good["id"]]["last_error"] is None and rows[good["id"]]["last_new"] == 1
    run = (await client.get("/api/jobs")).json()["runs"][0]
    assert run["status"] == "ok" and run["stats"]["errors"] == 1


@respx.mock(assert_all_called=False)
async def test_scan_paused_on_captcha(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    _mock_dicts(respx_mock)
    await _create_search(client)
    respx_mock.get(f"{BASE}/vacancies").mock(
        return_value=httpx.Response(
            403,
            json={
                "errors": [
                    {
                        "type": "captcha_required",
                        "value": "captcha_required",
                        "captcha_url": "https://hh.ru/account/captcha?state=1",
                    }
                ]
            },
        )
    )
    await client.post("/api/jobs/scan")
    await runtime.jobs.wait("scan")
    jobs = (await client.get("/api/jobs")).json()
    assert jobs["paused_reason"] == "captcha"
    assert "captcha" in json.dumps(jobs["runs"][0]["stats"])
    settings_view = (await client.get("/api/settings")).json()
    assert settings_view["captcha_url"].startswith("https://hh.ru/account/captcha?state=1&backurl=")
    # while paused the next scan is a no-op
    await client.post("/api/jobs/scan")
    await runtime.jobs.wait("scan")
    assert (await client.get("/api/jobs")).json()["runs"][0]["stats"]["skipped"].startswith("paused")
