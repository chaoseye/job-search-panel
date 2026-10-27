"""Named selection-criteria sets: searches bound to different sets filter differently, profile/letters stay shared."""

from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest
import respx
from httpx import ASGITransport

from app.config import Settings
from app.main import app
from app.runtime import Runtime
from app.services.preferences import CRITERIA_FIELDS, Preferences, apply_criteria, criteria_of
from tests.integration.test_api_m2 import BASE, _full, _items, _login, _mock_dicts


@pytest.fixture
async def api(settings: Settings):
    settings = settings.model_copy(update={"batch_auto_create": False})
    runtime = await Runtime.create(settings)
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


def test_apply_criteria_keeps_profile_fields() -> None:
    base = Preferences(
        stop_words_title=["1С"],
        salary_min=200000,
        wants="продукт",
        extra_facts="выход через 2 недели",
        letter_style={"tone": "живой"},
        skills_weights=[{"name": "Python", "weight": 5}],
    )
    crit = {
        "stop_words_title": ["Django"],
        "salary_min": 100000,
        "wants": "IGNORED",
        "skills_weights": [{"name": "Go", "weight": 4}],
    }
    eff = apply_criteria(base, crit)
    assert eff.stop_words_title == ["Django"] and eff.salary_min == 100000
    assert eff.wants == "продукт" and eff.extra_facts == "выход через 2 недели" and eff.letter_style.tone == "живой"
    assert [s.name for s in eff.skills_weights] == ["Go"]
    assert set(criteria_of(eff)) == set(CRITERIA_FIELDS) and "wants" not in criteria_of(eff)
    # invalid values are validated like the questionnaire
    with pytest.raises(ValueError):
        apply_criteria(base, {"salary_min": -5})


