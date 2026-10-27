"""hh.ru e-mails → manual journal (IMAP mocked), interview preparation through the packet exchange, automatic
re-evaluation after a criteria change."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
import respx

from app.db.base import utcnow
from app.scheduler import build_scheduler
from app.services import mail_sync
from app.services.mail_sync import MailItem, sync_mail
from tests.integration.test_api_m4 import api  # noqa: F401
from tests.integration.test_api_manual import _sent_assist_application

__all__ = ["api"]


def _items(vacancy_hh_id: str, vacancy_name: str) -> list[MailItem]:
    now = utcnow()
    return [
        MailItem(
            11,
            "<inv@hh>",
            "Вас пригласили на собеседование",
            "no-reply@hh.ru",
            now - timedelta(hours=3),
            f"Компания приглашает вас.\nhttps://hh.ru/vacancy/{vacancy_hh_id}?from=mail",
            [f"https://hh.ru/vacancy/{vacancy_hh_id}?from=mail"],
        ),
        MailItem(
            12,
            "<view@hh>",
            f"Работодатель просмотрел ваш отклик на «{vacancy_name}»",
            "no-reply@hh.ru",
            now - timedelta(hours=2),
            "Отклик просмотрен.",
            [],
        ),
        MailItem(
            13,
            "<msg@hh>",
            "Новое сообщение от работодателя",
            "no-reply@hh.ru",
            now - timedelta(hours=1),
            f"Вакансия {vacancy_name}\nКогда удобно созвониться?\nhttps://hh.ru/applicant/negotiations",
            [],
        ),
        MailItem(14, "<other@hh>", "Отказ по вакансии", "no-reply@hh.ru", now, "https://hh.ru/vacancy/999999", []),
        MailItem(15, "<promo@hh>", "Подборка вакансий для вас", "no-reply@hh.ru", now, "Новые вакансии", []),
    ]


@respx.mock(assert_all_called=False)
async def test_mail_sync_fills_the_journal_and_queues_a_prep(
    api, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, runtime = api
    ids, app_id = await _sent_assist_application(client, runtime, respx_mock)
    assert (await sync_mail(runtime)) == {"skipped": "IMAP не настроен"}
    runtime.settings = runtime.settings.model_copy(
        update={"imap_host": "imap.test", "imap_user": "user@test.local", "imap_password": "secret-app-password"}
    )
    calls: list[tuple[int | None, str]] = []

    async def fake_fetch(settings, last_uid, since):
        calls.append((last_uid, settings.imap_user))
        return _items("1", "Python developer 1"), 15

    monkeypatch.setattr(mail_sync, "fetch_new", fake_fetch)
    summary = await sync_mail(runtime)
    assert summary == {
        "fetched": 5,
        "matched": 3,
        "unmatched": 1,
        "ignored": 1,
        "duplicates": 0,
        "last_uid": 15,
        "reply_drafts": 1,
        "studio_replies": 0,  # no letters to studios were sent
    }
    assert calls == [(None, "user@test.local")]
    # the employer message queued a reply draft; invitation and message went to the event feed (source: mail)
    feed = (await client.get("/api/notifications")).json()
    kinds = [(n["event_type"], n["payload"].get("source")) for n in feed[:6]]
    assert ("invitation", "mail") in kinds and ("message", "mail") in kinds
    assert next(n for n in feed if n["event_type"] == "message")["payload"]["text"].startswith(
        "Вакансия Python developer 1"
    )

    # the journal got the invitation, the view and the employer message; the invitation queued a preparation
    row = next(a for a in (await client.get("/api/applications")).json()["items"] if a["id"] == app_id)
    neg = row["negotiation"]
    assert neg["manual"] and neg["state_id"] == "invitation" and neg["invited_at"]
    detail = (await client.get(f"/api/negotiations/{row['hh_negotiation_id']}")).json()
    assert [m["text"] for m in detail["messages"]] == ["Вакансия Python developer 1 Когда удобно созвониться?"]
    assert {e["event_type"] for e in detail["events"]} >= {"viewed", "state_changed", "message"}
    assert len(detail["reply_drafts"]) == 1 and detail["reply_drafts"][0]["status"] in ("queued", "sent_in_batch")
    preps = (await client.get(f"/api/applications/{app_id}/interview-preps")).json()
    assert (
        len(preps) == 1 and preps[0]["auto"] is True and preps[0]["status"] == "sent_in_batch" and preps[0]["batch_id"]
    )

    # status for the Settings page: masked user, last summary, the unmatched subject
    status = (await client.get("/api/mail/status")).json()
    assert status["configured"] and status["user"] == "us***@test.local" and status["last"]["summary"]["matched"] == 3
    assert status["unmatched"][0]["subject"] == "Отказ по вакансии" and "secret" not in json.dumps(status)

    # the same messages again → duplicates, nothing recorded twice; the cursor moved on
    calls.clear()
    summary = await sync_mail(runtime)
    assert summary["duplicates"] == 5 and summary["matched"] == 0 and calls == [(15, "user@test.local")]
    detail = (await client.get(f"/api/negotiations/{row['hh_negotiation_id']}")).json()
    assert len(detail["messages"]) == 1
    assert len((await client.get(f"/api/applications/{app_id}/interview-preps")).json()) == 1

    # the manual sync endpoint runs the same job; the scheduler registers the tick when IMAP is configured
    r = await client.post("/api/mail/sync")
    assert r.status_code == 200 and r.json()["last"]["summary"]["duplicates"] == 5
    assert "mail_sync" in {j.id for j in build_scheduler(runtime).get_jobs()}

    # a failing mailbox is reported without the password
    async def failing_fetch(settings, last_uid, since):
        raise RuntimeError("login failed for secret-app-password")

    monkeypatch.setattr(mail_sync, "fetch_new", failing_fetch)
    result = await sync_mail(runtime)
    assert "error" in result and "secret-app-password" not in result["error"] and "***" in result["error"]

    # interview prep: the packet task and the imported result
    s = runtime.settings
    batch_id = preps[0]["batch_id"]
    packet = json.loads((s.exchange_dir / "pending" / f"batch_{batch_id}.json").read_text(encoding="utf-8"))
    task = next(t for t in packet["tasks"] if t["type"] == "interview_prep")
    assert task["vacancy"]["id"] == "1" and task["letter_sent"].startswith("Здравствуйте")
    assert task["thread"] and task["thread"][-1]["text"].startswith("Вакансия Python developer 1")
    result_file = {
        "protocol_version": 1,
        "batch_id": batch_id,
        "profile_version": packet["profile_version"],
        "results": [
            {
                "vacancy_id": "1",
                "prep": {
                    "summary": "Им нужен человек на backend.",
                    "questions": [
                        {"question": "Расскажите о проекте с Kafka", "answer": "Собрал 12 сценариев…"},
                        {"answer": "без вопроса"},
                    ],
                    "ask_them": ["Какие задачи в первые 3 месяца?"],
                    "risks": ["нет опыта с Kafka"],
                    "prepare": ["освежить SQL"],
                },
            }
        ],
    }
    (s.exchange_dir / "done" / f"batch_{batch_id}.json").write_text(
        json.dumps(result_file, ensure_ascii=False), encoding="utf-8"
    )
    r = await client.post("/api/batches/import")
    assert r.status_code == 200, r.text
    assert r.json()["outcomes"][0]["preps"] == 1
    preps = (await client.get(f"/api/applications/{app_id}/interview-preps")).json()
    assert preps[0]["status"] == "done" and preps[0]["result"]["summary"].startswith("Им нужен")
    assert preps[0]["result"]["questions"] == [
        {"question": "Расскажите о проекте с Kafka", "answer": "Собрал 12 сценариев…"}
    ]
    assert preps[0]["result"]["prepare"] == ["освежить SQL"]

    # a manual request with notes creates another one; recording the invitation again does not (auto = no duplicate)
    r = await client.post(f"/api/applications/{app_id}/interview-prep", json={"notes": "спросят про Kafka"})
    assert r.status_code == 200, r.text
    assert r.json()["prep"]["user_notes"] == "спросят про Kafka" and len(r.json()["preps"]) == 2
    r = await client.post(f"/api/applications/{app_id}/manual-update", json={"state": "invitation"})
    assert r.status_code == 200
    assert len((await client.get(f"/api/applications/{app_id}/interview-preps")).json()) == 2
    assert ids["2"] != ids["1"]


@respx.mock(assert_all_called=False)
async def test_reprocess_after_criteria_change(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _sent_assist_application(client, runtime, respx_mock)
    runtime.settings = runtime.settings.model_copy(update={"reprocess_on_criteria_change": True})
    prefs = (await client.get("/api/profile/preferences")).json()
    # only a profile field changed → no re-evaluation
    prefs["extra_facts"] = "люблю котов"
    r = await client.put("/api/profile/preferences", json=prefs)
    assert r.status_code == 200 and r.json()["reprocess_started"] is False
    # a criterion changed → the job starts in the background
    prefs["stop_words_title"] = ["1С"]
    r = await client.put("/api/profile/preferences", json=prefs)
    assert r.status_code == 200 and r.json()["reprocess_started"] is True
    await runtime.jobs.wait("reprocess")
    runs = (await client.get("/api/jobs")).json()["runs"]
    assert runs[0]["job"] == "reprocess" and runs[0]["status"] == "ok"
    # switched off in settings → never
    runtime.settings = runtime.settings.model_copy(update={"reprocess_on_criteria_change": False})
    prefs["stop_words_title"] = ["1С", "продажи"]
    assert (await client.put("/api/profile/preferences", json=prefs)).json()["reprocess_started"] is False


@respx.mock(assert_all_called=False)
async def test_resume_advice_roundtrip(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    ids, _app_id = await _sent_assist_application(client, runtime, respx_mock)
    sid = (await client.get("/api/searches")).json()[0]["id"]
    r = await client.post("/api/analytics/resume-advice", json={"by": "search", "key": f"s{sid}", "notes": "коротко"})
    assert r.status_code == 200, r.text
    body = r.json()
    advice = body["advice"]
    assert advice["status"] == "sent_in_batch" and advice["segment_label"] == "S" and body["batch_id"]
    assert advice["demand"]["found"] == 2 and "top_skills" in advice["demand"] and advice["user_notes"] == "коротко"
    assert (await client.post("/api/analytics/resume-advice", json={"by": "nope"})).status_code == 400
    s = runtime.settings
    packet = json.loads((s.exchange_dir / "pending" / f"batch_{body['batch_id']}.json").read_text(encoding="utf-8"))
    task = next(t for t in packet["tasks"] if t["type"] == "resume_advice")
    assert task["vacancy"]["id"] == f"advice-{advice['id']}" and task["segment"]["label"] == "S"
    assert task["demand"]["top_skills"] and task["user_notes"] == "коротко" and task["want_letter"] is False
    result_file = {
        "protocol_version": 1,
        "batch_id": body["batch_id"],
        "profile_version": packet["profile_version"],
        "results": [
            {
                "vacancy_id": f"advice-{advice['id']}",
                "advice": {
                    "summary": "Резюме должно читаться как инженер интеграций.",
                    "edits": [
                        {"section": "Обо мне", "proposed": "Интегрирую LLM в продукты…", "reason": "просят LLM"},
                        {"section": "Навыки", "reason": "без текста — отбрасывается"},
                    ],
                    "add_skills": ["FastAPI"],
                    "soften": ["убрать «бизнес-ассистент» из заголовка"],
                    "questions": ["Есть ли опыт с Kafka?"],
                },
            }
        ],
    }
    (s.exchange_dir / "done" / f"batch_{body['batch_id']}.json").write_text(
        json.dumps(result_file, ensure_ascii=False), encoding="utf-8"
    )
    r = await client.post("/api/batches/import")
    assert r.status_code == 200, r.text
    outcome = r.json()["outcomes"][0]
    assert outcome["advices"] == 1 and outcome["status"] == "partial"  # the packet also carried vacancy tasks
    advices = (await client.get("/api/analytics/resume-advice")).json()
    assert advices[0]["status"] == "done" and advices[0]["result"]["edits"] == [
        {"section": "Обо мне", "proposed": "Интегрирую LLM в продукты…", "reason": "просят LLM"}
    ]
    assert advices[0]["result"]["questions"] == ["Есть ли опыт с Kafka?"]
    # a new request replaces a queued one and works for the whole base
    r = await client.post("/api/analytics/resume-advice", json={})
    assert r.json()["advice"]["segment_label"] == "все вакансии" and len(r.json()["advices"]) == 2
    assert ids["1"] != ids["2"]
