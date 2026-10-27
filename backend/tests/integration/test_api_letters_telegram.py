"""Event `letter`: every evaluated vacancy with a cover letter and a score ≥ TELEGRAM_LETTERS_MIN_SCORE goes to the
bot as its own message (vacancy, summary, the letter text, the apply link); revisions too."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from httpx import ASGITransport

from app.config import Settings
from app.main import app
from app.runtime import Runtime
from app.services.bot_commands import poll_commands
from app.services.telegram import deliver_pending
from app.services.tg_letters import flush_resolutions
from tests.integration.test_api_m3 import _good_letter, _login, _result, _seed
from tests.integration.test_api_m6 import TG, TOKEN

__all__ = ["TOKEN"]


@pytest.fixture
async def api(settings: Settings):
    settings = settings.model_copy(
        update={
            "telegram_bot_token": TOKEN,
            "telegram_chat_id": "42",
            "batch_size": 3,
            "batch_desc_max_chars": 60,
            "batch_auto_create": False,
        }
    )
    runtime = await Runtime.create(settings)
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


def _item(vacancy_id: str, score: int, letter: str | None, summary: str = "Совпадение") -> dict[str, Any]:
    verdict = "apply" if score >= 70 else ("maybe" if score >= 50 else "skip")
    return {
        "vacancy_id": vacancy_id,
        "evaluation": {"score": score, "verdict": verdict, "summary": summary, "matched": ["Python"], "gaps": []},
        "letter": {"text": letter, "language": "ru"} if letter else None,
        "error": None,
    }


@respx.mock(assert_all_called=False)
async def test_letters_go_to_the_bot_one_message_each(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    settings = runtime.settings
    await _login(client, respx_mock)
    ids = await _seed(client, runtime, respx_mock, ["1", "2", "3"])
    sent: list[dict[str, Any]] = []

    def tg_send(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7000 + len(sent)}})

    respx_mock.post(f"{TG}/sendMessage").mock(side_effect=tg_send)
    respx_mock.post(f"{TG}/getMe").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"username": "B"}})
    )

    packet = (await client.post("/api/batches/create", json={})).json()["created"][0]
    pending = sorted((settings.exchange_dir / "pending").glob("*.json"))
    assert len(pending) == 1
    # 85 with a letter → sent; 55 with a marker → sent with a warning; 45 with a letter → below the threshold
    result = _result(
        packet["batch_id"],
        [
            _item("1", 85, _good_letter(), "Отличное совпадение"),
            _item("2", 55, _good_letter(650) + " [[УТОЧНИТЬ: ожидания по зарплате]]", "Частично"),
            _item("3", 45, _good_letter(620), "Слабо"),
        ],
    )
    (settings.exchange_dir / "done" / pending[0].name).write_text(json.dumps(result, ensure_ascii=False), "utf-8")
    outcome = (await client.post("/api/batches/import")).json()["outcomes"][0]
    assert outcome["status"] == "imported" and outcome["letters"] == 3

    rows = [
        r
        for r in (await client.get("/api/notifications", params={"channel": "telegram", "limit": 50})).json()
        if r["event_type"] == "letter"
    ]
    assert [r["payload"]["hh_id"] for r in rows] == ["2", "1"] or [r["payload"]["hh_id"] for r in rows] == ["1", "2"]
    assert all(r["status"] == "queued" for r in rows)
    assert all(r["summary"].startswith("Вакансия с письмом: Python developer") for r in rows)
    by_hh = {r["payload"]["hh_id"]: r["payload"] for r in rows}
    assert by_hh["1"]["status"] == "suitable" and by_hh["1"]["letter"] == _good_letter().strip()
    assert by_hh["2"]["needs_clarification"] is True and by_hh["2"]["version"] == 1
    assert by_hh["1"]["apply_url"] and by_hh["1"]["url"].startswith("https://hh.ru/vacancy/")

    # delivery: one message per vacancy, right away (no merge window), letter text inside
    async with runtime.session_factory() as session:
        delivered = await deliver_pending(session, settings, runtime.http)
        await session.commit()
    assert delivered["sent"] == 3  # two letters + «Разбор импортирован»; the digest waits for its merge window
    texts = sorted(m["text"] for m in sent if m["text"].startswith("✉️"))
    assert len(texts) == 2
    first = next(t for t in texts if "оценка 85" in t)
    assert first.startswith('✉️ <b>Подходит</b> — оценка 85: <a href="https://hh.ru/vacancy/')
    assert ">Python developer 1</a>" in first
    assert "<i>Отличное совпадение</i>" in first and "<b>Сопроводительное письмо</b> (версия 1)" in first
    assert "<pre>Здравствуйте! Меня заинтересовала" in first and "[[УТОЧНИТЬ" not in first
    second = next(t for t in texts if "оценка 55" in t)
    assert second.startswith("✉️ <b>Возможно</b> — оценка 55") and "⚠️ В письме есть пометки [[УТОЧНИТЬ]]" in second
    assert all(TOKEN not in t for t in texts)
    # inline buttons: the panel actions + the hh.ru response page; the message id is remembered per vacancy
    letter_msgs = [m for m in sent if m["text"].startswith("✉️")]
    markup = letter_msgs[0]["reply_markup"]["inline_keyboard"]
    vid1 = ids["1"]
    data = {b["callback_data"] for b in markup[0]}
    assert {f"vac:{vid1}:sent", f"vac:{vid1}:skip", f"vac:{vid1}:snooze"} <= data or any(
        f"vac:{ids['2']}:sent" in d for d in data
    )
    assert markup[1][0]["text"] == "Откликнуться на hh.ru" and markup[1][0]["url"].startswith("https://hh.ru/")
    from sqlalchemy import select

    from app.db.models import Application, VacancyTgMessage

    async with runtime.session_factory() as session:
        remembered = (await session.execute(select(VacancyTgMessage))).scalars().all()
        assert {r.vacancy_id for r in remembered} == {ids["1"], ids["2"]} and all(
            r.status == "active" for r in remembered
        )
        msg_of = {r.vacancy_id: r.message_id for r in remembered}

    # bot → panel: «Да, отправлено» on vacancy 1 = assist application confirmed, message shrinks to a status
    await client.post("/api/applications/consent")
    calls: list[tuple[str, dict[str, Any]]] = []

    def tg_any(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": True})

    respx_mock.post(f"{TG}/answerCallbackQuery").mock(side_effect=tg_any)
    respx_mock.post(f"{TG}/editMessageText").mock(side_effect=tg_any)
    respx_mock.post(f"{TG}/deleteMessage").mock(side_effect=tg_any)
    update = {
        "update_id": 500,
        "callback_query": {
            "id": "cbq-1",
            "from": {"id": 42},
            "message": {"message_id": msg_of[vid1], "chat": {"id": 42}},
            "data": f"vac:{vid1}:sent",
        },
    }
    respx_mock.post(f"{TG}/getUpdates").mock(return_value=httpx.Response(200, json={"ok": True, "result": [update]}))
    summary = await poll_commands(runtime)
    assert summary["buttons"] == 1
    assert [c[0] for c in calls] == ["answerCallbackQuery", "editMessageText"]
    assert calls[0][1]["text"] == "Отклик отмечен отправленным" and calls[0][1]["show_alert"] is False
    assert calls[1][1]["message_id"] == msg_of[vid1] and calls[1][1]["text"].startswith(
        "✅ Отклик отправлен: Python developer 1"
    )
    detail = (await client.get(f"/api/vacancies/{vid1}")).json()
    assert detail["pipeline_status"] == "sent"
    async with runtime.session_factory() as session:
        app_row = (await session.execute(select(Application).where(Application.vacancy_id == vid1))).scalar_one()
        assert app_row.mode == "assist" and app_row.status == "sent" and app_row.confirmed_by_user is True
        rows = (
            (await session.execute(select(VacancyTgMessage).where(VacancyTgMessage.vacancy_id == vid1))).scalars().all()
        )
        assert all(r.status == "resolved" and r.resolution == "sent" for r in rows)
    # pressing again: a toast, nothing changes
    calls.clear()
    respx_mock.post(f"{TG}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": [{**update, "update_id": 501}]})
    )
    await poll_commands(runtime)
    assert calls[0][1]["text"] == "Отклик уже отмечен отправленным"

    # panel → bot: skip vacancy 2 in the panel → its bot message is deleted on the next tick
    calls.clear()
    vid2 = ids["2"]
    assert (await client.post(f"/api/vacancies/{vid2}/skip", json={"reason": "не то"})).status_code == 200
    async with runtime.session_factory() as session:
        row = (await session.execute(select(VacancyTgMessage).where(VacancyTgMessage.vacancy_id == vid2))).scalar_one()
        assert row.status == "resolve" and row.resolution == "skipped"
        assert await flush_resolutions(session, settings, runtime.http) == 1
        await session.commit()
        await session.refresh(row)
        assert row.status == "resolved"
    assert calls == [("deleteMessage", {"chat_id": "42", "message_id": msg_of[vid2]})]
    # too old to delete → the message is rewritten instead
    calls.clear()
    async with runtime.session_factory() as session:
        row = (await session.execute(select(VacancyTgMessage).where(VacancyTgMessage.vacancy_id == vid2))).scalar_one()
        row.status = "resolve"
        await session.commit()
    respx_mock.post(f"{TG}/deleteMessage").mock(
        return_value=httpx.Response(400, json={"ok": False, "description": "Bad Request: message can't be deleted"})
    )
    async with runtime.session_factory() as session:
        assert await flush_resolutions(session, settings, runtime.http) == 1
        await session.commit()
    assert [c[0] for c in calls] == ["editMessageText"] and calls[0][1]["text"].startswith(
        "⏭ Пропущена: Python developer 2"
    )

    # the switch in Settings: off → letters are not queued at all; the status shows the switch
    tg_status = (await client.get("/api/notifications/telegram")).json()
    assert tg_status["letters"] == {"enabled": True, "min_score": 50, "event_in_env": True}
    r = await client.put("/api/notifications/telegram/letters", json={"enabled": False, "min_score": 70})
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["min_score"] == 70
    before = len(
        [
            r
            for r in (await client.get("/api/notifications", params={"channel": "telegram", "limit": 100})).json()
            if r["event_type"] == "letter"
        ]
    )
    assert (await client.post(f"/api/vacancies/{ids['3']}/to-batch")).status_code == 200
    packet3 = (await client.post("/api/batches/create", json={})).json()["created"]
    assert packet3, "vacancy 3 (score 45, not_suitable) is re-evaluated through to-batch"
    pending3 = sorted((settings.exchange_dir / "pending").glob("*.json"))
    result3 = _result(packet3[0]["batch_id"], [_item("3", 90, _good_letter(), "Теперь подходит")])
    (settings.exchange_dir / "done" / pending3[0].name).write_text(json.dumps(result3, ensure_ascii=False), "utf-8")
    assert (await client.post("/api/batches/import")).json()["outcomes"][0]["letters"] == 1
    after = len(
        [
            r
            for r in (await client.get("/api/notifications", params={"channel": "telegram", "limit": 100})).json()
            if r["event_type"] == "letter"
        ]
    )
    assert after == before  # switched off: nothing queued
    assert (await client.put("/api/notifications/telegram/letters", json={"enabled": True, "min_score": 50})).json()[
        "enabled"
    ] is True

    # a revision comes back as its own message too (vacancy 3, re-evaluated to 90 above)
    vid = ids["3"]
    assert (await client.post(f"/api/vacancies/{vid}/letter/revision", json={"comment": "короче"})).status_code == 200
    packet2 = (await client.post("/api/batches/create", json={})).json()["created"][0]
    pending2 = sorted((settings.exchange_dir / "pending").glob("*.json"))
    assert len(pending2) == 1 and packet2["tasks_count"] == 1
    result2 = _result(
        packet2["batch_id"],
        [
            {
                "vacancy_id": "3",
                "evaluation": None,
                "letter": {"text": _good_letter(640), "language": "ru"},
                "error": None,
            }
        ],
    )
    (settings.exchange_dir / "done" / pending2[0].name).write_text(json.dumps(result2, ensure_ascii=False), "utf-8")
    assert (await client.post("/api/batches/import")).json()["outcomes"][0]["letters"] == 1
    async with runtime.session_factory() as session:
        delivered = await deliver_pending(session, settings, runtime.http)
        await session.commit()
    revised = [m["text"] for m in sent if m["text"].startswith("✉️ <b>Письмо переработано</b>")]
    assert len(revised) == 1 and revised[0].startswith("✉️ <b>Письмо переработано</b> (подходит, 90)")
    assert "(версия 3)" in revised[0] and delivered["sent"] >= 1  # v1 at 45, v2 at 90, v3 the revision
