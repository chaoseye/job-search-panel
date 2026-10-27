"""Studios: the four sources (hh.ru search, employers of the panel's vacancies, Telegram
channel posts, the user's list), contacts from the studio's site, the hh rule (an initiative application only), the
letter through the Claude packet, «sent», replies found in the mailbox, the API."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from alembic import command
from httpx import ASGITransport
from sqlalchemy import select

from app.config import Settings
from app.db.base import utcnow
from app.db.models import (
    CandidateProfile,
    ChannelPost,
    ClaudeBatch,
    Notification,
    Studio,
    StudioLetter,
    StudioLetterRequest,
    TelegramChannel,
    Vacancy,
)
from app.db.session import alembic_config
from app.exchange.importer import import_result_file
from app.exchange.packets import cancel_batch, done_dir
from app.exchange.schemas import ResultItem
from app.main import app
from app.runtime import Runtime
from app.services import humanizer, mail_sync, studio_contacts, studios
from app.services.notify import render_event
from app.services.studio_sources import find_studios, is_studio_card

BASE = "https://api.hh.ru"
SITE = "https://pixel-studio.example"


@pytest.fixture
async def api(settings: Settings):
    runtime = await Runtime.create(settings.model_copy(update={"batch_auto_create": False}))
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


def _card(employer_id: str, name: str, industries: list[str], description: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": employer_id,
        "name": name,
        "type": "company",
        "site_url": f"https://{employer_id}.studio-card.example",
        "area": {"id": "1", "name": "Москва"},
        "industries": [{"id": i, "name": i} for i in industries],
        "description": description,
        "open_vacancies": 3,
        "relations": [],
        **extra,
    }


def _mock_hh(m: respx.MockRouter, cards: list[dict[str, Any]]) -> dict[str, Any]:
    m.post(f"{BASE}/token").mock(return_value=httpx.Response(200, json={"access_token": "app", "expires_in": 100000}))
    routes = {
        c["id"]: m.get(f"{BASE}/employers/{c['id']}").mock(return_value=httpx.Response(200, json=c)) for c in cards
    }
    search = m.get(f"{BASE}/employers").mock(
        return_value=httpx.Response(
            200,
            json={
                "found": len(cards),
                "pages": 1,
                "page": 0,
                "per_page": 100,
                "items": [{"id": c["id"], "name": c["name"], "open_vacancies": 3} for c in cards],
            },
        )
    )
    return {"search": search, **routes}


# ---- sources ---------------------------------------------------------------------------------------------------------


def test_what_counts_as_a_studio() -> None:
    web = _card("1", "Пиксель", ["7.540"], "<p>Веб-студия: сайты и приложения под ключ для клиентов</p>")
    assert is_studio_card(web, strict=True)
    product = _card("2", "Продуктовая компания", ["7.540"], "<p>Развиваем собственный SaaS</p>")
    assert is_studio_card(product, strict=False) and not is_studio_card(product, strict=True)
    assert not is_studio_card(_card("3", "Кадры Плюс", ["44.391"], "подбор персонала"), strict=False)
    assert not is_studio_card(_card("4", "Магазин", ["41.525"], "студия продаж"), strict=False)
    fitness = _card("5", "Фитнес-студия", [], "занятия йогой")
    assert not is_studio_card(fitness, strict=False)
    assert studios.studio_kind("Дизайн-студия: брендинг и UX") == "design"
    assert studios.studio_kind("разработка мобильных приложений и дизайн интерфейсов") == "mixed"
    assert studios.site_parts("WWW.Pixel-Studio.example/about") == (
        "https://www.pixel-studio.example",
        "pixel-studio.example",
    )
    assert studios.site_parts("mailto:x@y.example") == (None, None)


def test_only_studios_that_do_digital_work() -> None:
    """«Студия дизайна» on hh.ru is mostly an interior studio: only digital work makes a studio."""
    interior = _card(
        "6",
        "Студия дизайна интерьера Альков",
        ["11.459"],
        "<p>Разработка дизайн-проекта по вашим пожеланиям, 3D-визуализация, авторский надзор</p>",
    )
    assert not is_studio_card(interior, strict=False)
    curtains = _card("7", "Дизайн студия Шторы и Ко", [], "<p>Текстильное оформление интерьера, автоматизация штор</p>")
    assert not is_studio_card(curtains, strict=False)
    hoodies = _card(
        "8", "Дизайн-студия ПринтМаг", ["11.459"], "<p>Печать любого изображения на: толстовках, кружках</p>"
    )
    assert not is_studio_card(hoodies, strict=False)
    # a design studio that says nothing about its work is an interior studio more often than not
    assert not is_studio_card(_card("9", "Студия дизайна Весна", ["11.459"], ""), strict=False)
    assert not is_studio_card(_card("10", "Студия архитектуры и дизайна Контур", [], ""), strict=False)
    # digital work: web, branding, a creative agency, a development industry; an interior studio also making sites
    for card in (
        _card("11", "Веб-студия Сайтоград", ["11.459"], ""),
        _card("12", "Дизайн-студия Пиксель", ["11.459"], "<p>Брендинг, логотипы, фирменный стиль</p>"),
        _card("13", "Студия дизайна Ромашка", ["11.459"], "<p>Креативное агентство: рекламные креативы</p>"),
        _card("14", "Студия Код", ["7.540"], ""),
        _card("15", "Студия Уют", ["11.459"], "<p>Дизайн интерьеров, а ещё разработка сайтов под ключ</p>"),
    ):
        assert is_studio_card(card, strict=False), card["name"]
    assert studios.digital_profile("Мы — студия разработки игр")
    assert not studios.digital_profile("Вебинары по дизайну интерьера")


def test_a_studio_site_is_its_own_site() -> None:
    assert studios.studio_site("https://vk.com/example_print") == (None, None)
    assert studios.studio_site("instagram.com/example_vesna") == (None, None)
    assert studios.studio_site("gmail.com") == (None, None)
    assert studios.studio_site("https://careers.bitrix24.ru") == ("https://bitrix24.ru", "bitrix24.ru")
    assert studios.studio_site("jobs.pixel-studio.example/python") == (
        "https://pixel-studio.example",
        "pixel-studio.example",
    )
    # the company's own domain
    assert studios.studio_site("careers.example") == ("https://careers.example", "careers.example")
    assert studios.social_page("vk.com/example_print") == "https://vk.com/example_print"
    assert studios.social_page("https://hh.ru/employer/1") is None and studios.social_page(SITE) is None


@respx.mock(assert_all_called=False)
async def test_a_social_network_in_the_hh_card_is_no_site(api, respx_mock: respx.MockRouter) -> None:
    """Two employers whose «site» is vk.com / instagram.com stay two studios (the second one used to merge into the
    first by the domain)."""
    client, runtime = api
    cards = [
        _card("41", "Веб-студия Раз", ["7.540"], "<p>сайты</p>", site_url="https://vk.com/studio_one"),
        _card("42", "Веб-студия Два", ["7.540"], "<p>сайты</p>", site_url="https://vk.com/studio_two"),
        _card("43", "Веб-студия Три", ["7.540"], "<p>сайты</p>", site_url="instagram.com"),
        _card(
            "44", "Веб-студия Четыре", ["7.540"], "<p>сайты</p>", site_url="https://careers.four-studio.example/jobs"
        ),
    ]
    _mock_hh(respx_mock, cards)
    result = await find_studios(runtime, ("hh_search",))
    assert (result["hh_search"]["added"], result["hh_search"]["updated"]) == (4, 0)
    async with runtime.session_factory() as session:
        rows = {r.hh_employer_id: r for r in (await session.execute(select(Studio))).scalars()}
    assert [(rows[i].domain, rows[i].site_url) for i in ("41", "42", "43")] == [(None, None)] * 3
    assert (rows["41"].contact_url, rows["42"].contact_url) == (
        "https://vk.com/studio_one",
        "https://vk.com/studio_two",
    )
    assert rows["43"].contact_url == "https://instagram.com"
    assert (rows["44"].domain, rows["44"].site_url) == ("four-studio.example", "https://four-studio.example")
    # a site typed in by hand must be the studio's own too
    r = await client.put(f"/api/studios/{rows['41'].id}", json={"site_url": "https://ok.ru/studio"})
    assert r.status_code == 422 and "соцсеть" in r.json()["detail"]
    r = await client.put(f"/api/studios/{rows['41'].id}", json={"site_url": "careers.studio-one.example"})
    assert r.status_code == 200 and r.json()["domain"] == "studio-one.example"


@respx.mock(assert_all_called=False)
async def test_hh_search_keeps_studios_and_remembers_the_rest(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    cards = [
        _card("11", "Пиксель", ["7.540"], "<p>Веб-студия: сайты и приложения, AI-чат-боты, Python</p>"),
        _card("12", "Кадры Плюс", ["44.391"], "<p>кадровое агентство</p>"),
        _card("13", "Магазин", ["41.525"], "<p>интернет-магазин</p>"),
    ]
    routes = _mock_hh(respx_mock, cards)
    async with runtime.session_factory() as session:
        await studios.save_studio_settings(session, {"keywords": ["веб-студия"]})
        await session.commit()

    result = await find_studios(runtime, ("hh_search",))
    assert result["hh_search"]["added"] == 1 and result["hh_search"]["rejected"] == 2
    params = routes["search"].calls[0].request.url.params
    assert (params["text"], params["area"], params["type"], params["only_with_vacancies"]) == (
        "веб-студия",
        "113",
        "company",
        "true",
    )
    async with runtime.session_factory() as session:
        row = (await session.execute(select(Studio))).scalar_one()
    assert (row.name, row.hh_employer_id, row.domain, row.source, row.letter_kind) == (
        "Пиксель",
        "11",
        "11.studio-card.example",
        "hh_search",
        "application",
    )
    assert row.area == "Москва" and row.open_vacancies == 3 and row.status == "new"
    assert "AI и автоматизация" in json.loads(row.services_json or "[]")

    # the next run asks hh.ru for no card again: the studio is fresh, the others are remembered as not studios
    result = await find_studios(runtime, ("hh_search",))
    assert result["hh_search"]["added"] == 0 and all(routes[c["id"]].call_count == 1 for c in cards)


@respx.mock(assert_all_called=False)
async def test_employers_of_the_panels_vacancies(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    cards = [
        _card("21", "Студия Код", ["7.540"], "<p>Аутсорс разработки: делаем проекты для клиентов</p>"),
        _card("22", "Банк", ["7.540"], "<p>Разрабатываем свой мобильный банк</p>"),
        _card("23", "Отфильтрованная", ["7.540"], "<p>студия</p>"),
    ]
    routes = _mock_hh(respx_mock, cards)
    async with runtime.session_factory() as session:
        for hh_id, employer, status in (
            ("901", "21", "suitable"),
            ("902", "22", "maybe"),
            ("903", "23", "filtered_out"),
        ):
            session.add(Vacancy(hh_id=hh_id, name="Python-разработчик", employer_id=employer, pipeline_status=status))
        await session.commit()

    result = await find_studios(runtime, ("hh_vacancies",))
    assert result["hh_vacancies"] == {**result["hh_vacancies"], "added": 1, "rejected": 1}
    assert routes["23"].call_count == 0  # its vacancy did not pass the rules
    async with runtime.session_factory() as session:
        row = (await session.execute(select(Studio))).scalar_one()
        detail = await studios.studio_detail(session, row)
    assert row.hh_employer_id == "21" and row.source == "hh_vacancies"
    assert [v["name"] for v in detail["vacancies"]] == ["Python-разработчик"]
    assert any("подходящих вакансий в панели" in n for n in detail["fit_notes"])


async def test_channel_posts_of_studios_with_a_site(api) -> None:
    client, runtime = api
    async with runtime.session_factory() as session:
        channel = TelegramChannel(username="job_python")
        session.add(channel)
        await session.flush()
        posts = [
            (
                "Пиксель — Python-разработчик",
                "Мы веб-студия, делаем проекты под ключ",
                [SITE + "/jobs", "https://t.me/job_python"],
            ),
            (
                "HireAgency — Python Developer",
                "Рекрутинговое агентство, студия подбора",
                ["https://hire-agency.example"],
            ),
            ("Без сайта — дизайнер", "Дизайн-студия ищет дизайнера", ["https://t.me/job_python", "https://vk.com/x"]),
            ("Банк — аналитик", "Крупный банк", ["https://bank.example"]),
            # the role first, «creative studio» in the tags, a job board link — not a studio
            (
                "Workflow & Design Systems Specialist (Contract) — Fintechco",
                "tags: design systems, fintech, creative studio",
                ["https://www.cryptojobs-board.example/jobs/fintechco"],
            ),
            ("Holdco — Engineer", "a digital assets holding company", ["https://holdco.example"]),
            (
                "Совиные игры — Lead UI/UX Designer",
                "Мы — студия разработки игр",
                ["https://careers.owl-games.example/ux"],
            ),
            ("Уют — дизайнер интерьера", "Студия дизайна интерьера ищет дизайнера", ["https://uyut-design.example"]),
        ]
        for n, (title, text, links) in enumerate(posts, start=1):
            session.add(
                ChannelPost(channel_id=channel.id, message_id=n, title=title, text=text, links_json=json.dumps(links))
            )
        await session.commit()

    result = await find_studios(runtime, ("channels",))
    assert result["channels"]["added"] == 2
    async with runtime.session_factory() as session:
        rows = {r.name: r for r in (await session.execute(select(Studio))).scalars()}
    assert set(rows) == {"Пиксель", "Совиные игры"}
    row = rows["Пиксель"]
    assert (row.domain, row.source, row.hh_employer_id) == ("pixel-studio.example", "channel", None)
    assert rows["Совиные игры"].domain == "owl-games.example"  # the careers subdomain belongs to the studio


async def test_the_users_list(api) -> None:
    client, runtime = api
    text = "\n".join(
        [
            "# мои студии",
            "pixel-studio.example",
            "Студия Код; https://www.kod-studio.example/; Hello@Kod-Studio.example",
            "hr@design-lab.example",
            "просто название",
            "Пиксель повтор, PIXEL-STUDIO.EXAMPLE",
        ]
    )
    r = await client.post("/api/studios/import", json={"text": text, "letter_kind": "offer"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert (data["added"], data["updated"]) == (3, 1) and data["errors"] == ["строка 5: нет адреса сайта"]
    listing = (await client.get("/api/studios", params={"group": "all", "sort": "name"})).json()
    by_domain = {s["domain"]: s for s in listing["items"]}
    assert set(by_domain) == {"pixel-studio.example", "kod-studio.example", "design-lab.example"}
    assert (
        by_domain["kod-studio.example"]["emails"] == ["hello@kod-studio.example"]
        and by_domain["kod-studio.example"]["status"] == "ready"
    )
    assert by_domain["design-lab.example"]["emails"] == ["hr@design-lab.example"]
    assert all(s["letter_kind"] == "offer" and s["offer_allowed"] for s in listing["items"])


async def test_the_users_list_without_a_site_of_the_studio(api) -> None:
    """A social network page or a public mailbox is no studio site: otherwise every gmail.com studio of the list would
    become one studio «gmail.com»."""
    client, runtime = api
    text = "\n".join(
        [
            "Студия А; vk.com/studio_a",
            "Студия Б; https://vk.com/studio_b; Hello.B@gmail.com",
            "Студия В; hello.v@gmail.com",
            "Студия Г; careers.studio-g.example",
        ]
    )
    data = (await client.post("/api/studios/import", json={"text": text})).json()
    assert (data["added"], data["updated"]) == (3, 0)
    assert data["errors"] == [
        "строка 1: vk.com/studio_a — не сайт студии (соцсеть или доска вакансий), добавьте сайт или почту"
    ]
    # the same public address again is the same studio
    data = (await client.post("/api/studios/import", json={"text": "Студия Б повтор; hello.b@gmail.com"})).json()
    assert (data["added"], data["updated"]) == (0, 1)
    items = (await client.get("/api/studios", params={"group": "all", "sort": "name"})).json()["items"]
    by_name = {s["name"]: s for s in items}
    assert set(by_name) == {"Студия Б", "Студия В", "Студия Г"}
    b = by_name["Студия Б"]
    assert (b["domain"], b["emails"], b["contact_url"], b["status"]) == (
        None,
        ["hello.b@gmail.com"],
        "https://vk.com/studio_b",
        "ready",
    )
    assert by_name["Студия В"]["domain"] is None and by_name["Студия В"]["emails"] == ["hello.v@gmail.com"]
    assert by_name["Студия Г"]["domain"] == "studio-g.example"


@respx.mock(assert_all_called=False)
async def test_a_studio_from_hh_gets_only_an_application(api, respx_mock: respx.MockRouter) -> None:
    """hh developer agreement §3.3/§4.5/§4.7: hh data serves job search only — no sales offer, even if the same
    studio also comes from the user's list."""
    client, runtime = api
    _mock_hh(respx_mock, [_card("31", "Пиксель", ["7.540"], "<p>веб-студия</p>", site_url=SITE)])
    await find_studios(runtime, ("hh_search",))
    r = await client.post("/api/studios/import", json={"text": "pixel-studio.example", "letter_kind": "offer"})
    assert r.json()["updated"] == 1
    listing = (await client.get("/api/studios", params={"group": "all"})).json()
    studio = listing["items"][0]
    assert (studio["letter_kind"], studio["offer_allowed"]) == ("application", False)
    assert studio["sources"] == ["hh_search", "list"]
    r = await client.put(f"/api/studios/{studio['id']}", json={"letter_kind": "offer"})
    assert r.status_code == 409 and "поиска работы" in r.json()["detail"]


