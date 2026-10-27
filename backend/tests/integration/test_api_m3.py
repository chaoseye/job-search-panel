"""Exchange with Claude Code end-to-end: packet → result file → import → letters/feedback/revisions."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from httpx import ASGITransport

from app.config import Settings
from app.main import app
from app.runtime import Runtime
from tests.integration.test_api_m2 import DICTS, _full, _items

BASE = "https://api.hh.ru"


@pytest.fixture
async def api(settings: Settings):
    settings = settings.model_copy(update={"batch_size": 2, "batch_desc_max_chars": 60, "batch_auto_create": False})
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


async def _seed(client: httpx.AsyncClient, runtime: Runtime, m: respx.MockRouter, ids: list[str]) -> dict[str, int]:
    """Profile from a file + one search scanned with mocked hh → vacancies in `prescored`."""
    r = await client.post(
        "/api/profile/resumes/upload", files={"file": ("cv.txt", "Иван, Python, FastAPI".encode(), "text/plain")}
    )
    assert r.status_code == 200
    m.get(f"{BASE}/dictionaries").mock(return_value=httpx.Response(200, json=DICTS))
    m.get(f"{BASE}/industries").mock(return_value=httpx.Response(200, json=[]))
    r = await client.post("/api/searches", json={"name": "S", "params": {"text": "python"}, "interval_min": 30})
    sid = r.json()["id"]
    m.get(f"{BASE}/vacancies").mock(
        return_value=httpx.Response(200, json={"found": len(ids), "pages": 1, "items": _items(ids)})
    )
    for i in ids:
        m.get(f"{BASE}/vacancies/{i}").mock(return_value=httpx.Response(200, json=_full(i)))
    await client.post(f"/api/searches/{sid}/run")
    await runtime.jobs.wait("scan")
    rows = (await client.get("/api/vacancies", params={"group": "prescored"})).json()["items"]
    assert {v["hh_id"] for v in rows} == set(ids)
    return {v["hh_id"]: v["id"] for v in rows}


def _result(batch_id: str, items: list[dict]) -> dict:
    return {"protocol_version": 1, "batch_id": batch_id, "profile_version": 1, "results": items}


def _good_letter(n: int = 700) -> str:
    base = "Здравствуйте! Меня заинтересовала ваша вакансия. За семь лет я построил платёжный сервис и снизил p95 с 800 до 120 мс. "
    text = (base * 10)[:n]
    return text


@respx.mock(assert_all_called=False)
async def test_full_exchange_cycle(api, respx_mock: respx.MockRouter, tmp_path: Path) -> None:
    client, runtime = api
    settings = runtime.settings
    await _login(client, respx_mock)
    ids = await _seed(client, runtime, respx_mock, ["1", "2", "3"])

    # no packet without a profile? profile exists (file). Create packets: batch_size=2 -> two packets
    r = await client.post("/api/batches/create", json={})
    assert r.status_code == 200, r.text
    created = r.json()["created"]
    assert [c["tasks_count"] for c in created] == [2, 1]
    assert r.json()["summary"]["awaiting_claude"] == 3 and r.json()["summary"]["pending_batches"] == 2
    pending_all = sorted((settings.exchange_dir / "pending").glob("*.json"))
    assert len(pending_all) == 2 and (settings.exchange_dir / "guidelines.md").exists()
    # the packet with two tasks first (batch ids share a minute stamp, so name order is random)
    pending = sorted(pending_all, key=lambda p: -len(json.loads(p.read_text(encoding="utf-8"))["tasks"]))
    packet = json.loads(pending[0].read_text(encoding="utf-8"))
    assert packet["protocol_version"] == 1 and packet["profile_version"] == 1
    assert packet["letter_defaults"]["min_chars"] == 600 and packet["letter_defaults"]["language"] == "auto"
    task = packet["tasks"][0]
    assert task["type"] == "evaluate_and_letter" and task["want_letter"] is True
    assert task["vacancy"]["description"].endswith("…[обрезано]") and len(task["vacancy"]["description"]) <= 60 + 12
    assert task["vacancy"]["key_skills"] == ["Python", "FastAPI", "PostgreSQL", "Kafka"]
    assert task["prescore"]["value"] is not None
    listing = (await client.get("/api/vacancies", params={"group": "awaiting_claude"})).json()
    assert listing["total"] == 3 and all(v["batch_id"] for v in listing["items"])

    # a second create call finds nothing new
    assert (await client.post("/api/batches/create", json={})).json()["created"] == []

    # Claude writes the result for the first packet: one apply with letter, one maybe with a marker, plus a stray id
    batch_id = packet["batch_id"]
    vac_ids = [t["vacancy"]["id"] for t in packet["tasks"]]
    result = _result(
        batch_id,
        [
            {
                "vacancy_id": vac_ids[0],
                "evaluation": {
                    "score": 85,
                    "verdict": "apply",
                    "summary": "Отличное совпадение",
                    "matched": ["Python"],
                    "gaps": [],
                    "red_flags": [],
                },
                "letter": {
                    "text": _good_letter(),
                    "language": "ru",
                    "mentions": ["платёжный сервис"],
                    "needs_clarification": [],
                },
                "error": None,
            },
            {
                "vacancy_id": vac_ids[1],
                "evaluation": {"score": 55, "verdict": "maybe", "summary": "Частично", "gaps": ["Kafka"]},
                "letter": {"text": _good_letter(650) + " [[УТОЧНИТЬ: ожидания по зарплате]]", "language": "ru"},
                "error": None,
            },
            {"vacancy_id": "999999", "evaluation": None, "letter": None, "error": "not in packet"},
        ],
    )
    done = settings.exchange_dir / "done" / pending[0].name
    done.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    r = await client.post("/api/batches/import")
    assert r.status_code == 200, r.text
    outcome = r.json()["outcomes"][0]
    assert outcome["status"] == "imported", outcome
    assert outcome["evaluations"] == 2 and outcome["letters"] == 2 and outcome["missing"] == []
    assert any("999999" in w for w in outcome["warnings"])
    assert not done.exists() and (settings.exchange_dir / "imported" / pending[0].name).exists()
    assert not pending[0].exists()  # packet moved too

    batches = (await client.get("/api/batches")).json()["items"]
    first = next(b for b in batches if b["batch_id"] == batch_id)
    assert first["status"] == "imported" and first["results_count"] == 2 and first["imported_at"]

    v1 = (await client.get(f"/api/vacancies/{ids[vac_ids[0]]}")).json()
    assert v1["pipeline_status"] == "suitable" and v1["score"] == 85 and v1["verdict"] == "apply"
    assert v1["evaluation"]["summary"] == "Отличное совпадение" and v1["evaluation"]["stale_profile"] is False
    assert v1["letter"]["origin"] == "claude" and v1["letter"]["status"] == "ready" and v1["letter_status"] == "ready"
    assert v1["letter"]["send_problems"] == [] and v1["letters"][0]["version"] == 1
    v2 = (await client.get(f"/api/vacancies/{ids[vac_ids[1]]}")).json()
    assert v2["pipeline_status"] == "maybe" and v2["letter"]["status"] == "needs_clarification"
    assert "[[УТОЧНИТЬ: ожидания по зарплате]]" in v2["letter"]["needs_clarification"]
    assert v2["letter"]["send_problems"]
    assert (await client.get("/api/vacancies", params={"group": "suitable"})).json()["total"] == 1
    assert (await client.get("/api/vacancies", params={"group": "maybe"})).json()["total"] == 1
    assert (await client.get("/api/vacancies")).json()["items"][0]["score"] == 85  # sorted by Claude score first

    # re-importing the same file is ignored
    done.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    outcome = (await client.post("/api/batches/import")).json()["outcomes"][0]
    assert outcome["status"] == "skipped"
    assert len((await client.get(f"/api/vacancies/{ids[vac_ids[0]]}")).json()["letters"]) == 1

    # edit letter -> version 2 (edited); restore version 1; feedback; revision request
    vid = ids[vac_ids[0]]
    r = await client.put(f"/api/vacancies/{vid}/letter", json={"text": "Отредактированное письмо без ссылок."})
    assert r.json()["letter"]["version"] == 2 and r.json()["letter"]["origin"] == "edited"
    r = await client.put(f"/api/vacancies/{vid}/letter", json={"text": "Отредактированное письмо без ссылок."})
    assert r.json()["letter"]["version"] == 2  # unchanged text -> no new version
    r = await client.post(f"/api/vacancies/{vid}/letter/{r.json()['letters'][1]['id']}/restore")
    assert r.json()["letter"]["version"] == 1 and r.json()["letter"]["origin"] == "claude"
    r = await client.post(f"/api/vacancies/{vid}/feedback", json={"feedback": "wrong", "comment": "Kafka у меня есть"})
    assert r.json()["evaluation"]["user_feedback"] == "wrong"
    r = await client.post(f"/api/vacancies/{vid}/letter/revision", json={"comment": "короче и про Kafka"})
    assert r.json()["revision_request"]["status"] == "queued"
    assert (
        await client.post(f"/api/vacancies/{ids[vac_ids[1]]}/letter/revision", json={"comment": ""})
    ).status_code == 422
    assert (await client.get("/api/batches/summary")).json()["revision_requests"] == 1

    # next packet: revision task + user_notes from feedback
    r = await client.post("/api/batches/create", json={})
    created = r.json()["created"]
    assert len(created) == 1 and created[0]["tasks_count"] == 1
    packet2 = json.loads(Path(created[0]["packet_path"]).read_text(encoding="utf-8"))
    assert packet2["tasks"][0]["type"] == "letter_revision"
    assert (
        packet2["tasks"][0]["user_comment"] == "короче и про Kafka" and packet2["tasks"][0]["evaluation"]["score"] == 85
    )
    assert packet2["tasks"][0]["previous_letter"].startswith("Здравствуйте!")
    assert any("Kafka у меня есть" in n for n in packet2["user_notes"])
    assert (await client.get(f"/api/vacancies/{vid}")).json()["revision_request"]["status"] == "sent_in_batch"
    result2 = _result(
        packet2["batch_id"], [{"vacancy_id": vac_ids[0], "letter": {"text": "Новая версия письма про Kafka." * 25}}]
    )
    (settings.exchange_dir / "done" / f"batch_{packet2['batch_id']}.json").write_text(
        json.dumps(result2), encoding="utf-8"
    )
    outcome = (await client.post("/api/batches/import")).json()["outcomes"][0]
    assert outcome["status"] == "imported" and outcome["letters"] == 1 and outcome["evaluations"] == 0
    v1 = (await client.get(f"/api/vacancies/{vid}")).json()
    assert v1["letter"]["version"] == 3 and v1["revision_request"] is None and v1["pipeline_status"] == "suitable"

    # partial result for the second original packet: the missing vacancy returns to `prescored`
    second = next(b for b in batches if b["batch_id"] != batch_id)
    lone = json.loads(Path(second["packet_path"]).read_text(encoding="utf-8"))["tasks"][0]["vacancy"]["id"]
    result3 = _result(second["batch_id"], [])
    (settings.exchange_dir / "done" / f"batch_{second['batch_id']}.json").write_text(
        json.dumps(result3), encoding="utf-8"
    )
    outcome = (await client.post("/api/batches/import")).json()["outcomes"][0]
    assert outcome["status"] == "partial" and outcome["missing"] == [lone]
    v3 = (await client.get(f"/api/vacancies/{ids[lone]}")).json()
    assert v3["pipeline_status"] == "prescored" and "будет включена снова" in v3["pipeline_reason"]

    # rejected result: bad JSON
    bad = settings.exchange_dir / "done" / "batch_nope.json"
    bad.write_text("{not json", encoding="utf-8")
    outcome = (await client.post("/api/batches/import")).json()["outcomes"][0]
    assert outcome["status"] == "rejected"
    assert (settings.exchange_dir / "rejected" / "batch_nope.json.error.txt").exists()

    # manual paste
    r = await client.post("/api/batches/create", json={})
    packet3 = json.loads(Path(r.json()["created"][0]["packet_path"]).read_text(encoding="utf-8"))
    text = json.dumps(
        _result(
            packet3["batch_id"],
            [{"vacancy_id": lone, "evaluation": {"score": 20, "verdict": "skip", "summary": "нет"}}],
        )
    )
    r = await client.post("/api/batches/import-text", json={"text": text})
    assert r.status_code == 200 and r.json()["outcome"]["status"] == "imported"
    assert (await client.get(f"/api/vacancies/{ids[lone]}")).json()["pipeline_status"] == "not_suitable"
    assert (await client.post("/api/batches/import-text", json={"text": "{}"})).status_code == 400

    # to-batch brings a not_suitable vacancy back into the queue; cancel returns awaiting to prescored
    r = await client.post(f"/api/vacancies/{ids[lone]}/to-batch")
    assert r.json()["pipeline_status"] == "prescored" and r.json()["user_override"] is True
    r = await client.post("/api/batches/create", json={})
    pk = r.json()["created"][0]["id"]
    assert (await client.get(f"/api/batches/{pk}")).json()["tasks"][0]["vacancy_id"] == lone
    r = await client.delete(f"/api/batches/{pk}")
    assert r.json()["ok"] and r.json()["returned"] == 1
    assert (await client.get(f"/api/vacancies/{ids[lone]}")).json()["pipeline_status"] == "prescored"
    assert (await client.delete(f"/api/batches/{pk}")).status_code == 409

    # cancel-pending: every pending packet is cancelled at once, its vacancies return to the queue
    r = await client.post("/api/batches/create", json={})
    pk2 = r.json()["created"][0]["id"]
    r = await client.post("/api/batches/cancel-pending")
    assert r.json()["cancelled"] == 1 and r.json()["returned"] == 1 and r.json()["summary"]["pending_batches"] == 0
    assert (await client.get(f"/api/batches/{pk2}")).json()["status"] == "cancelled"
    assert (await client.get(f"/api/vacancies/{ids[lone]}")).json()["pipeline_status"] == "prescored"
    assert (await client.post("/api/batches/cancel-pending")).json()["cancelled"] == 0

    # letters that still ask the user something (and can still go out) are collected in one group
    r = await client.put(
        f"/api/vacancies/{ids[lone]}/letter", json={"text": "Здравствуйте! [[УТОЧНИТЬ: зарплата]] " + "текст " * 80}
    )
    assert r.status_code == 200 and r.json()["letter"]["needs_clarification"]
    listing = (await client.get("/api/vacancies", params={"group": "needs_clarification"})).json()
    assert ids[lone] in [v["id"] for v in listing["items"]]
    before = listing["counts"]["needs_clarification"]
    assert before == listing["total"] >= 1
    r = await client.put(
        f"/api/vacancies/{ids[lone]}/letter", json={"text": "Здравствуйте! Без вопросов. " + "текст " * 80}
    )
    assert (await client.get("/api/vacancies", params={"group": "needs_clarification"})).json()["total"] == before - 1

    # template letter + guidelines endpoint
    r = await client.post(f"/api/vacancies/{ids[lone]}/letter/template")
    assert r.json()["letter"]["origin"] == "template" and f"Python developer {lone}" in r.json()["letter"]["text"]
    assert "guidelines_version" in (await client.get("/api/batches/guidelines")).text


async def test_create_requires_profile(api) -> None:
    client, _ = api
    r = await client.post("/api/batches/create", json={})
    assert r.status_code == 400 and "профиль" in r.json()["detail"].lower()


@respx.mock(assert_all_called=False)
async def test_scan_auto_creates_batches(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    runtime.settings = runtime.settings.model_copy(update={"batch_auto_create": True})
    await _login(client, respx_mock)
    await _seed(client, runtime, respx_mock, ["1"])
    # _seed ran the scan with auto-create on: the vacancy is already in a packet
    summary = (await client.get("/api/batches/summary")).json()
    assert summary["pending_batches"] >= 1 and summary["awaiting_claude"] >= 1
