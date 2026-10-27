"""M5: sync of negotiations, external applications, events, notifications and Telegram delivery (mocked)."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import Application, Negotiation, NegotiationEvent, Notification, Vacancy
from app.main import app
from app.runtime import Runtime
from app.scheduler import daily_summary, sync_tick
from app.services.notify import emit_event
from app.services.state import get_system_state, read_capabilities, update_capability
from app.services.telegram import deliver_pending
from tests.integration.test_api_m2 import _full
from tests.integration.test_api_m4 import BASE, _consent, _mock_post, _seed

TOKEN = "123456:TESTTOKEN"
TG = f"https://api.telegram.org/bot{TOKEN}"


@pytest.fixture
async def api(settings: Settings):
    settings = settings.model_copy(
        update={
            "apply_min_delay_sec": 0,
            "apply_max_delay_sec": 0,
            "apply_window": "00:00-23:59",
            "apply_max_per_day": 5,
            "apply_max_per_hour": 5,
            "apply_max_per_employer_7d": 10,
            "batch_auto_create": False,
            "telegram_bot_token": TOKEN,
            "telegram_chat_id": "42",
        }
    )
    runtime = await Runtime.create(settings)
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


def _iso(dt: Any) -> str:
    return as_utc(dt).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _neg(nid: str, vacancy: dict[str, Any], **over: Any) -> dict[str, Any]:
    now = utcnow()
    item = {
        "id": nid,
        "state": {"id": "response", "name": "Отклик"},
        "hidden": False,
        "created_at": _iso(now - timedelta(hours=2)),
        "updated_at": _iso(now - timedelta(hours=2)),
        "url": f"https://api.hh.ru/negotiations/{nid}",
        "has_updates": False,
        "viewed_by_opponent": False,
        "messaging_status": "ok",
        "counters": {"messages": 0, "unread_messages": 0},
        "resume": {"id": "0123456789abcdef0123456789abcdef012345"},
        "vacancy": vacancy,
    }
    item.update(over)
    return item


def _short_vacancy(hh_id: str, name: str) -> dict[str, Any]:
    return {
        "id": hh_id,
        "name": name,
        "employer": {"id": "e" + hh_id, "name": f"Работодатель {hh_id}"},
        "alternate_url": f"https://hh.ru/vacancy/{hh_id}",
        "area": {"id": "1", "name": "Москва"},
        "salary": {"from": 200000, "to": None, "currency": "RUR", "gross": False},
    }


class NegotiationsMock:
    """`GET /negotiations` responder driven by a mutable list of items."""

    def __init__(self, m: respx.MockRouter) -> None:
        self.items: list[dict[str, Any]] = []
        self.calls: list[dict[str, list[str]]] = []
        m.get(f"{BASE}/negotiations").mock(side_effect=self.respond)

    def respond(self, request: httpx.Request) -> httpx.Response:
        q = parse_qs(urlparse(str(request.url)).query)
        self.calls.append(q)
        items = self.items
        if q.get("has_updates") == ["true"]:
            items = [i for i in items if i.get("has_updates")]
        elif q.get("status") == ["non_archived"]:
            items = [i for i in items if i["state"]["id"] != "hidden"]
        page = int(q.get("page", ["0"])[0])
        return httpx.Response(
            200,
            json={"found": len(items), "pages": 1, "page": page, "per_page": 50, "items": items if page == 0 else []},
        )


async def _sync(client: httpx.AsyncClient, runtime: Runtime, *, full: bool = False) -> dict[str, Any]:
    r = await client.post("/api/negotiations/sync", params={"full": str(full).lower()})
    assert r.status_code == 200, r.text
    await runtime.jobs.wait("sync")
    runs = (await client.get("/api/jobs")).json()["runs"]
    run = next(x for x in runs if x["job"] == "sync")
    assert run["status"] == "ok", run
    return run["stats"]


@respx.mock(assert_all_called=False)
async def test_sync_external_events_and_notifications(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    ids = await _seed(client, runtime, respx_mock, ["1", "2"])
    await _consent(client)

    # 1. the panel sends one application through the API → negotiation 777
    _mock_post(respx_mock, [httpx.Response(201, headers={"Location": "/negotiations/777"})])
    r = await client.post("/api/applications/queue", json={"vacancy_ids": [ids["1"]]})
    assert r.json()["queued"] == [ids["1"]]
    await runtime.jobs.wait("apply")
    apps = (await client.get("/api/applications")).json()["items"]
    assert apps[0]["hh_negotiation_id"] == "777" and apps[0]["negotiation"] is None
    feed = (await client.get("/api/notifications")).json()
    assert [n["event_type"] for n in feed] == ["applied"] and feed[0]["summary"].startswith("Отклик отправлен: ")

    # 2. first sync (status=all): 777 mirrors our application; 555 is an external invitation with a message
    neg = NegotiationsMock(respx_mock)
    # seconds, not minutes: the daily limit counts from local midnight, and a 30-minute-old
    # application would fall on yesterday when the test runs just after midnight
    external_created = utcnow() - timedelta(seconds=20)
    neg.items = [
        _neg("777", _short_vacancy("1", "Python developer 1")),
        _neg(
            "555",
            _short_vacancy("555", "External vacancy"),
            state={"id": "invitation", "name": "Приглашение"},
            viewed_by_opponent=True,
            has_updates=True,
            counters={"messages": 1, "unread_messages": 1},
            created_at=_iso(external_created),
            updated_at=_iso(utcnow() - timedelta(seconds=10)),
        ),
    ]
    respx_mock.get(f"{BASE}/vacancies/555").mock(
        return_value=httpx.Response(200, json=_full("555", name="External vacancy (full)"))
    )
    respx_mock.get(f"{BASE}/negotiations/555/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "found": 1,
                "pages": 1,
                "items": [
                    {
                        "id": "m1",
                        "text": "Здравствуйте! Приглашаем вас на интервью.",
                        "created_at": _iso(utcnow() - timedelta(minutes=15)),
                        "author": {"participant_type": "employer"},
                        "viewed_by_me": False,
                        "viewed_by_opponent": True,
                    },
                ],
            },
        )
    )
    stats = await _sync(client, runtime)
    assert stats["first_run"] is True and stats["items"] == 2 and stats["created"] == 2 and stats["external"] == 1
    assert stats["vacancies_created"] == 1 and stats["messages"] == 1 and stats["notifications"] == 0
    assert neg.calls[0]["status"] == ["all"] and neg.calls[0]["order_by"] == ["updated_at"]

    async with runtime.session_factory() as session:
        rows = {n.hh_id: n for n in (await session.execute(select(Negotiation))).scalars()}
        assert set(rows) == {"777", "555"}
        ext = rows["555"]
        assert ext.state_id == "invitation" and ext.viewed_by_opponent and ext.invited_at and ext.first_viewed_at
        assert ext.first_reaction_at is not None and ext.last_employer_message_at is not None
        vac = (await session.execute(select(Vacancy).where(Vacancy.hh_id == "555"))).scalar_one()
        assert vac.pipeline_status == "already_applied" and vac.name == "External vacancy (full)"
        assert vac.raw_full_json and vac.description_text  # full fetch happened
        ext_app = (
            await session.execute(select(Application).where(Application.hh_negotiation_id == "555"))
        ).scalar_one()
        assert ext_app.mode == "external" and ext_app.status == "sent"
        assert abs((as_utc(ext_app.sent_at) - external_created).total_seconds()) < 2
        assert rows["777"].application_id == apps[0]["id"] and rows["777"].vacancy_id == ids["1"]
        events = [
            (e.event_type, e.to_state)
            for e in (
                await session.execute(
                    select(NegotiationEvent)
                    .where(NegotiationEvent.negotiation_id == ext.id)
                    .order_by(NegotiationEvent.id)
                )
            ).scalars()
        ]
        assert events == [
            ("created", "invitation"),
            ("viewed", None),
            ("state_changed", "invitation"),
            ("message", None),
        ]
        state = await get_system_state(session)
        assert state.last_sync_at is not None and read_capabilities(state)["negotiations_list"] == "available"
        # first run never notifies
        kinds = {n.event_type for n in (await session.execute(select(Notification))).scalars()}
        assert kinds == {"applied"}
    status = (await client.get("/api/negotiations/status")).json()
    assert status["negotiations"] == 2 and status["with_updates"] == 1 and status["capability"] == "available"
    # the external application counts against the daily limit
    assert (await client.get("/api/applications/status")).json()["limits"]["today_sent"] == 2

    # 3. second sync: 777 becomes an invitation with a message → events + notifications
    neg.items[0].update(
        {
            "state": {"id": "invitation", "name": "Приглашение"},
            "viewed_by_opponent": True,
            "has_updates": True,
            "counters": {"messages": 1, "unread_messages": 1},
            "updated_at": _iso(utcnow()),
        }
    )
    neg.items[1]["has_updates"] = False
    respx_mock.get(f"{BASE}/negotiations/777/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "found": 1,
                "pages": 1,
                "items": [
                    {
                        "id": "m7",
                        "text": "Добрый день! Готовы обсудить оффер, когда удобно созвониться?",
                        "created_at": _iso(utcnow()),
                        "author": {"participant_type": "employer"},
                    }
                ],
            },
        )
    )
    stats = await _sync(client, runtime)
    assert stats["first_run"] is False and stats["updated"] >= 1 and stats["notifications"] == 1
    assert any(c.get("status") == ["non_archived"] for c in neg.calls) and any(
        c.get("has_updates") == ["true"] for c in neg.calls
    )
    detail = (await client.get("/api/negotiations/777")).json()
    assert detail["negotiation"]["state_id"] == "invitation" and detail["negotiation"]["invited_at"]
    assert [m["author_type"] for m in detail["messages"]] == ["employer"]
    assert [e["event_type"] for e in detail["events"]] == ["created", "viewed", "state_changed", "message"]
    feed = (await client.get("/api/notifications")).json()
    assert feed[0]["event_type"] == "invitation" and "Python developer 1" in feed[0]["summary"]
    assert "message" not in {n["event_type"] for n in feed}  # merged into the invitation
    assert feed[0]["payload"]["text"].startswith("Добрый день!")

    # 4. the «Отклики» tab filters (F-8.9.4)
    inv = (await client.get("/api/applications", params={"stage": "invitation"})).json()
    assert inv["total"] == 2 and inv["stage_counts"]["invitation"] == 2 and inv["stage_counts"]["no_answer"] == 0
    assert inv["items"][0]["negotiation"]["state_label"] == "приглашение"
    assert (await client.get("/api/applications", params={"stage": "viewed"})).json()["total"] == 2
    assert (await client.get("/api/applications", params={"mode": "external"})).json()["total"] == 1
    today = utcnow().astimezone(ZoneInfo(runtime.settings.timezone)).strftime("%Y-%m-%d")  # the filter is local-day
    assert (await client.get("/api/applications", params={"date_from": today, "date_to": today})).json()["total"] == 2
    assert (await client.get("/api/applications", params={"date_to": "2000-01-01"})).json()["total"] == 0

    # 5. statistics over the same data
    st = (await client.get("/api/stats", params={"period": "all"})).json()
    m = st["metrics"]
    assert m["sent"] == 2 and m["sent_api"] == 1 and m["sent_external"] == 1 and m["viewed"] == 2
    assert m["invitations"] == 2 and m["conv_sent_invited"] == 100.0 and m["median_reaction_hours"] is not None
    assert m["found"] == 3 and m["hh_requests"] > 0  # 2 scanned + 1 created by sync
    csv_resp = await client.get("/api/stats/export", params={"kind": "applications", "period": "all"})
    assert csv_resp.status_code == 200 and csv_resp.headers["content-type"].startswith("text/csv")
    lines = csv_resp.text.lstrip("﻿").splitlines()
    assert len(lines) == 3 and "invited" in lines[1]
    assert (await client.get("/api/stats", params={"period": "custom"})).status_code == 400

    # 6. Telegram delivery: applied + invitation → two messages, HTML, no token anywhere
    sent_bodies: list[dict[str, Any]] = []

    def tg_ok(request: httpx.Request) -> httpx.Response:
        sent_bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent_bodies)}})

    route = respx_mock.post(f"{TG}/sendMessage").mock(side_effect=tg_ok)
    summary = (await client.post("/api/notifications/telegram/flush")).json()
    assert summary["messages"] == 2 and summary["sent"] == 2 and summary["failed"] == 0
    assert all(
        b["chat_id"] == "42" and b["parse_mode"] == "HTML" and b["disable_web_page_preview"] for b in sent_bodies
    )
    assert any("Отклик отправлен" in b["text"] for b in sent_bodies) and any(
        "Приглашение" in b["text"] for b in sent_bodies
    )
    assert all(TOKEN not in b["text"] for b in sent_bodies)
    rows = (await client.get("/api/notifications", params={"channel": "telegram"})).json()
    assert {r["status"] for r in rows} == {"sent"} and all(TOKEN not in json.dumps(r) for r in rows)

    # 7. retries: 429 with retry_after keeps the row queued; a 400 fails it
    async with runtime.session_factory() as session:
        await emit_event(session, runtime.settings, "discard", {"name": "X", "employer": "Y"})
        await session.commit()
    route.mock(
        return_value=httpx.Response(
            429, json={"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 5}}
        )
    )
    summary = (await client.post("/api/notifications/telegram/flush")).json()
    assert summary["deferred"] == 1 and summary["sent"] == 0
    async with runtime.session_factory() as session:
        row = (
            await session.execute(
                select(Notification).where(Notification.event_type == "discard", Notification.channel == "telegram")
            )
        ).scalar_one()
        assert row.status == "queued" and row.attempts == 1 and row.next_attempt_at is not None
        assert "Too Many Requests" in row.error and TOKEN not in row.error
        route.mock(return_value=httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"}))
        result = await deliver_pending(
            session, runtime.settings, runtime.http, now=utcnow() + timedelta(seconds=10), force=True
        )
        await session.commit()
        assert result["failed"] == 1
        await session.refresh(row)
        assert row.status == "failed" and "chat not found" in row.error

    # 8. settings helpers: status, chat id detection, test message
    # the bot identity comes from getMe and is cached per token (any bot works, none is hardcoded)
    get_me = respx_mock.post(f"{TG}/getMe").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"username": "MyPanelBot", "first_name": "Панель"}}
        )
    )
    tg = (await client.get("/api/notifications/telegram")).json()
    assert tg["configured"] and tg["chat_id"] == "42" and tg["chat_id_source"] == "env" and len(tg["events"]) == 22
    assert (
        tg["bot_username"] == "MyPanelBot"
        and tg["bot_name"] == "Панель"
        and tg["bot_link"] == "https://t.me/MyPanelBot"
    )
    tg = (await client.get("/api/notifications/telegram")).json()
    assert tg["bot_username"] == "MyPanelBot" and get_me.call_count == 1  # cached
    respx_mock.post(f"{TG}/getUpdates").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {"chat": {"id": 99, "first_name": "Иван", "type": "private"}, "text": "/start"},
                    }
                ],
            },
        )
    )
    assert (await client.post("/api/notifications/telegram/detect-chat")).json() == {
        "chat_id": "99",
        "chat_title": "Иван",
    }
    tg = (await client.get("/api/notifications/telegram")).json()
    assert tg["chat_id"] == "99" and tg["chat_id_source"] == "settings"
    get_me.mock(return_value=httpx.Response(200, json={"ok": True, "result": {"username": "RenamedBot"}}))
    route.mock(side_effect=tg_ok)
    r = await client.post("/api/notifications/telegram/test")
    assert r.json() == {"ok": True, "bot": "RenamedBot", "chat_id": "99"} and sent_bodies[-1]["chat_id"] == "99"
    assert (await client.get("/api/notifications/telegram")).json()[
        "bot_username"
    ] == "RenamedBot"  # test refreshes cache


@respx.mock(assert_all_called=False)
async def test_sync_skips_and_capability(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _seed(client, runtime, respx_mock, ["1"])
    # 403 forbidden (not oauth) → capability unavailable, later ticks skip the job
    respx_mock.get(f"{BASE}/negotiations").mock(
        return_value=httpx.Response(403, json={"errors": [{"type": "forbidden", "value": "no_scope"}]})
    )
    stats = await _sync(client, runtime)
    assert "aborted" in stats
    async with runtime.session_factory() as session:
        assert read_capabilities(await get_system_state(session))["negotiations_list"] == "unavailable"
    await sync_tick(runtime)
    assert not runtime.jobs.is_running("sync")
    await client.post("/api/negotiations/sync")
    await runtime.jobs.wait("sync")
    runs = (await client.get("/api/jobs")).json()["runs"]
    assert runs[0]["job"] == "sync" and runs[0]["stats"]["skipped"].startswith("GET /negotiations недоступен")
    # back to available: an empty list is a valid successful run
    async with runtime.session_factory() as session:
        await update_capability(session, "negotiations_list", "unknown")
        await session.commit()
    respx_mock.get(f"{BASE}/negotiations").mock(
        return_value=httpx.Response(200, json={"found": 0, "pages": 0, "items": []})
    )
    stats = await _sync(client, runtime)
    assert stats["items"] == 0 and stats["first_run"] is True


async def test_batch_ready_cooldown_and_daily_summary(api) -> None:
    client, runtime = api
    async with runtime.session_factory() as session:
        first = await emit_event(session, runtime.settings, "batch_ready", {"batch_id": "a", "tasks": 3})
        second = await emit_event(session, runtime.settings, "batch_ready", {"batch_id": "b", "tasks": 4})
        await session.commit()
        tg_rows = [r for r in first + second if r.channel == "telegram"]
        assert [r.status for r in tg_rows] == ["queued", "suppressed"]
    await daily_summary(runtime)
    feed = (await client.get("/api/notifications")).json()
    today_local = utcnow().astimezone(ZoneInfo(runtime.settings.timezone)).strftime("%Y-%m-%d")
    assert feed[0]["event_type"] == "daily" and feed[0]["payload"]["date"] == today_local
    assert set(feed[0]["payload"]) >= {"found", "suitable", "sent", "viewed", "invitations", "awaiting", "no_answer"}
