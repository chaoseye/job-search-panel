"""Telegram channels with vacancies: adding a channel (backfill, no notifications), a later sync with paging
(new posts → feed + Telegram), the feed filters and actions, channel settings, «в панель» for hh links,
the scheduler job, the bot command and the doctor row."""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx

from app.scheduler import build_scheduler
from app.services.bot_commands import handle_command
from app.services.channels import sync_channels
from app.services.telegram import deliver_pending
from tests.integration.test_api_m2 import BASE, _full, _login, _mock_dicts
from tests.integration.test_api_m6 import TG, api  # noqa: F401
from tests.unit.test_channels_units import RESUME_HTML, SHORT_HTML, VACANCY_HTML, page_html

__all__ = ["api"]
PREVIEW = "https://t.me/s/test_jobs"
GO_HTML = "Ищем middle Go-разработчика в продуктовую команду. Удалённо, зп от 250к ₽. Резюме присылайте @hr_go"
UK_HTML = "Шукаємо Media Buyer (FB, Gambling). Вимоги: досвід від 1 року. Умови: ставка + %, повний ремоут. Пиши в лс"


def _first_page() -> str:
    return page_html(
        [
            (101, "2026-09-15T08:00:00+00:00", VACANCY_HTML, "text"),
            (102, "2026-09-15T08:05:00+00:00", "Test Jobs pinned «Правила»", "service"),
            (103, "2026-09-15T08:10:00+00:00", RESUME_HTML, "reply"),
            (104, "2026-09-15T08:15:00+00:00", "", "photo"),
            (105, "2026-09-15T08:20:00+00:00", SHORT_HTML, "text"),
        ],
        before=101,
    )


def _mock_pages(m: respx.MockRouter, pages: dict[str | None, str]) -> None:
    """`None` → the latest page, `"121"` → ?before=121."""
    for before, html in pages.items():
        url = f"{PREVIEW}?before={before}" if before else PREVIEW
        m.get(url__eq=url).mock(return_value=httpx.Response(200, text=html))


