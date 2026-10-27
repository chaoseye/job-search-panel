"""Tenders of workspace.ru: parsing the list and the card, robots.txt with `*` rules, the
score against the questionnaire, the job (the first read fills the table without messages, later new suitable
tenders reach the bot, nothing but the allowed pages is requested), the page's API and the answer through Claude."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select

from app.config import Settings
from app.db.base import utcnow
from app.db.models import CandidateProfile, Notification, WsTender, WsTenderReplyRequest
from app.exchange.importer import import_result_file
from app.exchange.packets import done_dir
from app.main import app
from app.runtime import Runtime
from app.services import ws_tenders
from app.services.notify import render_event
from app.services.preferences import Preferences, SkillWeight
from app.services.robots_rules import RobotsRules

WS = "https://workspace.ru"
ROBOTS = """User-Agent: *
Allow: /bitrix/js/
Disallow: /bitrix/
Disallow: /*/*?page=*
Disallow: /tenders/?
Disallow: /tenders/mobile/
Disallow: /tenders/private-tender-*/
Disallow: /redirect/
"""


@pytest.fixture
async def api(settings: Settings):
    runtime = await Runtime.create(settings.model_copy(update={"batch_auto_create": False, "ws_tenders_sync_min": 0}))
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


def _card(ws_id: int, title: str, *, state: str = "open", budget: str = "до&nbsp;150 000&nbsp;", deadline: str = "30 декабря 2099",
          slug: str = "tender") -> str:  # fmt: skip
    return f"""
    <div class="vacancies__card _tender" data-tender-card>
      <div class="b-tender__card-bottom">
        <div class="tendercart-type__status-public" data-change-status-text="{state}">статус</div>
        <span class="reference _views">224</span>
      </div>
      <div class="b-tender__block--title">
        <div class="b-tender__title _wide"><a href="/tenders/{slug}-{ws_id}/">{title}</a></div>
        <div class="b-tender__info-item-text">{budget}<span class="rub"></span></div>
      </div>
      <div class="b-tender__info">
        <div class="b-tender__info-item"><div class="b-tender__info-item-title">Опубликован</div>
          <div class="b-tender__info-item-text">24 сентября 2026</div></div>
        <div class="b-tender__info-item"><div class="b-tender__info-item-title">Крайний срок приема заявок:</div>
          <div class="b-tender__info-item-text">{deadline}</div></div>
      </div>
    </div>"""


def _list(*cards: str) -> str:
    return f"<html><body><div data-tender-public-list>{''.join(cards)}</div></body></html>"


def _detail(description: str, *, service: str = "Разработка сайтов под ключ", finished: bool = False) -> str:
    status = (
        '<div class="tendercart-type__status-public _finished">Завершён</div>'
        if finished
        else ('<div class="tendercart-type__status-public">Идет прием заявок</div>')
    )
    return f"""<html><body>{status}
    <h1 class="tendercard__title">Тендер</h1>
    <div class="tendercard__desc">{description}<br/>В отклике просим указать:<ul><li>срок;</li><li>стоимость.</li></ul></div>
    <div class="b-tender__info _desc">
      <div class="b-tender__info-item"><div class="b-tender__info-item-title">Бюджет:</div>
        <div class="b-tender__info-item-text">до&nbsp;150 000&nbsp;рублей</div></div>
      <div class="b-tender__info-item"><div class="b-tender__info-item-title">Опубликован:</div>
        <div class="b-tender__info-item-text">24 сентября 2026</div></div>
      <div class="b-tender__info-item"><div class="b-tender__info-item-title">Крайний срок приема заявок:</div>
        <div class="b-tender__info-item-text">30 декабря 2099</div></div>
    </div>
    <div class="tendercard-conditions"><div class="tendercard-conditions__title">Требуемая услуга</div>
      <div class="tendercard-conditions__content"><a href="/web-development/"><i></i><span>{service}</span></a></div></div>
    <div class="tendercard-conditions"><div class="tendercard-conditions__title">Тип сайта</div>
      <div class="tendercard-conditions__content"><span>Лендинг пейдж</span></div></div>
    <div class="tendercard-conditions"><div class="tendercard-conditions__title">В тендере участвуют</div>
      <div class="tendercard-conditions__content"><ul><li><a href="/contractors/a/">A</a></li><li><a href="/contractors/b/">B</a></li></ul></div></div>
    <div class="card-info__inner"><div class="card-info__title">Oрганизатор</div><p class="card-info__desc">Юридическое лицо</p></div>
    <div class="tender-aside__seo">Этот тендер организован заказчиком из Ульяновска. Интересно узнать?</div>
    </body></html>"""  # fmt: skip


# ---- parsing and robots.txt ----------------------------------------------------------------------------------------


def test_list_and_card_are_parsed() -> None:
    items = ws_tenders.parse_list(
        _list(
            _card(18968, "Сборка MVP", slug="sborka-mvp"),
            _card(
                18884, "Электронная очередь", state="close", budget="400 000 - 1 200 000", deadline="21 августа 2026"
            ),
            '<div data-tender-card><div class="b-tender__title"><a href="/tenders/private-tender-44/">секрет</a></div></div>',
        )
    )
    assert [(i.ws_id, i.state, i.budget, i.published_on, i.deadline_on, i.views) for i in items] == [
        (18968, "open", "до 150 000", "2026-09-24", "2099-12-30", 224),
        (18884, "closed", "400 000 - 1 200 000", "2026-09-24", "2026-08-21", 224),
    ]
    assert items[0].url == f"{WS}/tenders/sborka-mvp-18968/"
    detail = ws_tenders.parse_detail(_detail("Нужен сайт с формой заявки."))
    assert detail.description == "Нужен сайт с формой заявки.\nВ отклике просим указать:\n— срок;\n— стоимость."
    assert (detail.state, detail.budget, detail.deadline_on, detail.service) == (
        "open",
        "до 150 000 рублей",
        "2099-12-30",
        "Разработка сайтов под ключ",
    )
    assert detail.conditions == [
        {"title": "Требуемая услуга", "value": "Разработка сайтов под ключ"},
        {"title": "Тип сайта", "value": "Лендинг пейдж"},
    ]
    assert (detail.participants, detail.organizer, detail.city) == (2, "Юридическое лицо", "Ульяновска")
    assert ws_tenders.parse_detail(_detail("x", finished=True)).state == "closed"
    assert [ws_tenders.budget_max(b) for b in ("до 150 000 рублей", "400 000 - 1 200 000", "от 900 000", None)] == [
        150000,
        1200000,
        900000,
        None,
    ]
    assert ws_tenders.parse_date("1 октября 2026") == "2026-10-01" and ws_tenders.parse_date("вчера") is None


def test_robots_rules_with_wildcards() -> None:
    rules = RobotsRules.parse(ROBOTS, "JobSearchPanel/1.0")
    assert rules.allowed("/tenders/") and rules.allowed("/tenders/crm/") and rules.allowed("/tenders/sayt-18967/")
    assert not rules.allowed("/tenders/crm/?page=2") and not rules.allowed("/tenders/?SORT=budget")
    assert not rules.allowed("/tenders/mobile/") and not rules.allowed("/tenders/private-tender-44/")
    assert rules.allowed("/bitrix/js/x.js") and not rules.allowed("/bitrix/admin/")  # the longer rule wins
    own = RobotsRules.parse(
        "User-agent: JobSearchPanel\nDisallow: /\n\nUser-agent: *\nDisallow:\n", "JobSearchPanel/1.0"
    )
    assert not own.allowed("/tenders/")
    anchored = RobotsRules.parse("User-agent: *\nDisallow: /*.pdf$\n")
    assert not anchored.allowed("/a/b.pdf") and anchored.allowed("/a/b.pdf?x=1")


def test_score_against_the_questionnaire() -> None:
    prefs = Preferences(
        skills_weights=[SkillWeight(name="Python", weight=5), SkillWeight(name="бот", weight=3)],
        target_keywords=["автоматизация"],
        stop_words_title=["SEO"],
    )
    cfg = {"sections": ["crm"], "min_score": 30, "rates": ""}
    row = WsTender(
        ws_id=1, url="u", title="Telegram-бот для записи", description="Python, автоматизация записи", state="open"
    )
    score, notes, relevant = ws_tenders.score_tender(row, prefs, cfg, "2026-09-24")
    assert (score, relevant) == (4 * 5 + 4 * 3 + 8, True) and notes == ["совпадает: Python, бот, автоматизация"]
    row.category = "crm"
    assert ws_tenders.score_tender(row, prefs, cfg, "2026-09-24")[0] == 40 + ws_tenders.SECTION_BONUS
    seo = WsTender(ws_id=2, url="u", title="SEO для сайта на Python", category="crm", state="open")
    assert ws_tenders.score_tender(seo, prefs, cfg, "2026-09-24")[2] is False  # a title stop word
    late = WsTender(ws_id=3, url="u", title="Бот", category="crm", state="open", deadline_on="2026-09-01")
    assert ws_tenders.score_tender(late, prefs, cfg, "2026-09-24")[2] is False  # the deadline has passed
    other = WsTender(ws_id=4, url="u", title="Логотип для кафе", state="open")
    assert ws_tenders.score_tender(other, prefs, cfg, "2026-09-24") == (0, [], False)


# ---- the job -------------------------------------------------------------------------------------------------------


def _mock_site(m: respx.MockRouter, pages: dict[str, str], details: dict[int, str]) -> dict[str, Any]:
    routes: dict[str, Any] = {"robots": m.get(f"{WS}/robots.txt").mock(return_value=httpx.Response(200, text=ROBOTS))}
    for path, html in pages.items():
        routes[path] = m.get(f"{WS}{path}").mock(return_value=httpx.Response(200, html=html))
    for ws_id, html in details.items():
        routes[ws_id] = m.get(url__regex=rf"^{WS}/tenders/[a-z0-9-]+-{ws_id}/$").mock(
            return_value=httpx.Response(200, html=html)
        )
    routes["other"] = m.route().mock(return_value=httpx.Response(404))
    return routes


@respx.mock(assert_all_called=False)
async def test_first_read_fills_then_new_suitable_tenders_reach_the_bot(
    api, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, runtime = api
    monkeypatch.setattr(ws_tenders, "PAUSE_SEC", 0)
    mvp = _card(18968, "Сборка MVP", slug="sborka-mvp")
    banner = _card(18969, "Нарисовать баннер под печать", slug="banner")
    closed = _card(18884, "Электронная очередь", state="close", deadline="21 августа 2026", slug="ochered")
    ai = _card(18936, "Доработка CRM на Битрикс24", slug="crm-bitrix")
    routes = _mock_site(
        respx_mock,
        {
            "/tenders/": _list(mvp, banner, closed),
            "/tenders/web-development/": _list(mvp),
            "/tenders/crm/": _list(ai),
            "/tenders/apps-development/": _list(),
        },
        {
            18968: _detail("Лендинг и анкета"),
            18969: _detail("Баннер 3×6 м", service="Дизайн"),
            18936: _detail("Битрикс24"),
        },
    )
    first = await ws_tenders.sync_tenders(runtime)
    assert (first["backfill"], first["found"], first["added"], first["details"], first["notified"]) == (
        True,
        4,
        4,
        3,
        0,
    )
    assert routes["other"].call_count == 0  # the closed tender's card and nothing else was asked
    async with runtime.session_factory() as session:
        rows = {r.ws_id: r for r in (await session.execute(select(WsTender))).scalars()}
        events = (await session.execute(select(Notification))).scalars().all()
    assert (rows[18968].category, rows[18936].category, rows[18969].category) == ("web-development", "crm", None)
    assert {w for w, r in rows.items() if r.relevant} == {18968, 18936}
    assert rows[18968].city == "Ульяновска" and rows[18968].participants == 2 and rows[18884].state == "closed"
    assert events == []  # the first read tells nothing

    # the questionnaire: a stop word keeps an SEO tender quiet
    r = await client.put("/api/profile/preferences", json={"stop_words_title": ["SEO"]})
    assert r.status_code == 200, r.text
    bot = _card(18971, "Telegram-бот для записи клиентов", slug="bot")
    seo = _card(18972, "SEO-продвижение сайта", slug="seo")
    respx_mock.clear()
    routes = _mock_site(
        respx_mock,
        {
            "/tenders/": _list(seo, mvp, banner),
            "/tenders/web-development/": _list(mvp),
            "/tenders/crm/": _list(bot, ai),
            "/tenders/apps-development/": _list(),
        },
        {18971: _detail("Бот в Telegram"), 18972: _detail("Продвижение"), 18968: _detail("x")},
    )
    second = await ws_tenders.sync_tenders(runtime)
    assert (second["backfill"], second["added"], second["details"], second["notified"]) == (False, 2, 2, 1)
    assert routes[18968].call_count == 0 and routes["other"].call_count == 0  # known cards are not read again
    async with runtime.session_factory() as session:
        events = [
            json.loads(n.payload_json or "{}")
            for n in (await session.execute(select(Notification))).scalars()
            if n.event_type == "ws_tender"
        ]
    assert [e["title"] for e in events] == ["Telegram-бот для записи клиентов"]
    assert events[0]["url"] == f"{WS}/tenders/bot-18971/" and events[0]["section"] == "CRM, 1С, ПО, чат-боты"
    summary = (await client.get("/api/tenders/summary")).json()
    assert summary == {"new_relevant": 3}  # MVP and Bitrix24 from the first read, the bot


@respx.mock(assert_all_called=False)
async def test_workspace_not_answering_is_a_failed_run(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    respx_mock.get(f"{WS}/robots.txt").mock(side_effect=httpx.ConnectTimeout("timed out"))
    result = await ws_tenders.sync_tenders(runtime)
    assert result["job_failed"].startswith("robots.txt: workspace.ru не ответил")
    respx_mock.get(f"{WS}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /tenders/\n")
    )
    result = await ws_tenders.sync_tenders(runtime)
    assert result["pages"] == 0 and "закрыто в robots.txt" in result["errors"][0] and result["job_failed"]


# ---- the page and the answer through Claude --------------------------------------------------------------------------


async def _tenders(runtime: Runtime) -> dict[str, int]:
    now = utcnow()
    async with runtime.session_factory() as session:
        session.add(
            CandidateProfile(version=1, is_active=True, preferences_json="{}", profile_md="# профиль", hash="h")
        )
        rows = {
            "mvp": WsTender(ws_id=1, url=f"{WS}/tenders/mvp-1/", title="Сборка MVP", category="web-development",
                            description="Нужен лендинг", budget="до 150 000 рублей", budget_max=150000,
                            deadline_on="2099-12-30", published_on="2026-09-24", relevant=True, fit_score=60,
                            first_seen_at=now),
            "bot": WsTender(ws_id=2, url=f"{WS}/tenders/bot-2/", title="Бот", category="crm", relevant=True,
                            fit_score=30, deadline_on="2099-10-01", published_on="2026-09-23", first_seen_at=now),
            "logo": WsTender(ws_id=3, url=f"{WS}/tenders/logo-3/", title="Логотип", category=None, relevant=False,
                             fit_score=0, first_seen_at=now),
            "old": WsTender(ws_id=4, url=f"{WS}/tenders/old-4/", title="Старый", category="crm", relevant=True,
                            state="closed", fit_score=50, first_seen_at=now),
        }  # fmt: skip
        session.add_all(rows.values())
        await ws_tenders.save_tender_settings(session, {"rates": "Сайт-визитка — от 40 000 ₽, 10 дней"})
        await session.commit()
        return {k: r.id for k, r in rows.items()}


async def test_list_groups_filters_and_actions(api) -> None:
    client, runtime = api
    ids = await _tenders(runtime)
    data = (await client.get("/api/tenders")).json()
    assert [t["title"] for t in data["items"]] == ["Сборка MVP", "Бот"]
    assert data["counts"] == {"relevant": 2, "open": 3, "drafted": 0, "responded": 0, "closed": 0, "all": 4}
    by_deadline = (await client.get("/api/tenders", params={"group": "open", "sort": "deadline"})).json()
    assert [t["title"] for t in by_deadline["items"]] == ["Бот", "Сборка MVP", "Логотип"]
    crm = (await client.get("/api/tenders", params={"group": "all", "section": "crm"})).json()
    assert {t["title"] for t in crm["items"]} == {"Бот", "Старый"} and crm["items"][0][
        "section"
    ] == "CRM, 1С, ПО, чат-боты"
    assert (await client.get("/api/tenders", params={"section": "mobile"})).status_code == 422

    r = await client.post(f"/api/tenders/{ids['bot']}/interesting")
    assert r.json()["status"] == "interesting"
    assert (await client.post(f"/api/tenders/{ids['logo']}/skip")).json()["status"] == "skipped"
    assert (await client.post(f"/api/tenders/{ids['logo']}/restore")).json()["status"] == "interesting"
    r = await client.post(f"/api/tenders/{ids['mvp']}/responded")
    assert r.json()["status"] == "responded" and r.json()["responded_at"]
    # the organizer's answer is recorded by hand, once the user has responded
    r = await client.post(f"/api/tenders/{ids['bot']}/replied")
    assert r.status_code == 409 and "уже откликнулись" in r.json()["detail"]
    r = await client.post(f"/api/tenders/{ids['mvp']}/replied")
    assert r.json()["status"] == "replied" and r.json()["status_label"] == "ответили" and r.json()["answered_at"]
    answered_at = r.json()["answered_at"]
    assert (await client.post(f"/api/tenders/{ids['mvp']}/responded")).json()["status"] == "replied"  # stays
    r = await client.post(f"/api/tenders/{ids['mvp']}/declined")
    assert (r.json()["status"], r.json()["answered_at"]) == ("declined", answered_at)
    assert (await client.get("/api/tenders", params={"group": "responded"})).json()["counts"]["responded"] == 1
    # hidden by mistake and brought back: «Откликнулся», not posting the answer again; the answer is marked anew there
    assert (await client.post(f"/api/tenders/{ids['mvp']}/hide")).json()["status"] == "hidden"
    r = await client.post(f"/api/tenders/{ids['mvp']}/restore")
    assert (r.json()["status"], r.json()["answered_at"]) == ("responded", answered_at)
    r = await client.post(f"/api/tenders/{ids['mvp']}/replied")
    assert (r.json()["status"], r.json()["answered_at"]) == ("replied", answered_at)
    r = await client.put(f"/api/tenders/{ids['mvp']}", json={"notes": " позвонить в пятницу "})
    assert r.json()["notes"] == "позвонить в пятницу"
    assert (await client.post("/api/tenders/999/skip")).status_code == 404

    settings = (await client.get("/api/tenders/settings")).json()
    assert (
        settings["sections"] == ["web-development", "crm", "apps-development"]
        and len(settings["available_sections"]) == 10
    )
    r = await client.put("/api/tenders/settings", json={"sections": ["seo"], "min_score": 70})
    assert r.status_code == 200 and r.json()["sections"] == ["seo"]
    detail = (await client.get(f"/api/tenders/{ids['bot']}")).json()
    assert detail["relevant"] is False  # rescored: crm is not watched any more and 30 < 70
    assert (await client.put("/api/tenders/settings", json={"sections": ["mobile"]})).status_code == 422


async def test_answer_through_claude(api) -> None:
    client, runtime = api
    ids = await _tenders(runtime)
    r = await client.post(
        "/api/tenders/replies", json={"ids": [ids["mvp"], ids["old"], 999], "notes": "упомянуть Docker"}
    )
    data = r.json()
    assert data["queued"] == 1 and data["batch_id"]
    assert data["skipped"] == ["«Старый»: приём заявок закрыт", "#999: не найден"]
    packet = json.loads(
        (runtime.settings.exchange_dir / "pending" / f"batch_{data['batch_id']}.json").read_text("utf-8")
    )
    task = packet["tasks"][0]
    assert task["type"] == "tender_reply" and task["vacancy"]["id"].startswith("tender-")
    assert (
        task["tender"]["rates"] == "Сайт-визитка — от 40 000 ₽, 10 дней"
        and task["tender"]["budget"] == "до 150 000 рублей"
    )
    assert task["user_notes"] == "упомянуть Docker"
    assert (await client.get(f"/api/tenders/{ids['mvp']}")).json()["status"] == "drafting"

    text = "Здравствуйте! Соберу лендинг и анкету за пять дней. " * 6 + "Примеры: https://example.com"
    outcome = await _import(runtime, data["batch_id"], [{"vacancy_id": task["vacancy"]["id"], "bid": {"text": text}}])
    assert outcome.status == "imported" and outcome.pitches == 1
    detail = (await client.get(f"/api/tenders/{ids['mvp']}")).json()
    assert detail["status"] == "drafted" and detail["reply"]["origin"] == "claude"
    assert any("ссылка" in w for w in detail["reply"]["warnings"])
    r = await client.put(f"/api/tenders/{ids['mvp']}/reply", json={"text": "Здравствуйте! Новый текст отклика."})
    assert [x["origin"] for x in r.json()["replies"]] == ["edited", "claude"] and r.json()["reply"]["version"] == 2

    # no result: back to the queue; an error: closed with the reason, the tender keeps its answer
    batch_id = (await client.post("/api/tenders/replies", json={"ids": [ids["mvp"]]})).json()["batch_id"]
    outcome = await _import(runtime, batch_id, [])
    async with runtime.session_factory() as session:
        req = (
            await session.execute(select(WsTenderReplyRequest).where(WsTenderReplyRequest.status == "queued"))
        ).scalar_one()
    assert outcome.missing and req.batch_id is None
    batch_id = (await client.post("/api/tenders/replies", json={"ids": [ids["bot"]]})).json()["batch_id"]
    packet = json.loads((runtime.settings.exchange_dir / "pending" / f"batch_{batch_id}.json").read_text("utf-8"))
    keys = {t["tender"]["title"]: t["vacancy"]["id"] for t in packet["tasks"]}
    await _import(runtime, batch_id, [{"vacancy_id": keys["Бот"], "error": "тендер не про разработку"}])
    detail = (await client.get(f"/api/tenders/{ids['bot']}")).json()
    assert detail["status"] == "interesting" and detail["request"]["error"] == "тендер не про разработку"


async def _import(runtime: Runtime, batch_id: str, items: list[dict[str, Any]]) -> Any:
    folder = done_dir(runtime.settings)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"batch_{batch_id}.json"
    result = {"protocol_version": 1, "batch_id": batch_id, "profile_version": 1, "results": items}
    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    async with runtime.session_factory() as session:
        outcome = await import_result_file(session, runtime.settings, path)
        await session.commit()
    return outcome


def test_tender_notification_reads_well() -> None:
    one = render_event(
        "ws_tender",
        [{"title": "Сайт для отеля", "url": f"{WS}/tenders/sayt-1/", "budget": "до 200 000", "deadline": "2026-09-30",
          "city": "Ульяновска", "score": 55}],
    )  # fmt: skip
    assert "Тендер на workspace.ru" in one and "заявки до 30.09.2026" in one and "заказчик из Ульяновска" in one
    many = render_event("ws_tender", [{"title": f"Тендер {n}", "url": f"{WS}/tenders/t-{n}/"} for n in range(3)])
    assert "Новые тендеры на workspace.ru: 3" in many