# ---- contacts from the studio's site ---------------------------------------------------------------------------------

HOME = f"""<html><head><title>Пиксель — веб-студия</title>
<meta name="description" content="Разрабатываем сайты и мобильные приложения, внедряем чат-ботов на LLM"></head>
<body><h1>Сайты и приложения под ключ</h1>
<a href="/contacts">Контакты</a> <a href="{SITE}/about">О студии</a> <a href="https://other.example/contacts">чужие</a>
<a href="https://t.me/example_pixel_studio">Telegram</a> <img src="logo@2x.png">
</body></html>"""
CONTACTS = """<html><body>
<a href="mailto:Hello@Pixel-Studio.example?subject=Проект">hello@pixel-studio.example</a>
<a href="/cdn-cgi/l/email-protection#{cf}">[email&#160;protected]</a>
<p>Вакансии: hr [at] pixel-studio (dot) example, директор ivan.petrov@pixel-studio.example</p>
<p>errors@sentry.io support@gmail.com</p>
</body></html>"""


def _cf(email: str, key: int = 0x2A) -> str:
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in email)


@respx.mock(assert_all_called=False)
async def test_contacts_are_read_from_the_site(
    api, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, runtime = api
    monkeypatch.setattr(studio_contacts, "PAUSE_SEC", 0)
    respx_mock.get(f"{SITE}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /admin\n")
    )
    respx_mock.get(f"{SITE}/").mock(return_value=httpx.Response(200, html=HOME))
    respx_mock.get(f"{SITE}/contacts").mock(
        return_value=httpx.Response(200, html=CONTACTS.replace("{cf}", _cf("team@pixel-studio.example")))
    )
    respx_mock.get(f"{SITE}/about").mock(return_value=httpx.Response(200, html="<p>О нас</p>"))
    async with runtime.session_factory() as session:
        row, _ = await studios.upsert_studio(session, name="Пиксель", source="list", site_url=SITE)
        await session.commit()

    r = await client.post("/api/studios/contacts", json={"ids": [row.id]})
    assert r.status_code == 200
    await runtime.jobs.wait("studio_contacts")
    detail = (await client.get(f"/api/studios/{row.id}")).json()
    # the studio's own domain and role mailboxes first; vendors (sentry) dropped; at most five
    assert detail["emails"] == [
        "hello@pixel-studio.example",
        "team@pixel-studio.example",
        "hr@pixel-studio.example",
        "ivan.petrov@pixel-studio.example",
        "support@gmail.com",
    ]
    assert detail["telegram"] == "https://t.me/example_pixel_studio" and detail["contact_url"] == f"{SITE}/contacts"
    assert detail["status"] == "ready" and detail["contacts_error"] is None
    assert "веб-студия" in detail["site_summary"] and "Сайты и приложения под ключ" in detail["site_summary"]
    assert "AI и автоматизация" in detail["services"] and "мобильные приложения" in detail["services"]


