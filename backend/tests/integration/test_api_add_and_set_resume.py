"""Vacancies added by hh.ru links (past the rules, straight into a packet) and a criteria set with its own resume
(profile_set<id>.md for Claude, own skills for the prescore)."""

from __future__ import annotations

import json

import httpx
import respx
from sqlalchemy import select

from app.db.models import Vacancy
from app.services.manual_add import parse_vacancy_ids
from tests.integration.test_api_m2 import BASE, DICTS, _full, _items, _login, _mock_dicts
from tests.integration.test_api_selection import api  # noqa: F401

__all__ = ["api"]


def test_parse_vacancy_ids() -> None:
    text = """https://hh.ru/vacancy/123450001?from=search
    https://nn.hh.ru/vacancy/123450002/ , 123450003; https://hh.ru/employer/12345 просто текст
    https://hh.ru/vacancy/123450001"""
    assert parse_vacancy_ids(text) == ["123450001", "123450002", "123450003"]
    assert parse_vacancy_ids("нет ссылок") == []


@respx.mock(assert_all_called=False)
async def test_add_by_links_goes_past_rules_into_a_packet(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    _mock_dicts(respx_mock)
    r = await client.post(
        "/api/profile/resumes/upload", files={"file": ("cv.txt", "Иван, Python, FastAPI".encode(), "text/plain")}
    )
    assert r.status_code == 200
    prefs = (await client.get("/api/profile/preferences")).json()
    prefs.update({"stop_words_title": ["1С"], "target_titles": ["Python developer"]})
    assert (await client.put("/api/profile/preferences", json=prefs)).status_code == 200
    # 1 — a normal card; 2 — a stop word in the title (rules would drop it); 3 — archived; 4 — gone
    respx_mock.get(f"{BASE}/vacancies/1").mock(return_value=httpx.Response(200, json=_full("1")))
    respx_mock.get(f"{BASE}/vacancies/2").mock(return_value=httpx.Response(200, json=_full("2", name="Программист 1С")))
    respx_mock.get(f"{BASE}/vacancies/3").mock(return_value=httpx.Response(200, json=_full("3", archived=True)))
    respx_mock.get(f"{BASE}/vacancies/4").mock(return_value=httpx.Response(404, json={"errors": []}))

    text = "https://hh.ru/vacancy/1?from=share https://nn.hh.ru/vacancy/2/ hh.ru/vacancy/3 https://hh.ru/vacancy/4 мусор 12"
    r = await client.post("/api/vacancies/add", json={"text": text})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [a["hh_id"] for a in body["added"]] == ["1", "2"]
    assert all(a["status"] == "awaiting_claude" or a["status"] == "prescored" for a in body["added"])
    assert body["batch_ids"] and len(body["batch_ids"]) == 1
    assert [e["url"] for e in body["errors"]] == ["3", "4"]
    assert "архиве" in body["errors"][0]["error"] and "не найдена" in body["errors"][1]["error"]
    rows = {v["hh_id"]: v for v in (await client.get("/api/vacancies", params={"group": "all"})).json()["items"]}
    assert rows["1"]["pipeline_status"] == "awaiting_claude" and rows["2"]["pipeline_status"] == "awaiting_claude"
    assert rows["2"]["user_override"] is True
    packet_path = runtime.settings.exchange_dir / "pending" / f"batch_{body['batch_ids'][0]}.json"
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert sorted(t["vacancy"]["id"] for t in packet["tasks"]) == ["1", "2"]
    # adding again: already in a packet
    r = await client.post("/api/vacancies/add", json={"text": "https://hh.ru/vacancy/1"})
    assert r.json()["added"] == [] and "уже в пакете" in r.json()["errors"][0]["error"]
    # no ids at all
    r = await client.post("/api/vacancies/add", json={"text": "просто текст без ссылок"})
    assert r.json()["added"] == [] and "не нашёл id" in r.json()["errors"][0]["error"]


@respx.mock(assert_all_called=False)
async def test_criteria_set_with_its_own_resume(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    _mock_dicts(respx_mock)
    exchange = runtime.settings.exchange_dir
    # primary resume + an additional one that must NOT become primary
    r = await client.post(
        "/api/profile/resumes/upload",
        files={"file": ("main.txt", "Иван, Python\nКлючевые навыки: Python, FastAPI".encode(), "text/plain")},
    )
    assert r.status_code == 200
    r = await client.post(
        "/api/profile/resumes/upload?make_primary=false",
        files={"file": ("go.txt", "Иван, Go\nКлючевые навыки: Go, Rust\nОпыт работы — 2 года".encode(), "text/plain")},
    )
    assert r.status_code == 200
    resumes = r.json()["resumes"]
    assert [x["file_name"] for x in resumes if x["is_primary"]] == ["main.txt"]
    second = next(x for x in resumes if x["file_name"] == "go.txt")
    assert r.json()["set_profiles"] == []

    # a set bound to the second resume gets its own profile file
    pid = (await client.post("/api/selection-profiles", json={"name": "Go"})).json()["id"]
    r = await client.put(f"/api/selection-profiles/{pid}", json={"resume_id": second["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["resume_name"] == "go.txt" and r.json()["profile_file"] == f"profile_set{pid}.md"
    view = (await client.get("/api/profile")).json()
    assert view["set_profiles"][0]["file"] == f"profile_set{pid}.md" and view["set_profiles"][0]["exists"]
    md = (exchange / f"profile_set{pid}.md").read_text(encoding="utf-8")
    assert "Go, Rust" in md and "Rust (3)" in md and "**Опыт всего:** 2 года" in md
    main_md = (exchange / "profile.md").read_text(encoding="utf-8")
    assert "Rust" not in main_md

    # the scanner context of that set uses the second resume's skills
    from app.services.scanner import build_context

    async with runtime.session_factory() as session:
        ctx = await build_context(session, runtime.hh_client(), runtime.settings, profile_id=pid)
        assert {w.name for w in ctx.weights} >= {"Go", "Rust"} and ctx.candidate_months == 24
        base = await build_context(session, runtime.hh_client(), runtime.settings)
        assert "Rust" not in {w.name for w in base.weights}

    # a search on the set → its vacancy's task points at the set's profile file (the set's own weights make the
    # fixture vacancy pass the prescore threshold; the resume skills are merged on top)
    r = await client.put(
        f"/api/selection-profiles/{pid}",
        json={"criteria": {"skills_weights": [{"name": "Python", "weight": 5}], "target_titles": ["Python developer"]}},
    )
    assert r.status_code == 200, r.text
    s = (
        await client.post(
            "/api/searches",
            json={"name": "G", "params": {"text": "go"}, "interval_min": 30, "selection_profile_id": pid},
        )
    ).json()
    respx_mock.get(f"{BASE}/vacancies").mock(
        return_value=httpx.Response(200, json={"found": 1, "pages": 1, "items": _items(["7"])})
    )
    respx_mock.get(f"{BASE}/vacancies/7").mock(return_value=httpx.Response(200, json=_full("7")))
    await client.post(f"/api/searches/{s['id']}/run")
    await runtime.jobs.wait("scan")
    r = await client.post("/api/batches/create", json={})
    assert r.status_code == 200, r.text
    packet = json.loads(
        (exchange / "pending" / f"batch_{r.json()['created'][0]['batch_id']}.json").read_text(encoding="utf-8")
    )
    assert packet["profile_file"] == "profile.md"
    assert packet["tasks"][0]["profile_file"] == f"profile_set{pid}.md"

    # unbinding (resume_id=0) removes the file; deleting the resume also unbinds
    r = await client.put(f"/api/selection-profiles/{pid}", json={"resume_id": 0})
    assert r.json()["resume_id"] is None and not (exchange / f"profile_set{pid}.md").exists()
    r = await client.put(f"/api/selection-profiles/{pid}", json={"resume_id": second["id"]})
    assert (exchange / f"profile_set{pid}.md").exists()
    assert (await client.delete(f"/api/profile/resumes/{second['id']}")).status_code == 200
    assert (await client.get("/api/selection-profiles")).json()["items"][0]["resume_id"] is None
    assert not (exchange / f"profile_set{pid}.md").exists()
    async with runtime.session_factory() as session:
        assert (await session.execute(select(Vacancy).where(Vacancy.hh_id == "7"))).scalar_one() is not None
    assert DICTS  # fixtures imported for the mocks above
