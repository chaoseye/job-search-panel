"""Reminder «пора поднять резюме»: the scheduled tick, the «напомнить сейчас» endpoint, the skip rules and the
cron registration in the scheduler."""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx

from app.scheduler import build_scheduler
from app.services.resume_bump import bump_reminder_tick
from app.services.state import update_capability
from app.services.telegram import deliver_pending
from tests.integration.test_api_m6 import TG, api  # noqa: F401

__all__ = ["api"]
RESUME_ID = "0123456789abcdef0123456789abcdef012345"


@respx.mock(assert_all_called=False)
async def test_resume_bump_reminder(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    runtime.settings = runtime.settings.model_copy(update={"resume_bump_reminder_at": "08:45, 13:00,17:15"})
    sent: list[dict[str, Any]] = []
    respx_mock.post(f"{TG}/getMe").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"username": "B"}})
    )
    respx_mock.post(f"{TG}/sendMessage").mock(
        side_effect=lambda request: (sent.append(json.loads(request.content)), httpx.Response(200, json={"ok": True}))[
            1
        ]
    )

    # no resume id known yet → the link leads to the resumes list
    result = await bump_reminder_tick(runtime, slot="08:45")
    assert result["ok"] and result["telegram"] and result["url"] == "https://hh.ru/applicant/resumes"
    assert result["next"]  # a weekday schedule always has a next slot within a week

    # the id the user entered by hand → the resume page
    assert (await client.put("/api/settings", json={"resume_id": RESUME_ID})).status_code == 200
    result = await bump_reminder_tick(runtime, slot="13:00")
    assert result["url"] == f"https://hh.ru/resume/{RESUME_ID}"
    feed = (await client.get("/api/notifications")).json()
    assert feed[0]["event_type"] == "resume_bump" and feed[0]["payload"]["slot"] == "13:00"
    assert feed[0]["summary"].startswith("Пора поднять резюме (13:00) · следующее ")

    # immediate delivery: no 5-minute merge window
    async with runtime.session_factory() as session:
        summary = await deliver_pending(session, runtime.settings, runtime.http)
        await session.commit()
    assert summary["messages"] == 1 and summary["sent"] == 2  # both rows in one message (same event type)
    assert sent[-1]["chat_id"] == "42" and f'<a href="https://hh.ru/resume/{RESUME_ID}">' in sent[-1]["text"]
    assert "Поднять в поиске" in sent[-1]["text"] and "(13:00)" in sent[-1]["text"]

    # the Settings block shows the schedule
    tg = (await client.get("/api/notifications/telegram")).json()
    assert tg["resume_bump"] == {
        "at": ["08:45", "13:00", "17:15"],
        "days": "mon-fri",
        "label": "08:45, 13:00, 17:15 (пн–пт)",
        "event_enabled": True,
    }
    assert any(e["id"] == "resume_bump" and e["enabled"] for e in tg["events"])

    # «напомнить сейчас»: queued and flushed right away
    r = await client.post("/api/notifications/resume-bump")
    assert r.status_code == 200 and r.json()["ok"] and r.json()["telegram"]
    assert "(" not in sent[-1]["text"].split("\n")[0]  # no slot in a manual reminder
    assert f"https://hh.ru/resume/{RESUME_ID}" in sent[-1]["text"]

    # skipped when the panel bumps the resume itself (hh reopened publish + RESUME_AUTO_PUBLISH)
    async with runtime.session_factory() as session:
        await update_capability(session, "resume_publish", "available")
        await session.commit()
    runtime.settings = runtime.settings.model_copy(update={"resume_auto_publish": True})
    assert (await bump_reminder_tick(runtime, slot="17:15")) == {
        "skipped": "панель поднимает резюме сама (RESUME_AUTO_PUBLISH)"
    }
    assert (await client.post("/api/notifications/resume-bump")).json()["ok"]  # the button still works

    # the event is off in TELEGRAM_EVENTS → feed row only
    runtime.settings = runtime.settings.model_copy(update={"resume_auto_publish": False, "telegram_events": "daily"})
    result = await bump_reminder_tick(runtime, slot="17:15")
    assert result["ok"] and result["telegram"] is False

    # scheduler: one cron job per slot, weekdays only; nothing registered when the setting is empty
    jobs = {j.id: j for j in build_scheduler(runtime).get_jobs()}
    assert {"resume_bump_0845", "resume_bump_1300", "resume_bump_1715"} <= set(jobs)
    assert (
        "mon,tue,wed,thu,fri" in str(jobs["resume_bump_0845"].trigger) and jobs["resume_bump_0845"].args[1] == "08:45"
    )
    runtime.settings = runtime.settings.model_copy(update={"resume_bump_reminder_at": ""})
    assert not [j for j in build_scheduler(runtime).get_jobs() if j.id.startswith("resume_bump")]