@pytest.mark.parametrize(
    ("robots", "home", "error"),
    [
        (httpx.Response(200, text="User-agent: *\nDisallow: /\n"), None, "robots.txt"),
        (httpx.Response(404), httpx.Response(403, text="Forbidden"), "HTTP 403"),
        (httpx.Response(404), httpx.Response(503, text="<script>challenge</script>"), "защитой от роботов"),
        (
            httpx.Response(404),
            httpx.Response(200, html="<div id='root'></div><script src='/app.js'></script>"),
            "не нашлось",
        ),
    ],
)
@respx.mock(assert_all_called=False)
async def test_sites_without_readable_contacts(
    api, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, robots: httpx.Response, home: Any, error: str
) -> None:
    client, runtime = api
    monkeypatch.setattr(studio_contacts, "PAUSE_SEC", 0)
    respx_mock.get(f"{SITE}/robots.txt").mock(return_value=robots)
    if home is not None:
        respx_mock.get(f"{SITE}/").mock(return_value=home)
    respx_mock.route().mock(return_value=httpx.Response(404))
    async with runtime.session_factory() as session:
        row, _ = await studios.upsert_studio(session, name="Пиксель", source="list", site_url=SITE)
        await session.commit()
    await studio_contacts.check_contacts(runtime, [row.id])
    async with runtime.session_factory() as session:
        fresh = await session.get(Studio, row.id)
    assert fresh is not None and fresh.status == "no_contacts" and error in (fresh.contacts_error or "")
    # an address typed in by hand makes the studio ready anyway
    r = await client.put(f"/api/studios/{row.id}", json={"emails": ["Hello@pixel-studio.example", "не почта"]})
    assert r.json()["emails"] == ["hello@pixel-studio.example"] and r.json()["status"] == "ready"


