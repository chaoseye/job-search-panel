"""Words that keep a vacancy out of the Telegram bot: the Telegram row is suppressed or the list message loses the
matched vacancies, the UI feed keeps everything; the setting through the API and the Settings page status."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from httpx import ASGITransport

from app.config import Settings
from app.main import app
from app.runtime import Runtime
from app.services import tg_filter
from app.services.notify import emit_event

TOKEN = "123456:TESTTOKEN"
WORDS = ["продаж", "Курьер", "кассир", "холодные звонки", " продаж "]


@pytest.fixture
async def api(settings: Settings):
    runtime = await Runtime.create(
        settings.model_copy(update={"telegram_bot_token": TOKEN, "telegram_chat_id": "42", "batch_auto_create": False})
    )
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


def test_words_match_the_start_of_a_word_of_the_title() -> None:
    words = tg_filter.clean_words(WORDS)
    assert words == ["продаж", "Курьер", "кассир", "холодные звонки"]  # trimmed, no duplicates
    assert tg_filter.matched_word("Менеджер по продажам B2B", words) == "продаж"
    assert tg_filter.matched_word("Ночной специалист ПРОДАЖ в колл-центр", words) == "продаж"
    assert tg_filter.matched_word("Курьер на автомобиле (ст. м. «Пушкинская»)", words) == "Курьер"
    assert tg_filter.matched_word("Холодные звонки, ночные смены", words) == "холодные звонки"
    assert tg_filter.matched_word("Холодные напитки", words) is None  # a phrase matches as written
    assert tg_filter.matched_word("Разработчик CRM (отдел продаж)", words) == "продаж"
    assert tg_filter.matched_word("Инженер ПО «Распродажа»", words) is None  # inside a word — no
    assert tg_filter.matched_word("Python-разработчик", words) is None and tg_filter.matched_word(None, words) is None


def test_list_messages_lose_only_the_matched_vacancies() -> None:
    words = ["продаж"]
    digest = {"suitable": 2, "maybe": 0, "top": [{"name": "Менеджер по продажам"}, {"name": "Python-разработчик"}]}
    filtered, reason = tg_filter.telegram_payload("digest", digest, words)
    assert (
        reason is None and filtered == {**digest, "top": [{"name": "Python-разработчик"}]} and len(digest["top"]) == 2
    )
    hot = {"total": 2, "items": [{"name": "Специалист отдела продаж"}, {"name": "Руководитель продаж"}]}
    assert tg_filter.telegram_payload("hot", hot, words) == (None, "в названиях «продаж» — не для бота")
    part = {"total": 2, "items": [{"name": "Специалист отдела продаж"}, {"name": "QA-инженер"}]}
    assert tg_filter.telegram_payload("hot", part, words)[0] == {"total": 1, "items": [{"name": "QA-инженер"}]}
    same = {"top": [{"name": "QA-инженер"}]}
    assert tg_filter.telegram_payload("digest", same, words) == (same, None)
    assert tg_filter.telegram_payload("studio_reply", {"name": "Продажи-студия"}, words)[0] is not None  # not a vacancy
    assert tg_filter.telegram_payload("search_new", {"title": "Продажи"}, []) == ({"title": "Продажи"}, None)


@respx.mock(assert_all_called=False)
async def test_the_bot_does_not_get_them_the_feed_does(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    respx_mock.route(url__startswith=f"https://api.telegram.org/bot{TOKEN}/getMe").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"username": "ExamplePanelBot", "first_name": "HH"}}
        )
    )
    r = await client.put("/api/notifications/telegram/stop-words", json={"words": WORDS})
    assert r.status_code == 200 and r.json() == {"stop_words": ["продаж", "Курьер", "кассир", "холодные звонки"]}
    status = (await client.get("/api/notifications/telegram")).json()
    assert status["stop_words"] == ["продаж", "Курьер", "кассир", "холодные звонки"]

    async with runtime.session_factory() as session:
        sales = await emit_event(session, runtime.settings, "search_new", {"title": "Менеджер по продажам", "url": "u"})
        site = await emit_event(
            session, runtime.settings, "site_vacancy", {"title": "Курьер в магазин", "site": "Merlion"}
        )
        good = await emit_event(session, runtime.settings, "search_new", {"title": "Python-разработчик", "url": "u"})
        digest = await emit_event(
            session,
            runtime.settings,
            "digest",
            {"suitable": 2, "maybe": 0, "top": [{"name": "Кассир"}, {"name": "QA-инженер", "score": 80}]},
        )
        await session.commit()
    by_channel = {n: {r.channel: r for r in rows} for n, rows in
                  (("sales", sales), ("site", site), ("good", good), ("digest", digest))}  # fmt: skip
    assert all(rows["ui"].status == "sent" for rows in by_channel.values())  # the feed keeps everything
    assert (by_channel["sales"]["telegram"].status, by_channel["sales"]["telegram"].error) == (
        "suppressed",
        "в названии «продаж» — не для бота",
    )
    assert by_channel["site"]["telegram"].status == "suppressed" and by_channel["good"]["telegram"].status == "queued"
    tg_digest = json.loads(by_channel["digest"]["telegram"].payload_json or "{}")
    ui_digest = json.loads(by_channel["digest"]["ui"].payload_json or "{}")
    assert [i["name"] for i in tg_digest["top"]] == ["QA-инженер"] and len(ui_digest["top"]) == 2

    # an empty list turns the filter off
    r = await client.put("/api/notifications/telegram/stop-words", json={"words": []})
    assert r.json() == {"stop_words": []}
    async with runtime.session_factory() as session:
        again = await emit_event(session, runtime.settings, "search_new", {"title": "Курьер", "url": "u"})
        await session.commit()
    assert next(r for r in again if r.channel == "telegram").status == "queued"
