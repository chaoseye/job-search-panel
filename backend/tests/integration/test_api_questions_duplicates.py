"""Employer questions at application (task answer_questions) and duplicate vacancies at scan time."""

from __future__ import annotations

import json

import httpx
import respx

from app.services.questions import parse_questions
from tests.integration.test_api_m2 import BASE, _full
from tests.integration.test_api_m4 import api  # noqa: F401
from tests.integration.test_api_manual import _sent_assist_application
from tests.unit.test_vacancy_norm import short_item

__all__ = ["api"]


def test_parse_questions() -> None:
    text = (
        "1. Есть ли у вас опыт с PostgreSQL?\n\n```\n\n```\n\n"
        "Какие технологии использовали?\nМожно выбрать несколько:\n\n* Docker\n* Kafka\n* Ни одну\n\n"
        "Опишите самый сложный кейс,\nкоторый вы реализовали.\n\nок"
    )
    assert parse_questions(text) == [
        "Есть ли у вас опыт с PostgreSQL?",
        "Какие технологии использовали?\nМожно выбрать несколько:\nDocker\nKafka\nНи одну",
        "Опишите самый сложный кейс, который вы реализовали.",
    ]
    assert parse_questions("") == [] and parse_questions("\n\n") == []


@respx.mock(assert_all_called=False)
async def test_answer_questions_roundtrip(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    ids, _app_id = await _sent_assist_application(client, runtime, respx_mock)
    vid = ids["1"]  # the applied-to vacancy: its own evaluate task is over, so the answers go into the next packet
    text = "Есть ли у вас опыт с PostgreSQL?\n\nКакие технологии использовали?\n\n* Docker\n* Kafka"
    r = await client.post(f"/api/vacancies/{vid}/questions", json={"text": text})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["request"]["status"] == "sent_in_batch" and len(body["request"]["questions"]) == 2 and body["batch_id"]
    assert (await client.post(f"/api/vacancies/{vid}/questions", json={"text": "```\n```"})).status_code == 422
    s = runtime.settings
    packet = json.loads((s.exchange_dir / "pending" / f"batch_{body['batch_id']}.json").read_text(encoding="utf-8"))
    task = next(t for t in packet["tasks"] if t["type"] == "answer_questions")
    assert task["vacancy"]["id"] == "1" and task["questions"][1].startswith("Какие технологии")
    assert task["current_letter"] and task["current_letter"].startswith("Здравствуйте")
    answers = (await client.get(f"/api/vacancies/{vid}/questions")).json()
    assert answers[0]["status"] == "sent_in_batch" and answers[0]["batch_id"] == packet["batch_id"]
    result = {
        "protocol_version": 1,
        "batch_id": packet["batch_id"],
        "profile_version": packet["profile_version"],
        "results": [
            {
                "vacancy_id": "1",
                "answers": {
                    "answers": [
                        {"question": "Есть ли опыт?", "answer": "Да: пайплайн в проде.", "needs_clarification": []},
                        {
                            "question": "Технологии?",
                            "answer": "Docker; Kafka [[УТОЧНИТЬ: Kafka]]",
                            "needs_clarification": ["Kafka"],
                        },
                        {"question": "пусто", "answer": ""},
                    ]
                },
            }
        ],
    }
    (s.exchange_dir / "done" / f"batch_{packet['batch_id']}.json").write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8"
    )
    r = await client.post("/api/batches/import")
    assert r.status_code == 200, r.text
    assert r.json()["outcomes"][0]["answers"] == 1
    detail = (await client.get(f"/api/vacancies/{vid}")).json()
    qa = detail["question_answers"][0]
    assert qa["status"] == "done" and len(qa["result"]["answers"]) == 2
    assert qa["result"]["answers"][1]["needs_clarification"] == ["Kafka"]


@respx.mock(assert_all_called=False)
async def test_duplicate_vacancy_is_skipped(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    ids, _app_id = await _sent_assist_application(client, runtime, respx_mock)  # «1» is applied to
    # a new scan brings a repost of «1» (same employer, same title) and a repost of «2» (still a candidate)
    respx_mock.get(f"{BASE}/vacancies").mock(
        return_value=httpx.Response(
            200,
            json={
                "found": 2,
                "pages": 1,
                "items": [
                    short_item(id="11", name="Python developer 1"),
                    short_item(id="12", name="Python developer 2"),
                ],
            },
        )
    )
    respx_mock.get(f"{BASE}/vacancies/11").mock(
        return_value=httpx.Response(200, json=_full("11", name="Python developer 1"))
    )
    respx_mock.get(f"{BASE}/vacancies/12").mock(
        return_value=httpx.Response(200, json=_full("12", name="Python developer 2"))
    )
    sid = (await client.get("/api/searches")).json()[0]["id"]
    await client.post(f"/api/searches/{sid}/run")
    await runtime.jobs.wait("scan")
    rows = {v["hh_id"]: v for v in (await client.get("/api/vacancies", params={"group": "all"})).json()["items"]}
    assert rows["11"]["pipeline_status"] == "skipped" and rows["11"]["duplicate_of_id"] == ids["1"]
    assert "отклик уже отправлен" in rows["11"]["pipeline_reason"]
    assert rows["12"]["pipeline_status"] == "skipped" and rows["12"]["duplicate_of_id"] == ids["2"]
    assert "уже есть в базе" in rows["12"]["pipeline_reason"]
    # the originals are untouched
    assert rows["1"]["pipeline_status"] == "sent" and rows["1"]["duplicate_of_id"] is None
    # restoring a duplicate by hand puts it back into the pipeline (user_override)
    r = await client.post(f"/api/vacancies/{rows['12']['id']}/restore")
    assert r.status_code == 200 and r.json()["pipeline_status"] == "new"  # no prescore yet: the next scan does it
    # a repost of a closed vacancy is a real new one: archive «2», bring «13» with the same title
    from sqlalchemy import select

    from app.db.models import Vacancy

    async with runtime.session_factory() as session:
        row = (await session.execute(select(Vacancy).where(Vacancy.id == ids["2"]))).scalar_one()
        row.archived = True
        row.pipeline_status = "filtered_out"
        await session.commit()
    respx_mock.get(f"{BASE}/vacancies").mock(
        return_value=httpx.Response(
            200, json={"found": 1, "pages": 1, "items": [short_item(id="13", name="Python developer 2")]}
        )
    )
    respx_mock.get(f"{BASE}/vacancies/13").mock(
        return_value=httpx.Response(200, json=_full("13", name="Python developer 2"))
    )
    await client.post(f"/api/searches/{sid}/run")
    await runtime.jobs.wait("scan")
    rows = {v["hh_id"]: v for v in (await client.get("/api/vacancies", params={"group": "all"})).json()["items"]}
    # «12» was restored (user_override) and is alive, so «13» duplicates it, not the archived «2»
    assert rows["13"]["pipeline_status"] == "skipped" and rows["13"]["duplicate_of_id"] == rows["12"]["id"]