@respx.mock(assert_all_called=False)
async def test_a_site_that_does_not_answer_is_not_waited_for_twice(api, respx_mock: respx.MockRouter) -> None:
    """No connection already for robots.txt: the home page is not asked (a dead site used to cost two timeouts)."""
    client, runtime = api
    respx_mock.get(f"{SITE}/robots.txt").mock(side_effect=httpx.ConnectTimeout("timed out"))
    home = respx_mock.get(f"{SITE}/").mock(return_value=httpx.Response(200, html=HOME))
    found = await studio_contacts.fetch_contacts(runtime.http, runtime.settings, SITE, pause=0)
    assert found.error == "сайт не открылся: ConnectTimeout" and home.call_count == 0


@respx.mock(assert_all_called=False)
async def test_sites_that_did_not_open_are_tried_again(
    api, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The button without ids checks the new studios and, RETRY_AFTER later, the ones whose site did not open — not
    the ones that opened without contacts or refused robots."""
    client, runtime = api
    monkeypatch.setattr(studio_contacts, "PAUSE_SEC", 0)
    respx_mock.route(path="/robots.txt").mock(return_value=httpx.Response(404))
    respx_mock.route(path="/").mock(return_value=httpx.Response(200, html='<a href="mailto:hi@x.example">hi</a>'))
    respx_mock.route().mock(return_value=httpx.Response(404))
    now = utcnow()
    long_ago, recently = now - timedelta(hours=4), now - timedelta(hours=1)
    async with runtime.session_factory() as session:
        ids = {}
        for key, error, checked in (
            ("fresh", None, None),
            ("timeout", "сайт не открылся: ConnectTimeout", long_ago),
            ("server", "сайт ответил HTTP 503", long_ago),
            ("recent", "сайт не открылся: ReadTimeout", recently),
            ("empty", "на сайте не нашлось почты и Telegram", long_ago),
            ("forbidden", "сайт ответил HTTP 403", long_ago),
        ):
            row, _ = await studios.upsert_studio(session, name=key, source="list", site_url=f"https://{key}.ru")
            if checked is not None:
                row.status, row.contacts_error, row.contacts_checked_at = "no_contacts", error, checked
            ids[key] = row.id
        await session.commit()

    summary = await studio_contacts.check_contacts(runtime)
    assert (summary["checked"], summary["retried"], summary["with_contacts"]) == (3, 2, 3)
    async with runtime.session_factory() as session:
        rows = {r.name: r for r in (await session.execute(select(Studio))).scalars()}
    assert {n for n, r in rows.items() if r.status == "ready"} == {"fresh", "timeout", "server"}
    assert rows["recent"].contacts_error == "сайт не открылся: ReadTimeout"
    # the per-run limit takes the new studios first, the retries fill the rest
    async with runtime.session_factory() as session:
        row, _ = await studios.upsert_studio(session, name="newer", source="list", site_url="https://newer.example")
        await session.commit()
        later = now + timedelta(hours=4)
        assert await studio_contacts._due(session, 1, later) == ([row.id], 0)
        assert await studio_contacts._due(session, 5, later) == ([row.id, ids["recent"]], 1)


# ---- the letter: Claude packet → import → sent → reply -------------------------------------------------------------


async def _ready_studio(runtime: Runtime, **extra: Any) -> int:
    async with runtime.session_factory() as session:
        session.add(
            CandidateProfile(version=1, is_active=True, preferences_json="{}", profile_md="# профиль", hash="h")
        )
        row, _ = await studios.upsert_studio(session, name="Пиксель", source="list", site_url=SITE, **extra)
        row.emails_json, row.status, row.contacts_checked_at = '["hello@pixel-studio.example"]', "ready", utcnow()
        await studios.save_studio_settings(session, {"signature": "Иван Петров\n+7 900 000-00-00"})
        await session.commit()
        return row.id


def _result(batch_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"protocol_version": 1, "batch_id": batch_id, "profile_version": 1, "results": items}


def _pitch(
    text: str = "Здравствуйте! " + "Помогу вашей студии с backend-разработкой в проектах клиентов. " * 12,
    style: str | None = None,
) -> dict[str, Any]:
    pitch = {
        "subject": "Python-разработчик для проектов Пикселя",
        "text": text,
        "language": "ru",
        "needs_clarification": [],
    }
    return pitch if style is None else {"style": style, **pitch}


SIMPLE_TEXT = "Здравствуйте!\n\nПишу backend на Python и тесты к нему. Если нужен такой человек, давайте созвонимся?"


async def _import(runtime: Runtime, batch_id: str, items: list[dict[str, Any]]) -> Any:
    folder = done_dir(runtime.settings)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"batch_{batch_id}.json"
    path.write_text(json.dumps(_result(batch_id, items), ensure_ascii=False), encoding="utf-8")
    async with runtime.session_factory() as session:
        outcome = await import_result_file(session, runtime.settings, path)
        await session.commit()
    return outcome


async def test_letter_through_claude_sent_and_answered(api, monkeypatch: pytest.MonkeyPatch) -> None:
    client, runtime = api
    studio_id = await _ready_studio(runtime)
    r = await client.post("/api/studios/letters", json={"ids": [studio_id, 999], "notes": "упомянуть Docker"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["queued"] == 1 and data["skipped"] == ["#999: не найдена"] and data["batch_id"]
    packet = json.loads(
        Path(runtime.settings.exchange_dir / "pending" / f"batch_{data['batch_id']}.json").read_text("utf-8")
    )
    task = packet["tasks"][0]
    assert (
        task["type"] == "studio_letter"
        and task["letter_kind"] == "application"
        and task["user_notes"] == "упомянуть Docker"
    )
    assert task["studio"]["site"] == SITE and task["vacancy"]["id"].startswith("studio-")
    assert task["styles"] == ["business", "simple"]
    assert (await client.get(f"/api/studios/{studio_id}")).json()["status"] == "drafting"

    # a draft per style: the business one is shown first, the user picks
    pitches = [_pitch(style="простой", text=SIMPLE_TEXT), _pitch(style="business")]
    outcome = await _import(runtime, data["batch_id"], [{"vacancy_id": task["vacancy"]["id"], "pitches": pitches}])
    assert outcome.status == "imported" and outcome.pitches == 1
    detail = (await client.get(f"/api/studios/{studio_id}")).json()
    assert detail["status"] == "drafted" and detail["letter"]["origin"] == "claude"
    assert (detail["letter"]["style"], detail["letter"]["style_label"]) == ("business", "деловой")
    assert [(v["style"], v["is_current"]) for v in detail["variants"]] == [("business", True), ("simple", False)]
    assert detail["variants"][1]["text"] == SIMPLE_TEXT
    assert all(v["full_text"].endswith("Иван Петров\n+7 900 000-00-00") for v in detail["variants"])

    # the user's edit of the simple draft is a new version of it, and the one picked
    r = await client.put(
        f"/api/studios/{studio_id}/letter", json={"subject": "Тема", "text": "Здравствуйте! " * 5, "style": "simple"}
    )
    data = r.json()
    assert (data["letter"]["style"], data["letter"]["origin"], data["letter"]["version"]) == ("simple", "edited", 3)
    assert [(v["style"], v["origin"], v["is_current"]) for v in data["variants"]] == [
        ("business", "claude", False),
        ("simple", "edited", True),
    ]
    business_id = data["variants"][0]["id"]
    async with runtime.session_factory() as session:
        other, _ = await studios.upsert_studio(session, name="Другая", source="list", site_url="https://other.example")
        await session.commit()
    r = await client.post(f"/api/studios/{studio_id}/sent", json={"to": "x@y.example", "letter_id": business_id + 100})
    assert r.status_code == 404
    r = await client.post(f"/api/studios/{other.id}/sent", json={"to": "x@y.example", "letter_id": business_id})
    assert r.status_code == 404  # a letter of another studio

    # sent from the user's mailbox — the business draft after all; the soft daily limit is reported
    runtime.settings = runtime.settings.model_copy(update={"studios_daily_limit": 0})
    r = await client.post(
        f"/api/studios/{studio_id}/sent",
        json={"to": "hello@pixel-studio.example", "via": "email", "letter_id": business_id},
    )
    assert r.status_code == 200 and (r.json()["status"], r.json()["sent_today"], r.json()["over_limit"]) == (
        "sent",
        1,
        True,
    )
    assert (r.json()["letter"]["id"], r.json()["letter"]["style"]) == (business_id, "business")
    assert (await client.post(f"/api/studios/{studio_id}/sent", json={"to": "x@y.example"})).status_code == 409

    # the mail sync finds the answer from the studio's domain
    async with runtime.session_factory() as session:
        targets = await studios.reply_targets(session)
    assert [(t[0], t[1]) for t in targets] == [(studio_id, "@pixel-studio.example")]
    monkeypatch.setattr(
        mail_sync,
        "_studio_replies_sync",
        lambda settings, targets: [(targets[0][0], "Re: Python-разработчик", utcnow())],
    )
    assert await mail_sync._studio_replies(runtime) == 1
    detail = (await client.get(f"/api/studios/{studio_id}")).json()
    assert detail["status"] == "replied" and detail["reply_subject"] == "Re: Python-разработчик"
    async with runtime.session_factory() as session:
        events = [n.event_type for n in (await session.execute(select(Notification))).scalars()]
    assert "studio_reply" in events
    assert (await client.get("/api/studios/summary")).json() == {"drafted": 0, "replied": 1}


async def test_style_scans_run_before_the_import_writes(api, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each humanizer scan is a Python process (1–2 s): made inside the import's write transaction, a batch of them
    kept every other write of the panel waiting for a minute. Now every scan finds the database free — even when the
    caller (the watcher importing several files) has written before — and each text is scanned once."""
    client, runtime = api
    studio_id = await _ready_studio(runtime)
    batch_id = (await client.post("/api/studios/letters", json={"ids": [studio_id]})).json()["batch_id"]
    key = f"studio-{(await client.get(f'/api/studios/{studio_id}')).json()['request']['id']}"
    db_path = runtime.settings.db_path
    scans: list[tuple[str, bool]] = []

    async def fake_scan(text: str, *, timeout: float = 60) -> dict[str, Any]:
        con = sqlite3.connect(db_path, timeout=0.2)  # type: ignore[arg-type]
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("ROLLBACK")
            free = True
        except sqlite3.OperationalError:
            free = False
        finally:
            con.close()
        scans.append((text, free))
        return {"score": 50, "band": "подозрительно", "hard_bans": [], "marker_count": 0, "markers": []}

    monkeypatch.setattr(humanizer, "scan_text", fake_scan)
    runtime.settings = runtime.settings.model_copy(update={"humanizer_check": True})
    folder = done_dir(runtime.settings)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"batch_{batch_id}.json"
    pitches = [_pitch(style="business"), _pitch(style="simple", text=SIMPLE_TEXT)]
    path.write_text(
        json.dumps(_result(batch_id, [{"vacancy_id": key, "pitches": pitches}]), ensure_ascii=False), "utf-8"
    )
    async with runtime.session_factory() as session:
        session.add(ClaudeBatch(batch_id="20260101_0000_dead", packet_path="x.json", status="imported"))
        await session.flush()  # the caller holds the write lock, as the watcher does after its previous file
        outcome = await import_result_file(session, runtime.settings, path)
        await session.commit()
    assert outcome.status == "imported"
    assert [free for _, free in scans] == [True, True] and len({t for t, _ in scans}) == 2
    detail = (await client.get(f"/api/studios/{studio_id}")).json()
    assert all(v["warnings"] == ["humanizer: чистота 50/100 (подозрительно)"] for v in detail["variants"])


async def test_a_result_with_one_pitch_is_one_business_draft(api) -> None:
    """A result written by the rules of v9 (one `pitch`, no style) still imports: one business draft; the style
    names are read loosely, a repeated style keeps the first draft."""
    client, runtime = api
    studio_id = await _ready_studio(runtime)
    batch_id = (await client.post("/api/studios/letters", json={"ids": [studio_id]})).json()["batch_id"]
    key = f"studio-{(await client.get(f'/api/studios/{studio_id}')).json()['request']['id']}"
    outcome = await _import(runtime, batch_id, [{"vacancy_id": key, "pitch": _pitch()}])
    assert outcome.status == "imported"
    detail = (await client.get(f"/api/studios/{studio_id}")).json()
    assert [(v["style"], v["is_current"]) for v in detail["variants"]] == [("business", True)]
    # an edit without a style keeps the current one's
    r = await client.put(f"/api/studios/{studio_id}/letter", json={"subject": "Тема", "text": "Здравствуйте! " * 5})
    assert (r.json()["letter"]["style"], r.json()["letter"]["version"]) == ("business", 2)
    r = await client.put(f"/api/studios/{studio_id}/letter", json={"subject": "Т", "text": "x" * 30, "style": "fancy"})
    assert r.status_code == 422

    item = ResultItem.model_validate(
        {
            "vacancy_id": "studio-1",
            "pitches": [
                {"style": "Casual", "subject": "Простое", "text": SIMPLE_TEXT},
                {"style": "simple", "subject": "Второе простое", "text": SIMPLE_TEXT},
                {"style": "???", "subject": "Деловое", "text": "Здравствуйте! Текст."},
            ],
            "pitch": {"subject": "Старое", "text": "Текст"},
        }
    )
    assert [(p.style, p.subject) for p in item.studio_pitches()] == [("business", "Деловое"), ("simple", "Простое")]


async def test_letter_errors_missing_results_and_cancelled_packets(api) -> None:
    client, runtime = api
    studio_id = await _ready_studio(runtime)
    batch_id = (await client.post("/api/studios/letters", json={"ids": [studio_id]})).json()["batch_id"]
    key = f"studio-{(await client.get(f'/api/studios/{studio_id}')).json()['request']['id']}"

    # an error: the request is closed with the reason, the studio is back to «ready»
    outcome = await _import(runtime, batch_id, [{"vacancy_id": key, "error": "студия только печатает визитки"}])
    assert outcome.status == "partial"
    detail = (await client.get(f"/api/studios/{studio_id}")).json()
    assert detail["status"] == "ready" and detail["request"]["status"] == "cancelled"
    assert "визитки" in detail["request"]["error"]

    # no result at all: the request waits for the next packet
    batch_id = (await client.post("/api/studios/letters", json={"ids": [studio_id]})).json()["batch_id"]
    outcome = await _import(runtime, batch_id, [])
    assert outcome.missing and (await client.get(f"/api/studios/{studio_id}")).json()["request"]["status"] == "queued"

    # a cancelled packet returns its requests to the queue
    async with runtime.session_factory() as session:
        req = (
            await session.execute(select(StudioLetterRequest).where(StudioLetterRequest.status == "queued"))
        ).scalar_one()
        req.status, req.batch_id = "sent_in_batch", "20260924_1200_abcd"
        batch = ClaudeBatch(batch_id="20260924_1200_abcd", packet_path="x.json", status="pending", tasks_count=1)
        session.add(batch)
        await session.flush()
        await cancel_batch(session, runtime.settings, batch)
        await session.commit()
        assert (req.status, req.batch_id) == ("queued", None)


async def test_actions_and_statuses(api) -> None:
    client, runtime = api
    studio_id = await _ready_studio(runtime)
    assert (
        await client.post(f"/api/studios/{studio_id}/sent", json={"to": "x@y.example"})
    ).status_code == 409  # no letter
    assert (await client.post(f"/api/studios/{studio_id}/replied")).status_code == 409  # nothing was sent
    assert (await client.post(f"/api/studios/{studio_id}/skip")).json()["status"] == "skipped"
    listing = (await client.get("/api/studios")).json()
    assert listing["total"] == 0 and listing["counts"]["closed"] == 1
    assert (await client.post(f"/api/studios/{studio_id}/restore")).json()["status"] == "ready"
    r = await client.post("/api/studios/letters", json={"ids": [studio_id]})
    assert r.json()["queued"] == 1
    r = await client.post("/api/studios/letters", json={"ids": [studio_id]})
    assert r.json()["queued"] == 0 and "письмо у Claude" in r.json()["skipped"][0]
    assert (await client.post(f"/api/studios/{studio_id}/declined")).json()["status"] == "declined"
    assert (await client.get("/api/studios", params={"group": "replied"})).json()["total"] == 1


async def test_settings_and_fit(api) -> None:
    client, runtime = api
    r = await client.put(
        "/api/studios/settings",
        json={"keywords": [" веб-студия ", "", "дизайн-студия"], "only_with_vacancies": False, "offer_notes": "проект"},
    )
    assert r.json()["keywords"] == ["веб-студия", "дизайн-студия"] and r.json()["only_with_vacancies"] is False
    r = await client.put(
        "/api/profile/preferences",
        json={
            "skills_weights": [{"name": "Python", "weight": 5}, {"name": "Docker", "weight": 3}],
            "target_keywords": ["Kafka"],
        },
    )
    assert r.status_code == 200, r.text
    async with runtime.session_factory() as session:
        row, _ = await studios.upsert_studio(
            session,
            name="Пиксель",
            source="list",
            site_url=SITE,
            description="Python, Kafka-стриминг, сервисы в Docker",
        )
        await studios.refresh_fit(session, row)
        await session.commit()
    assert row.fit_score == 4 * 5 + 4 * 3 + 8
    assert json.loads(row.fit_notes_json or "[]") == ["совпадает: Python, Docker, Kafka"]


async def test_letters_sent_after_local_midnight_count_today(api) -> None:
    """The soft daily limit counts by the local day: 00:30 Moscow time is 21:30 UTC of the day before."""
    client, runtime = api
    sent = datetime(2026, 9, 23, 21, 30, tzinfo=UTC)  # 24.09 00:30 МСК
    async with runtime.session_factory() as session:
        row, _ = await studios.upsert_studio(session, name="Пиксель", source="list", site_url=SITE)
        row.status, row.sent_at = "sent", sent
        await session.commit()
        settings = runtime.settings.model_copy(update={"timezone": "Europe/Moscow"})
        assert await studios.sent_today(session, settings, now=sent + timedelta(minutes=30)) == 1
        assert await studios.sent_today(session, settings, now=sent + timedelta(days=1)) == 0


def test_migration_repairs_the_stored_sites(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, logs_dir=tmp_path / "logs")  # type: ignore[call-arg]
    cfg = alembic_config(settings)
    command.upgrade(cfg, "0023_studios")
    con = sqlite3.connect(settings.db_path)  # type: ignore[arg-type]
    for name, domain, site, status in (
        ("ПринтМаг", "vk.com", "https://vk.com", "new"),
        ("Весна", "instagram.com", "https://www.instagram.com", "new"),
        ("Почта", "gmail.com", "https://gmail.com", "ready"),
        ("Битрикс24", "careers.bitrix24.ru", "https://careers.bitrix24.ru", "no_contacts"),
        ("Дубль", "jobs.pixel-studio.example", "https://jobs.pixel-studio.example", "no_contacts"),
        ("Пиксель", "pixel-studio.example", "https://pixel-studio.example", "ready"),
    ):
        con.execute(
            "insert into studios (name, domain, site_url, source, status, contacts_checked_at, contacts_error, "
            "first_seen_at, created_at, updated_at) values (?, ?, ?, 'hh_search', ?, '2026-09-24 01:00:00', "
            "'сайт не открылся: ConnectTimeout', '2026-09-24 01:00:00', '2026-09-24 01:00:00', '2026-09-24 01:00:00')",
            (name, domain, site, status),
        )
    con.commit()
    con.close()
    command.upgrade(cfg, "head")
    con = sqlite3.connect(settings.db_path)  # type: ignore[arg-type]
    rows = {
        r[0]: r[1:]
        for r in con.execute("select name, domain, site_url, contact_url, status, contacts_checked_at from studios")
    }
    con.close()
    assert rows["ПринтМаг"][:3] == (None, None, "https://vk.com")
    assert rows["Весна"][:3] == (None, None, "https://www.instagram.com")
    assert rows["Почта"][:3] == (None, None, None)  # a mailbox is not a contact page
    assert rows["Битрикс24"] == ("bitrix24.ru", "https://bitrix24.ru", None, "new", None)
    # the company's domain is taken by another studio: left as it is
    assert rows["Дубль"][:2] == ("jobs.pixel-studio.example", "https://jobs.pixel-studio.example")
    assert rows["Пиксель"][:2] == ("pixel-studio.example", "https://pixel-studio.example")


def test_studio_reply_notification_reads_well() -> None:
    text = render_event("studio_reply", [{"name": "Пиксель", "subject": "Re: Python-разработчик"}])
    assert "Студия ответила" in text and "<b>Пиксель</b>" in text and "Re: Python-разработчик" in text


class FakeImap:
    """IMAP4_SSL stand-in: SEARCH by FROM finds the studio's message, FETCH returns its headers."""

    searches: list[str] = []

    def __init__(self, host: str, port: int) -> None:
        pass

    def __enter__(self) -> FakeImap:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def login(self, user: str, password: str) -> None:
        return None

    def select(self, folder: str, readonly: bool = False) -> None:
        return None

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        if command == "search":
            FakeImap.searches.append(args[0])
            return ("OK", [b"7 9"] if "pixel-studio.example" in args[0] else [b""])
        # an hour ago: a fixed date stopped being «after the letter» two days later (the test went red on 2026-10-02)
        date = format_datetime(utcnow() - timedelta(hours=1))
        header = f"Subject: =?utf-8?b?UmU6INCf0LjRgdGM0LzQvg==?=\r\nDate: {date}\r\n\r\n".encode()
        return ("OK", [(f"9 (BODY[HEADER.FIELDS (SUBJECT DATE)] {{{len(header)}}}".encode(), header), b")"])


def test_reply_search_by_domain_or_exact_address(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(mail_sync.imaplib, "IMAP4_SSL", FakeImap)
    FakeImap.searches = []
    since = utcnow() - timedelta(days=2)
    found = mail_sync._studio_replies_sync(
        settings, [(1, "@pixel-studio.example", since), (2, "studio@gmail.com", since)]
    )
    assert [(f[0], f[1]) for f in found] == [(1, "Re: Письмо")]
    assert (
        FakeImap.searches[0].endswith('FROM "@pixel-studio.example")')
        and 'FROM "studio@gmail.com"' in FakeImap.searches[1]
    )
    # an answer dated before the letter (same day) does not count
    found = mail_sync._studio_replies_sync(settings, [(1, "@pixel-studio.example", utcnow() + timedelta(days=30))])
    assert found == []


async def test_public_mailbox_is_matched_by_the_full_address(api) -> None:
    client, runtime = api
    async with runtime.session_factory() as session:
        row, _ = await studios.upsert_studio(session, name="Пиксель", source="list", site_url=SITE)
        row.status, row.sent_via, row.sent_to, row.sent_at = "sent", "email", "Pixel.Studio@Gmail.com", utcnow()
        other, _ = await studios.upsert_studio(session, name="Другая", source="list", site_url="https://other.example")
        other.status, other.sent_via, other.sent_to, other.sent_at = "sent", "telegram", "@other", utcnow()
        await session.commit()
        targets = await studios.reply_targets(session)
    assert [(t[0], t[1]) for t in targets] == [(row.id, "pixel.studio@gmail.com")]
    async with runtime.session_factory() as session:
        letters = (await session.execute(select(StudioLetter))).scalars().all()
    assert letters == []