@respx.mock(assert_all_called=False)
async def test_channel_lifecycle(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    _mock_pages(respx_mock, {None: _first_page()})
    respx_mock.get("https://t.me/s/private_one").mock(
        return_value=httpx.Response(302, headers={"location": "https://t.me/private_one"})
    )

    # bad input, unreadable channel: nothing is created
    assert (await client.post("/api/channels", json={"input": "https://example.com/x"})).status_code == 400
    r = await client.post("/api/channels", json={"input": "@private_one"})
    assert r.status_code == 400 and "предпросмотр недоступен" in r.json()["detail"]
    assert (await client.get("/api/channels")).json()["items"] == []

    # add: the first sync is a backfill — posts are stored as seen, nothing goes to Telegram
    r = await client.post("/api/channels", json={"input": "https://t.me/test_jobs"})
    assert r.status_code == 200, r.text
    ch = r.json()
    assert ch["username"] == "test_jobs" and ch["title"] == "Test Jobs" and ch["url"] == "https://t.me/test_jobs"
    assert ch["posts_count"] == 3 and ch["last_message_id"] == 105 and ch["last_new"] == 0 and ch["last_error"] is None
    assert (await client.post("/api/channels", json={"input": "test_jobs"})).status_code == 400  # duplicate
    feed = (await client.get("/api/channels/posts", params={"only_vacancies": "false"})).json()
    assert feed["total"] == 3 and all(p["status"] == "seen" for p in feed["items"]) and feed["new_count"] == 0
    assert [p["message_id"] for p in feed["items"]] == [105, 103, 101]  # newest first
    vac = feed["items"][2]
    assert vac["is_vacancy"] is True and vac["hh_links"] == ["https://hh.ru/vacancy/555"] and vac["vacancy_id"] is None
    assert vac["url"] == "https://t.me/test_jobs/101" and vac["title"].startswith("Senior Python Developer")
    assert (await client.get("/api/channels/posts")).json()["total"] == 1  # only vacancies by default
    assert (await client.get("/api/notifications", params={"channel": "telegram"})).json() == []
    summary = (await client.get("/api/channels/summary")).json()
    assert (
        summary["channels"] == 1
        and summary["enabled"] == 1
        and summary["new_posts"] == 0
        and summary["interval_min"] == 30
    )

    # a later sync: two pages of new posts (121–122 on the latest page, 106–107 behind the cursor)
    latest = page_html(
        [
            (121, "2026-09-15T12:00:00+00:00", GO_HTML, "text"),
            (122, "2026-09-15T12:10:00+00:00", RESUME_HTML, "text"),
            (123, "2026-09-15T12:20:00+00:00", UK_HTML, "text"),
        ],
        before=121,
    )
    middle = page_html(
        [
            (106, "2026-09-15T10:00:00+00:00", VACANCY_HTML, "text"),
            (107, "2026-09-15T10:30:00+00:00", "Всем привет! Сегодня без вакансий, отдыхаем всей командой.", "text"),
        ],
        before=106,
    )
    _mock_pages(respx_mock, {None: latest, "121": middle, "106": _first_page()})
    r = await client.post(f"/api/channels/{ch['id']}/sync")
    assert r.status_code == 200, r.text
    ch = r.json()
    assert ch["last_message_id"] == 123 and ch["last_new"] == 5 and ch["posts_count"] == 8 and ch["new_posts"] == 2
    new = (await client.get("/api/channels/posts", params={"only_new": "true"})).json()
    assert [p["message_id"] for p in new["items"]] == [121, 106] and new["new_count"] == 2
    assert (await client.get("/api/channels/summary")).json()["new_posts"] == 2
    everything = (await client.get("/api/channels/posts", params={"only_vacancies": "false"})).json()
    assert everything["total"] == 8 and sum(p["status"] == "new" for p in everything["items"]) == 5
    # the Ukrainian post is stored with its language but is not a vacancy (CHANNEL_LANGUAGES=ru,en by default)
    uk_post = next(p for p in everything["items"] if p["message_id"] == 123)
    assert uk_post["lang"] == "uk" and uk_post["is_vacancy"] is False
    assert next(p for p in everything["items"] if p["message_id"] == 121)["lang"] == "ru"
    assert (await client.get("/api/channels/summary")).json()["languages"] == ["en", "ru"]
    # search is case-insensitive for Cyrillic too
    assert (await client.get("/api/channels/posts", params={"q": "go-РАЗРАБОТЧИКА"})).json()["total"] == 1

    # Telegram: one queued row per new vacancy post, merged into one message
    rows = (await client.get("/api/notifications", params={"channel": "telegram"})).json()
    assert len(rows) == 2 and {r["event_type"] for r in rows} == {"channel_post"}
    payloads = {r["payload"]["url"]: r["payload"] for r in rows}
    assert payloads["https://t.me/test_jobs/106"]["hh_url"] == "https://hh.ru/vacancy/555"
    assert (
        payloads["https://t.me/test_jobs/121"]["hh_url"] is None
        and payloads["https://t.me/test_jobs/121"]["channel"] == "Test Jobs"
    )
    assert all(r["summary"].startswith("Вакансия из Telegram-канала:") and "@test_jobs" in r["summary"] for r in rows)
    sent: list[dict[str, Any]] = []
    respx_mock.post(f"{TG}/sendMessage").mock(
        side_effect=lambda request: (sent.append(json.loads(request.content)), httpx.Response(200, json={"ok": True}))[
            1
        ]
    )
    async with runtime.session_factory() as session:
        delivered = await deliver_pending(session, runtime.settings, runtime.http, force=True)
        await session.commit()
    assert delivered["messages"] == 1 and delivered["sent"] == 2
    text = sent[-1]["text"]
    assert text.startswith("📣 <b>Новые вакансии в Telegram-каналах: 2</b>")
    assert '<a href="https://t.me/test_jobs/106">' in text and '<a href="https://hh.ru/vacancy/555">hh.ru</a>' in text
    assert "@test_jobs" in text and "TESTTOKEN" not in text

    # nothing new on the next sync, no duplicates
    _mock_pages(respx_mock, {None: latest})
    r = await client.post("/api/channels/sync")
    assert r.status_code == 200 and r.json()["items"][0]["last_new"] == 0 and r.json()["items"][0]["posts_count"] == 8

    # feed actions: seen / hide / restore / mark all seen
    post_121 = new["items"][0]["id"]
    assert (await client.post(f"/api/channels/posts/{post_121}/seen")).json()["status"] == "seen"
    assert (await client.get("/api/channels/summary")).json()["new_posts"] == 1
    hidden = (await client.post(f"/api/channels/posts/{post_121}/hide")).json()
    assert hidden["status"] == "hidden"
    assert (await client.get("/api/channels/posts")).json()["total"] == 2  # 101 and 106; hidden 121 is out
    assert (await client.get("/api/channels/posts", params={"include_hidden": "true"})).json()["total"] == 3
    assert (await client.post(f"/api/channels/posts/{post_121}/restore")).json()["status"] == "seen"
    assert (await client.post(f"/api/channels/posts/{post_121}/nope")).status_code == 404
    assert (await client.post("/api/channels/posts/mark-seen", json={})).json()["marked"] == 4  # 106, 107, 122, 123
    assert (await client.get("/api/channels/summary")).json()["new_posts"] == 0

    # channel settings: mode «all» flags every post, keywords narrow it, notify/enabled toggles
    r = await client.put(f"/api/channels/{ch['id']}", json={"mode": "all"})
    assert r.status_code == 200 and r.json()["mode"] == "all"
    assert (await client.get("/api/channels/posts")).json()["total"] == 7  # all but the Ukrainian one
    # allowing every language brings it back after a reflag; restoring the default hides it again
    runtime.settings = runtime.settings.model_copy(update={"channel_languages": ""})
    assert (await client.post("/api/channels/reflag")).json()["changed"] == 1
    assert (await client.get("/api/channels/posts")).json()["total"] == 8
    runtime.settings = runtime.settings.model_copy(update={"channel_languages": "ru, en"})
    assert (await client.post("/api/channels/reflag")).json()["changed"] == 1
    r = await client.put(f"/api/channels/{ch['id']}", json={"keywords": "python, go"})
    assert r.json()["keywords"] == "python, go"
    assert {p["message_id"] for p in (await client.get("/api/channels/posts")).json()["items"]} == {
        101,
        103,
        106,
        121,
        122,
    }
    r = await client.put(
        f"/api/channels/{ch['id']}", json={"mode": "vacancies", "keywords": "", "notify": False, "enabled": False}
    )
    assert r.json()["notify"] is False and r.json()["enabled"] is False and r.json()["keywords"] is None
    assert (await client.get("/api/channels/posts")).json()["total"] == 3  # 101, 106, 121
    assert (await client.put(f"/api/channels/{ch['id']}", json={"mode": "weird"})).status_code == 400
    assert (await client.put("/api/channels/999", json={"mode": "all"})).status_code == 404
    # a disabled channel is skipped by the scheduled job
    assert (await sync_channels(runtime))["skipped"] == "каналов нет"
    assert (await client.post(f"/api/channels/{ch['id']}/sync")).status_code == 409  # a disabled channel is not read
    # notify=False: a new vacancy post still lands in the panel feed, but no Telegram row is queued
    await client.put(f"/api/channels/{ch['id']}", json={"enabled": True})
    _mock_pages(
        respx_mock,
        {None: page_html([(130, "2026-09-15T13:00:00+00:00", GO_HTML, "text")], before=130), "130": latest},
    )
    r = await client.post(f"/api/channels/{ch['id']}/sync")
    assert r.json()["last_new"] == 1 and r.json()["new_posts"] == 1
    ui = (await client.get("/api/notifications", params={"channel": "ui"})).json()
    assert ui[0]["event_type"] == "channel_post" and ui[0]["payload"]["url"] == "https://t.me/test_jobs/130"
    tg_rows = (await client.get("/api/notifications", params={"channel": "telegram"})).json()
    assert all(r["payload"]["url"] != "https://t.me/test_jobs/130" for r in tg_rows)
    assert (await client.get("/api/channels/posts", params={"only_new": "true"})).json()["items"][0][
        "notified_at"
    ] is None
    await client.put(f"/api/channels/{ch['id']}", json={"enabled": False})
    assert (await client.post("/api/channels/posts/mark-seen", json={})).json()["marked"] == 1

    # bot command
    async with runtime.session_factory() as session:
        assert "Новых вакансий в Telegram-каналах нет" in await handle_command(runtime, session, "/channels")
        await client.put(f"/api/channels/{ch['id']}", json={"enabled": True})
        from sqlalchemy import update

        from app.db.models import ChannelPost

        await session.execute(update(ChannelPost).where(ChannelPost.message_id == 106).values(status="new"))
        await session.commit()
        text = await handle_command(runtime, session, "/channels")
    assert text.startswith("📣 <b>Новые вакансии в каналах: 1</b>") and "https://t.me/test_jobs/106" in text

    # delete: posts go with the channel
    assert (await client.delete(f"/api/channels/{ch['id']}")).json() == {"ok": True}
    assert (await client.get("/api/channels")).json()["items"] == []
    assert (await client.get("/api/channels/posts", params={"only_vacancies": "false"})).json()["total"] == 0
    assert (await client.delete(f"/api/channels/{ch['id']}")).status_code == 404


@respx.mock(assert_all_called=False)
async def test_post_to_panel_adds_the_hh_vacancy(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    _mock_pages(respx_mock, {None: _first_page()})
    ch = (await client.post("/api/channels", json={"input": "@test_jobs"})).json()
    post = (await client.get("/api/channels/posts")).json()["items"][0]
    assert post["hh_links"] == ["https://hh.ru/vacancy/555"]
    r = await client.post(f"/api/channels/posts/{post['id']}/to-panel", json={})
    assert r.status_code == 400 and "войдите" in r.json()["detail"]
    await _login(client, respx_mock)
    _mock_dicts(respx_mock)
    respx_mock.get(f"{BASE}/vacancies/555").mock(return_value=httpx.Response(200, json=_full("555")))
    r = await client.post(f"/api/channels/posts/{post['id']}/to-panel", json={"create_packet": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [a["hh_id"] for a in body["added"]] == ["555"] and body["errors"] == []
    assert body["post"]["vacancy_id"] == body["added"][0]["id"] and body["post"]["vacancy_status_label"]
    # the feed keeps the link to the panel vacancy
    post = (await client.get("/api/channels/posts")).json()["items"][0]
    assert post["vacancy_id"] == body["added"][0]["id"] and post["vacancy_status"] == body["added"][0]["status"]
    # a post without hh links cannot be added
    other = (await client.get("/api/channels/posts", params={"only_vacancies": "false"})).json()["items"][0]
    assert (await client.post(f"/api/channels/posts/{other['id']}/to-panel", json={})).status_code == 400
    # a new scan that finds the vacancy links later posts automatically: the second sync sees 555 in the base
    _mock_pages(
        respx_mock,
        {None: page_html([(130, "2026-09-16T08:00:00+00:00", VACANCY_HTML, "text")], before=130), "130": _first_page()},
    )
    assert (await client.post(f"/api/channels/{ch['id']}/sync")).json()["last_new"] == 1
    newest = (await client.get("/api/channels/posts", params={"only_new": "true"})).json()["items"][0]
    assert newest["message_id"] == 130 and newest["vacancy_id"] == body["added"][0]["id"]


FOLDER_PAGE = (
    '<html><body><div class="tgme_page_description">\n  You can add <b>14 chats</b> from the <strong>Демо-подборка</strong> list on '
    '</b>Telegram</b>.\n</div><div class="tgme_page_action"><a class="tgme_action_button_new shine" '
    'href="tg://addlist?slug=Demo_Folder_42xy">View Chats</a></div></body></html>'
)


@respx.mock(assert_all_called=False)
async def test_bulk_add_and_folder_links(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    _mock_pages(respx_mock, {None: _first_page()})
    respx_mock.get("https://t.me/s/go_jobs").mock(
        return_value=httpx.Response(
            200, text=page_html([(7, "2026-09-15T09:00:00+00:00", GO_HTML, "text")], title="Go")
        )
    )
    respx_mock.get("https://t.me/s/private_one").mock(
        return_value=httpx.Response(302, headers={"location": "https://t.me/private_one"})
    )
    respx_mock.get("https://t.me/addlist/Demo_Folder_42xy").mock(return_value=httpx.Response(200, text=FOLDER_PAGE))
    text = "https://t.me/addlist/Demo_Folder_42xy\n@test_jobs, https://t.me/go_jobs\n@private_one\nne-kanal! @test_jobs"
    r = await client.post("/api/channels/bulk", json={"input": text, "mode": "all"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [c["username"] for c in body["added"]] == ["test_jobs", "go_jobs"]  # the repeated @test_jobs is skipped
    assert all(c["mode"] == "all" and c["posts_count"] for c in body["added"])
    errors = {e["input"]: e["error"] for e in body["errors"]}
    assert set(errors) == {"https://t.me/addlist/Demo_Folder_42xy", "@private_one", "ne-kanal!"}
    assert "папка «Демо-подборка» (14 чатов)" in errors["https://t.me/addlist/Demo_Folder_42xy"]
    assert "списком" in errors["https://t.me/addlist/Demo_Folder_42xy"]
    assert "предпросмотр недоступен" in errors["@private_one"] and "публичный канал" in errors["ne-kanal!"]
    assert len((await client.get("/api/channels")).json()["items"]) == 2
    # the single-channel endpoint explains a folder link too (without fetching the page)
    r = await client.post("/api/channels", json={"input": "https://t.me/addlist/zzz"})
    assert r.status_code == 400 and "папку" in r.json()["detail"]
    # reflag after a dictionary change: flags are recomputed with the channel settings, unchanged rows are counted
    r = await client.post("/api/channels/reflag")
    assert r.status_code == 200 and r.json() == {"posts": 4, "changed": 0, "channels": 2}
    go = next(c for c in body["added"] if c["username"] == "go_jobs")
    from sqlalchemy import update

    from app.db.models import ChannelPost

    async with runtime.session_factory() as session:
        await session.execute(update(ChannelPost).where(ChannelPost.channel_id == go["id"]).values(is_vacancy=False))
        await session.commit()
    assert (await client.post("/api/channels/reflag", params={"channel_id": go["id"]})).json()["changed"] == 1


@respx.mock(assert_all_called=False)
async def test_backlog_is_delivered_in_chunks(api, respx_mock: respx.MockRouter) -> None:
    """After the panel was down for a while every new vacancy post must reach the bot, not «… и ещё N»."""
    from app.services.notify import emit_event

    client, runtime = api
    sent: list[dict[str, Any]] = []
    respx_mock.post(f"{TG}/sendMessage").mock(
        side_effect=lambda request: (sent.append(json.loads(request.content)), httpx.Response(200, json={"ok": True}))[
            1
        ]
    )
    async with runtime.session_factory() as session:
        for i in range(23):
            await emit_event(
                session,
                runtime.settings,
                "channel_post",
                {"channel": "Jobs", "username": "jobs", "title": f"Vacancy {i}", "url": f"https://t.me/jobs/{i}"},
            )
        await session.commit()
        delivered = await deliver_pending(session, runtime.settings, runtime.http, force=True)
        await session.commit()
    assert delivered == {"sent": 23, "failed": 0, "deferred": 0, "messages": 3}
    texts = [m["text"] for m in sent]
    assert texts[0].startswith("📣 <b>Новые вакансии в Telegram-каналах: 23</b> (часть 1 из 3)")
    assert texts[2].startswith("📣 <b>Новые вакансии в Telegram-каналах: 23</b> (часть 3 из 3)")
    assert all("и ещё" not in t for t in texts)
    assert sum(t.count("https://t.me/jobs/") for t in texts) == 23
    rows = (await client.get("/api/notifications", params={"channel": "telegram", "limit": 30})).json()
    assert len(rows) == 23 and all(r["status"] == "sent" for r in rows)


async def test_scheduler_and_doctor_rows(api) -> None:
    client, runtime = api
    jobs = {j.id: j for j in build_scheduler(runtime).get_jobs()}
    assert "channel_sync" in jobs and "minutes=30" in str(jobs["channel_sync"].trigger).replace("0:30:00", "minutes=30")
    runtime.settings = runtime.settings.model_copy(update={"channel_sync_min": 0})
    assert "channel_sync" not in {j.id for j in build_scheduler(runtime).get_jobs()}
    from app.services.doctor import INFO, collect

    report = await collect(runtime.settings.model_copy(update={"app_port": 8099}))
    row = next(c for c in report.checks if c.id == "channels")
    assert row.status == INFO and "не добавлены" in row.detail and "Каналы" in row.hint
    assert (await client.get("/api/channels")).json() == {
        "items": [],
        "interval_min": 0,
        "languages": ["en", "ru"],
        "running": False,
    }