@respx.mock(assert_all_called=False)
async def test_two_searches_two_criteria_sets(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    _mock_dicts(respx_mock)
    prefs = (await client.get("/api/profile/preferences")).json()
    prefs.update({"stop_words_title": ["1С"], "salary_min": 200000, "target_titles": ["Python developer"]})
    assert (await client.put("/api/profile/preferences", json=prefs)).status_code == 200

    # a second set: copied from the questionnaire, then Django is a stop word and salary is not required
    r = await client.post("/api/selection-profiles", json={"name": "Без Django"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    assert r.json()["criteria"]["stop_words_title"] == ["1С"]  # copied from the base questionnaire
    r = await client.put(
        f"/api/selection-profiles/{pid}",
        json={"criteria": {**r.json()["criteria"], "stop_words_title": ["Django"], "salary_min": None}},
    )
    assert r.json()["criteria"]["stop_words_title"] == ["Django"] and r.json()["criteria"]["salary_min"] is None
    assert (await client.post("/api/selection-profiles", json={"name": "  "})).status_code == 400
    listing = (await client.get("/api/selection-profiles")).json()
    assert [p["name"] for p in listing["items"]] == ["Без Django"] and "wants" not in listing["criteria_fields"]

    # search A uses the questionnaire; search B uses the set
    a = (
        await client.post("/api/searches", json={"name": "A", "params": {"text": "python"}, "interval_min": 30})
    ).json()
    b = (
        await client.post(
            "/api/searches",
            json={"name": "B", "params": {"text": "django"}, "interval_min": 30, "selection_profile_id": pid},
        )
    ).json()
    assert (
        a["selection_profile_id"] is None
        and b["selection_profile_id"] == pid
        and b["selection_profile_name"] == "Без Django"
    )

    # A finds 1 (ok) and 2 (Django in title, cheap); B finds 3 (Django in title) and 4 (cheap, no Django)
    items_a = _items(["1", "2"])
    items_a[1]["name"] = "Django developer 2"
    items_b = _items(["3", "4"])
    items_b[0]["name"] = "Django developer 3"
    cheap = {"from": 100000, "to": 120000, "currency": "RUR", "gross": False}
    items_a[1]["salary"] = cheap
    items_b[1]["salary"] = cheap

    def search(request: httpx.Request) -> httpx.Response:
        text = parse_qs(request.url.query.decode()).get("text", [""])[0]
        items = items_a if text == "python" else items_b
        return httpx.Response(200, json={"found": len(items), "pages": 1, "items": items})

    respx_mock.get(f"{BASE}/vacancies").mock(side_effect=search)
    for i, over in (
        ("1", {}),
        ("2", {"salary": cheap, "name": "Django developer 2"}),
        ("3", {"name": "Django developer 3"}),
        ("4", {"salary": cheap}),
    ):
        respx_mock.get(f"{BASE}/vacancies/{i}").mock(return_value=httpx.Response(200, json=_full(i, **over)))

    assert (await client.post("/api/jobs/scan")).status_code == 200
    await runtime.jobs.wait("scan")
    rows = {v["hh_id"]: v for v in (await client.get("/api/vacancies", params={"group": "all"})).json()["items"]}
    assert rows["1"]["pipeline_status"] == "prescored" and rows["1"]["selection_profile_id"] is None
    assert rows["2"]["pipeline_status"] == "filtered_out"  # base set: salary 200k required (Django allowed)
    assert "зарплат" in rows["2"]["pipeline_reason"]
    assert rows["3"]["pipeline_status"] == "filtered_out" and rows["3"]["selection_profile_name"] == "Без Django"
    assert "Django" in rows["3"]["pipeline_reason"]
    assert (
        rows["4"]["pipeline_status"] == "prescored" and rows["4"]["selection_profile_id"] == pid
    )  # no salary rule here
    # filter by set
    only_set = (await client.get("/api/vacancies", params={"group": "all", "selection_profile_id": pid})).json()
    assert {v["hh_id"] for v in only_set["items"]} == {"3", "4"}
    only_base = (await client.get("/api/vacancies", params={"group": "all", "selection_profile_id": 0})).json()
    assert {v["hh_id"] for v in only_base["items"]} == {"1", "2"}

    # reprocess uses the vacancy's own set: relax the set → vacancy 3 passes
    r = await client.put(f"/api/selection-profiles/{pid}", json={"criteria": {"stop_words_title": []}})
    assert r.status_code == 200
    assert r.json()["criteria"]["salary_min"] is None  # partial update keeps the set's other overrides
    r = await client.post(f"/api/vacancies/{rows['3']['id']}/reprocess", json={"refetch": False})
    assert r.status_code == 200, r.text
    assert r.json()["pipeline_status"] == "prescored"
    # a vacancy of the base set is untouched by the set change
    r = await client.post(f"/api/vacancies/{rows['2']['id']}/reprocess", json={"refetch": False})
    assert r.json()["pipeline_status"] == "filtered_out"

    # re-binding a search moves the vacancies it found (still on its old set) to the new set
    r = await client.put(f"/api/searches/{a['id']}", json={**a, "selection_profile_id": pid})
    assert r.status_code == 200, r.text
    moved = (await client.get("/api/vacancies", params={"group": "all", "selection_profile_id": pid})).json()
    assert {v["hh_id"] for v in moved["items"]} == {"1", "2", "3", "4"}
    r = await client.put(f"/api/searches/{a['id']}", json={**a, "selection_profile_id": None})
    assert r.status_code == 200, r.text
    back = (await client.get("/api/vacancies", params={"group": "all", "selection_profile_id": 0})).json()
    assert {v["hh_id"] for v in back["items"]} == {"1", "2"}

    # deleting the set: searches and vacancies fall back to the questionnaire
    r = await client.delete(f"/api/selection-profiles/{pid}")
    assert r.json() == {"ok": True, "searches": 1, "vacancies": 2}
    b2 = next(s for s in (await client.get("/api/searches")).json() if s["id"] == b["id"])
    assert b2["selection_profile_id"] is None
    assert (await client.get("/api/vacancies", params={"group": "all", "selection_profile_id": pid})).json()[
        "total"
    ] == 0
    assert (await client.delete(f"/api/selection-profiles/{pid}")).status_code == 404


@respx.mock(assert_all_called=False)
async def test_reprocess_all_with_new_criteria(api, respx_mock: respx.MockRouter) -> None:
    """After tuning the questionnaire, one job re-evaluates every vacancy from stored JSON (no hh calls needed)."""
    client, runtime = api
    await _login(client, respx_mock)
    _mock_dicts(respx_mock)
    prefs = (await client.get("/api/profile/preferences")).json()
    prefs.update({"stop_words_title": [], "salary_min": None, "target_titles": ["Python developer"]})
    assert (await client.put("/api/profile/preferences", json=prefs)).status_code == 200
    await client.post("/api/searches", json={"name": "A", "params": {"text": "python"}, "interval_min": 30})
    items = _items(["1", "2", "3"])
    items[1]["name"] = "Sales manager 2"
    respx_mock.get(f"{BASE}/vacancies").mock(
        return_value=httpx.Response(200, json={"found": 3, "pages": 1, "items": items})
    )
    for i in ("1", "2", "3"):
        respx_mock.get(f"{BASE}/vacancies/{i}").mock(
            return_value=httpx.Response(
                200, json=_full(i, name="Sales manager 2" if i == "2" else f"Python developer {i}")
            )
        )
    assert (await client.post("/api/jobs/scan")).status_code == 200
    await runtime.jobs.wait("scan")
    before = {
        v["hh_id"]: v["pipeline_status"]
        for v in (await client.get("/api/vacancies", params={"group": "all"})).json()["items"]
    }
    assert before == {"1": "prescored", "2": "prescored", "3": "prescored"}

    # tighten: "Sales" becomes a stop word → bulk reprocess without touching hh
    prefs["stop_words_title"] = ["Sales"]
    assert (await client.put("/api/profile/preferences", json=prefs)).status_code == 200
    calls_before = sum(1 for c in respx_mock.calls if "/vacancies" in str(c.request.url))
    r = await client.post("/api/vacancies/reprocess-all", json={})
    assert r.status_code == 200, r.text
    await runtime.jobs.wait("reprocess")
    run = next(x for x in (await client.get("/api/jobs")).json()["runs"] if x["job"] == "reprocess")
    assert run["status"] == "ok" and run["stats"]["processed"] == 3
    assert run["stats"]["by_status"] == {"prescored": 2, "filtered_out": 1}
    calls_after = sum(1 for c in respx_mock.calls if "/vacancies" in str(c.request.url))
    assert calls_after == calls_before  # stored full cards were reused, hh not called
    after = {v["hh_id"]: v for v in (await client.get("/api/vacancies", params={"group": "all"})).json()["items"]}
    assert after["2"]["pipeline_status"] == "filtered_out" and "Sales" in after["2"]["pipeline_reason"]
    assert (await client.post("/api/vacancies/reprocess-all", json={"statuses": ["bogus"]})).status_code == 400

    # «мой стаж» from the questionnaire feeds the experience part of the prescore (file resume gives none)
    v1 = (await client.get(f"/api/vacancies/{after['1']['id']}")).json()
    assert "опыт не сопоставлен" in " ".join(v1["prescore_notes"])
    prefs["own_experience_years"] = 4
    assert (await client.put("/api/profile/preferences", json=prefs)).status_code == 200
    r = await client.post(f"/api/vacancies/{after['1']['id']}/reprocess", json={"refetch": False})
    assert r.status_code == 200, r.text
    v1 = (await client.get(f"/api/vacancies/{after['1']['id']}")).json()
    assert "опыт подходит (От 3 до 6 лет)" in " ".join(v1["prescore_notes"])
    assert v1["prescore"] == after["1"]["prescore"] + 7  # 15/15 instead of the neutral 8/15

    # vacancies already packed for /hh-batch are left alone unless cancel_pending asks to re-evaluate them too
    r = await client.post(
        "/api/profile/resumes/upload", files={"file": ("cv.txt", "Иван, Python, FastAPI".encode(), "text/plain")}
    )
    assert r.status_code == 200
    assert "**Опыт всего:** 4 года" in (await client.get("/api/profile/md")).text  # from the questionnaire
    r = await client.post("/api/batches/create", json={})
    assert r.status_code == 200 and len(r.json()["created"]) == 1
    assert (await client.get("/api/vacancies/summary")).json()["awaiting_claude"] == 2
    prefs["stop_words_title"] = ["Sales", "developer 1"]
    assert (await client.put("/api/profile/preferences", json=prefs)).status_code == 200
    assert (await client.post("/api/vacancies/reprocess-all", json={})).status_code == 200
    await runtime.jobs.wait("reprocess")
    assert (await client.get("/api/vacancies/summary")).json()["awaiting_claude"] == 2
    assert (await client.post("/api/vacancies/reprocess-all", json={"cancel_pending": True})).status_code == 200
    await runtime.jobs.wait("reprocess")
    run = next(x for x in (await client.get("/api/jobs")).json()["runs"] if x["job"] == "reprocess")
    assert run["stats"]["cancelled_batches"] == 1 and run["stats"]["processed"] == 3
    summary = (await client.get("/api/vacancies/summary")).json()
    assert summary["awaiting_claude"] == 0 and summary["filtered_out"] == 2 and summary["prescored"] == 1
    assert (await client.get("/api/batches")).json()["items"][0]["status"] == "cancelled"
