"""hirehi.ru — the second source of «Вакансии»: the searches and the careful
reading (robots.txt, the first pages until a known vacancy, the cards of new vacancies that passed the rules only, the
refusal of the site = a pause and a message), the vacancies of hh.ru through hh.ru, the duplicates between the sites,
the tasks of hh.ru that never touch a HireHi vacancy, its own check of closed vacancies, the packet for Claude, the
manual response and the API. Every request of the panel is mocked: an unexpected one fails the test (respx)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from alembic import command
from httpx import ASGITransport
from sqlalchemy import func, select

from app import scheduler as scheduler_module
from app.config import Settings
from app.db.base import utcnow
from app.db.models import Application, JobRun, Notification, SavedSearch, Vacancy
from app.db.session import alembic_config
from app.hirehi.parse import HireHiParams, parse_card, parse_listing
from app.main import app
from app.runtime import Runtime
from app.services import hirehi
from app.services.jobs import FAILED_KEY
from app.services.preferences import Preferences, SkillWeight, save_preferences
from app.services.robots_rules import RobotsRules
from app.services.scanner import apply_data_to_row, vacancy_data_from_row
from app.services.state import get_system_state, set_setting
from tests.integration.test_api_m2 import DICTS
from tests.unit.test_hirehi_parse import card_html, job, listing_html, slug
from tests.unit.test_vacancy_norm import load_full, short_item

HH = "https://api.hh.ru"
SITE = "https://hirehi.ru"
ROBOTS = """User-agent: *
Allow: /
Disallow: /admin/*
Disallow: /api/
Disallow: /*?*search=
Disallow: /*?*sort=
"""
PARAMS = HireHiParams(section="backend", formats=["remote_russia"])


@pytest.fixture
async def api(settings: Settings, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(hirehi, "PAUSE_SEC", 0.0)
    runtime = await Runtime.create(settings.model_copy(update={"batch_auto_create": False, "hirehi_sync_min": 0}))
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


def ago(**delta: float) -> str:
    return (utcnow() - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def card_url(j: dict[str, Any]) -> str:
    return f"{SITE}/{j['category']}/{slug(j['title'])}-{j['id']}"


def mock_site(m: respx.MockRouter, *, robots: str = ROBOTS) -> respx.Route:
    return m.get(url__eq=f"{SITE}/robots.txt").mock(return_value=httpx.Response(200, text=robots))


def mock_listing(m: respx.MockRouter, jobs: list[dict[str, Any]], *, page: int = 1, **kw: Any) -> respx.Route:
    return m.get(url__eq=SITE + PARAMS.path(page)).mock(return_value=httpx.Response(200, text=listing_html(jobs, **kw)))


def mock_card(m: respx.MockRouter, j: dict[str, Any], **kw: Any) -> respx.Route:
    return m.get(url__eq=card_url(j)).mock(
        return_value=httpx.Response(200, text=card_html(int(j["id"]), j["title"], **kw))
    )


def mock_hh(m: respx.MockRouter) -> None:
    m.post(f"{HH}/token").mock(return_value=httpx.Response(200, json={"access_token": "app", "token_type": "bearer"}))
    m.get(f"{HH}/dictionaries").mock(return_value=httpx.Response(200, json=DICTS))


async def prefs(runtime: Runtime) -> None:
    async with runtime.session_factory() as session:
        await save_preferences(
            session,
            Preferences(
                skills_weights=[
                    SkillWeight(name="Python", weight=5),
                    SkillWeight(name="FastAPI", weight=4),
                    SkillWeight(name="Kafka", weight=5),
                ],
                work_formats=["REMOTE", "HYBRID"],
                areas=["Казань"],
                stop_words_title=["продаж"],
            ),
        )
        await session.commit()


async def create_search(client: httpx.AsyncClient, params: HireHiParams = PARAMS, **extra: Any) -> dict[str, Any]:
    body = {"name": "HireHi: Backend", "source": "hirehi", "hirehi": params.model_dump(), "interval_min": 60, **extra}
    r = await client.post("/api/searches", json=body)
    assert r.status_code == 200, r.text
    return dict(r.json())


async def rows(runtime: Runtime) -> dict[str, Vacancy]:
    async with runtime.session_factory() as session:
        found = (await session.execute(select(Vacancy).order_by(Vacancy.id))).scalars().all()
    return {(v.source_id if v.source == "hirehi" else f"hh{v.hh_id}"): v for v in found}


def hirehi_row(jid: int, title: str, *, status: str = "new", card: bool = False, **fields: Any) -> Vacancy:
    """A HireHi vacancy written straight into the base (as the reader leaves it)."""
    item = parse_listing(listing_html([job(jid, title, created=ago(hours=1))])).items[0]
    row = Vacancy(
        hh_id=None,
        source="hirehi",
        source_id=str(jid),
        name=title,
        pipeline_status=status,
        source_search_ids=",",
        first_seen_at=utcnow(),
        last_seen_at=utcnow(),
        raw_short_json=json.dumps(item.raw(), ensure_ascii=False),
        alternate_url=f"{SITE}{item.path}",
        apply_alternate_url=f"{SITE}{item.path}",
        employer_name="Рога и копыта",
        published_at=utcnow() - timedelta(hours=1),
    )
    if card:
        row.raw_full_json = json.dumps(parse_card(card_html(jid, title)).raw(), ensure_ascii=False)
        row.full_fetched_at = utcnow()
    apply_data_to_row(row, vacancy_data_from_row(row), {"RUR": 1.0})  # as the reader stores it
    row.source = "hirehi"
    for key, value in fields.items():
        setattr(row, key, value)
    return row


# ---- reading a search ----------------------------------------------------------------------------------------------


@respx.mock(assert_all_called=False)
async def test_a_search_reads_the_first_page_and_only_the_cards_of_new_vacancies_that_passed_the_rules(
    api, respx_mock: respx.MockRouter
) -> None:
    client, runtime = api
    await prefs(runtime)
    search = await create_search(client)
    assert search["interval_min"] == 60 and search["source"] == "hirehi"
    good = job(1004, "Backend engineer", created=ago(hours=1))
    sales = job(1003, "Менеджер по продажам ПО", created=ago(hours=2))
    from_hh = job(1002, "Kafka engineer", created=ago(hours=3))
    old = job(1001, "Python developer", created=ago(days=20))
    robots = mock_site(respx_mock)
    mock_hh(respx_mock)
    listing = mock_listing(respx_mock, [good, sales, from_hh, old], total=195, has_more=True)
    good_card = mock_card(respx_mock, good)
    hh_card = mock_card(respx_mock, from_hh, label="Отклик на hh")
    published = (utcnow() - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    hh_item = short_item(
        id="555",
        name="Kafka-инженер",
        employer={"id": "777", "name": "Рога и копыта"},
        published_at=published,
        alternate_url="https://hh.ru/vacancy/555",
    )
    hh_search = respx_mock.get(f"{HH}/vacancies", params={"employer_id": "777"}).mock(
        return_value=httpx.Response(200, json={"found": 1, "pages": 1, "items": [hh_item]})
    )
    full = {**load_full(), "id": "555", "name": "Kafka-инженер", "employer": {"id": "777", "name": "Рога и копыта"}}
    hh_full = respx_mock.get(f"{HH}/vacancies/555").mock(return_value=httpx.Response(200, json=full))

    summary = await hirehi.sync_hirehi(runtime, only_due=False)
    assert FAILED_KEY not in summary, summary
    # one listing page (the first run), the cards of the two new vacancies that passed the rules
    assert robots.call_count == 1 and listing.call_count == 1
    assert good_card.call_count == 1 and hh_card.call_count == 1
    assert hh_search.call_count == 1 and hh_full.call_count == 1

    got = await rows(runtime)
    assert "1001" not in got  # older than the first scan of hh.ru takes
    assert got["1004"].source == "hirehi" and got["1004"].hh_id is None
    assert got["1004"].pipeline_status == "prescored" and got["1004"].description_text
    assert got["1004"].alternate_url == card_url(good) and got["1004"].employer_id == "777"
    assert got["1003"].pipeline_status == "filtered_out" and "продаж" in (got["1003"].pipeline_reason or "")
    assert got["1003"].raw_full_json is None  # the rules on the listing data, no card
    # the vacancy of hh.ru goes through hh.ru: an hh.ru row, the HireHi one is its duplicate
    hh_row = got["hh555"]
    assert hh_row.source == "hh" and hh_row.raw_full_json and hh_row.pipeline_status != "new"
    assert f",{search['id']}," in hh_row.source_search_ids
    assert got["1002"].pipeline_status == "skipped" and got["1002"].duplicate_of_id == hh_row.id
    assert (got["1002"].pipeline_reason or "").startswith(hirehi.HH_ROUTED_REASON)
    async with runtime.session_factory() as session:
        stored = await session.get(SavedSearch, search["id"])
        assert stored is not None and stored.last_found == 195 and stored.last_new == 3 and stored.last_error is None

    # the next run: a new vacancy on top, the rest known — no second page, one new card
    newer = job(1005, "Python Kafka developer", created=ago(minutes=10))
    listing.mock(return_value=httpx.Response(200, text=listing_html([newer, good, sales, from_hh], has_more=True)))
    newer_card = mock_card(respx_mock, newer)
    summary = await hirehi.sync_hirehi(runtime, only_due=False)
    assert FAILED_KEY not in summary and listing.call_count == 2 and newer_card.call_count == 1
    assert good_card.call_count == 1 and hh_card.call_count == 1
    assert (await rows(runtime))["1005"].pipeline_status == "prescored"


@respx.mock(assert_all_called=False)
async def test_pages_go_on_until_a_vacancy_the_search_has_seen(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await create_search(client)
    mock_site(respx_mock)
    mock_hh(respx_mock)
    first = [job(2000 + n, f"Python developer {n}", created=ago(hours=n + 1)) for n in range(3)]
    page1 = mock_listing(respx_mock, first, has_more=True)
    for j in first:
        mock_card(respx_mock, j)
    await hirehi.sync_hirehi(runtime, only_due=False)
    assert page1.call_count == 1  # the first run: the first page only

    fresh = [job(2100 + n, f"Python developer new {n}", created=ago(minutes=n + 1)) for n in range(3)]
    page1.mock(return_value=httpx.Response(200, text=listing_html(fresh, has_more=True)))
    page2 = mock_listing(respx_mock, [first[0], *first[1:]], page=2, has_more=True)
    page3 = respx_mock.get(url__eq=SITE + PARAMS.path(3)).mock(return_value=httpx.Response(500))
    for j in fresh:
        mock_card(respx_mock, j)
    summary = await hirehi.sync_hirehi(runtime, only_due=False)
    assert FAILED_KEY not in summary
    # page 1 had nothing known, page 2 starts with a known vacancy: no page 3
    assert page1.call_count == 2 and page2.call_count == 1 and page3.call_count == 0
    assert {str(2100 + n) for n in range(3)} <= set(await rows(runtime))


@respx.mock(assert_all_called=False)
async def test_a_refusal_of_the_site_stops_the_reading_and_tells_the_bot(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await create_search(client)
    robots = mock_site(respx_mock)
    mock_hh(respx_mock)
    listing = respx_mock.get(url__eq=SITE + PARAMS.path(1)).mock(
        return_value=httpx.Response(403, text="<html><title>DDoS-Guard</title>Checking your browser</html>")
    )
    summary = await hirehi.sync_hirehi(runtime, only_due=False)
    assert summary["blocked"].startswith("HireHi ответил HTTP 403") and summary[FAILED_KEY]
    async with runtime.session_factory() as session:
        state = await hirehi.get_state(session)
        until = hirehi.blocked_until(state)
        assert until is not None and until - utcnow() > timedelta(hours=hirehi.BLOCK_HOURS - 1)
        notes = (
            (await session.execute(select(Notification).where(Notification.event_type == "job_error"))).scalars().all()
        )
        assert len(notes) == 1 and "HireHi" in notes[0].payload_json
        search = (await session.execute(select(SavedSearch))).scalar_one()
        assert "403" in (search.last_error or "")
    # while it refuses, nothing is asked: neither the tick nor a run nor «Проверить»
    assert not await hirehi.sync_due(runtime)
    skipped = await hirehi.sync_hirehi(runtime, only_due=False)
    assert "skipped" in skipped and robots.call_count == 1 and listing.call_count == 1
    r = await client.post("/api/searches/preview", json={"source": "hirehi", "hirehi": PARAMS.model_dump()})
    assert r.status_code == 409 and "HireHi" in r.json()["detail"]
    # a second refusal later does not repeat the message while the first pause lasts
    async with runtime.session_factory() as session:
        await hirehi.block(session, runtime.settings, "HireHi ответил HTTP 429")
        await session.commit()
        count = (
            await session.execute(select(func.count(Notification.id)).where(Notification.event_type == "job_error"))
        ).scalar()
        assert count == 1


@respx.mock(assert_all_called=False)
async def test_a_browser_check_instead_of_the_page_is_a_refusal_too(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await create_search(client)
    mock_site(respx_mock)
    mock_hh(respx_mock)
    respx_mock.get(url__eq=SITE + PARAMS.path(1)).mock(
        return_value=httpx.Response(200, text="<html><title>DDoS-Guard</title><body>Проверка браузера…</body></html>")
    )
    summary = await hirehi.sync_hirehi(runtime, only_due=False)
    assert "проверку браузера" in summary["blocked"]


@respx.mock(assert_all_called=False)
async def test_robots_txt_decides_what_is_read(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await create_search(client)
    mock_site(respx_mock, robots="User-agent: *\nDisallow: /vacancies/\n")
    mock_hh(respx_mock)
    listing = respx_mock.get(url__eq=SITE + PARAMS.path(1)).mock(return_value=httpx.Response(200, text=""))
    summary = await hirehi.sync_hirehi(runtime, only_due=False)
    assert listing.call_count == 0 and "robots.txt" in summary["searches"][0]["error"]
    assert summary[FAILED_KEY].startswith("все поиски HireHi")


# ---- a vacancy of hh.ru --------------------------------------------------------------------------------------------


@respx.mock(assert_all_called=False)
async def test_a_vacancy_of_hh_already_in_the_panel_takes_the_hirehi_one_as_its_duplicate(
    api, respx_mock: respx.MockRouter
) -> None:
    client, runtime = api
    search = await create_search(client)
    async with runtime.session_factory() as session:
        hh_row = Vacancy(
            hh_id="900",
            name="DevOps-инженер",
            employer_id="777",
            employer_name="Рога и копыта",
            pipeline_status="suitable",
            source_search_ids=",",
            first_seen_at=utcnow(),
            published_at=utcnow() - timedelta(hours=5),
        )
        session.add(hh_row)
        await session.commit()
    mock_site(respx_mock)
    mock_hh(respx_mock)
    rewritten = job(3001, "DevOps engineer for high-load applications", created=ago(hours=1))
    mock_listing(respx_mock, [rewritten])
    mock_card(respx_mock, rewritten, label="Отклик на hh")
    hh_search = respx_mock.get(f"{HH}/vacancies").mock(return_value=httpx.Response(500))
    await hirehi.sync_hirehi(runtime, only_due=False)
    got = await rows(runtime)
    assert hh_search.call_count == 0  # found in the panel, hh.ru not asked
    assert got["3001"].pipeline_status == "skipped" and got["3001"].duplicate_of_id == got["hh900"].id
    assert got["hh900"].pipeline_status == "suitable"  # its work stays
    assert f",{search['id']}," in got["hh900"].source_search_ids


@respx.mock(assert_all_called=False)
async def test_a_vacancy_of_hh_not_found_on_hh_is_skipped_with_the_reason(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await create_search(client)
    mock_site(respx_mock)
    mock_hh(respx_mock)
    vac = job(3101, "Kafka engineer", created=ago(hours=1), icon="/static/uploads/Some_url_20261016110557_4b49ec.webp")
    mock_listing(respx_mock, [vac])
    mock_card(respx_mock, vac, label="Отклик на hh")
    # no hh.ru number in the logo: one employer of hh.ru with exactly this name, then its recent vacancies
    employers = respx_mock.get(f"{HH}/employers").mock(
        return_value=httpx.Response(200, json={"items": [{"id": "31", "name": "Рога и копыта"}]})
    )
    unrelated = short_item(id="556", name="Бухгалтер", employer={"id": "31", "name": "Рога и копыта"})
    respx_mock.get(f"{HH}/vacancies", params={"employer_id": "31"}).mock(
        return_value=httpx.Response(200, json={"found": 1, "pages": 1, "items": [unrelated]})
    )
    summary = await hirehi.sync_hirehi(runtime, only_due=False)
    got = await rows(runtime)
    assert employers.call_count == 1 and "hh556" not in got
    assert got["3101"].pipeline_status == "skipped" and got["3101"].duplicate_of_id is None
    assert got["3101"].pipeline_reason == hirehi.HH_NOT_FOUND_REASON and summary["hh_not_found"] == 1


@respx.mock(assert_all_called=False)
async def test_the_twin_of_a_busy_employer_is_looked_for_around_its_date_and_on_more_pages(
    api, respx_mock: respx.MockRouter
) -> None:
    """A big employer publishes hundreds of vacancies a week: hh.ru is asked for the window of the date HireHi listed
    the vacancy (newest first from its end), and the next page is read while none is similar (TWIN_PAGES)."""
    client, runtime = api
    await create_search(client)
    mock_site(respx_mock)
    mock_hh(respx_mock)
    vac = job(3301, "Kafka engineer", created=ago(hours=2))  # the logo carries the hh.ru number 777
    mock_listing(respx_mock, [vac])
    mock_card(respx_mock, vac, label="Отклик на hh")
    published = (utcnow() - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    employer = {"id": "777", "name": "Рога и копыта"}
    others = [
        short_item(id=str(7000 + n), name=f"Кассир {n}", employer=employer, published_at=published) for n in range(100)
    ]
    twin = short_item(id="7999", name="Kafka-инженер", employer=employer, published_at=published)
    asked: list[dict[str, str]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        asked.append(params)
        page = int(params.get("page", "0"))
        return httpx.Response(
            200, json={"found": 101, "pages": 2, "page": page, "items": others if page == 0 else [twin]}
        )

    respx_mock.get(f"{HH}/vacancies", params={"employer_id": "777"}).mock(side_effect=answer)
    full = {**load_full(), "id": "7999", "name": "Kafka-инженер", "employer": employer}
    respx_mock.get(f"{HH}/vacancies/7999").mock(return_value=httpx.Response(200, json=full))
    await hirehi.sync_hirehi(runtime, only_due=False)
    got = await rows(runtime)
    assert [p["page"] for p in asked] == ["0", "1"]
    assert all("period" not in p and p["date_from"] < p["date_to"] for p in asked)
    assert got["3301"].pipeline_status == "skipped" and got["3301"].duplicate_of_id == got["hh7999"].id


@respx.mock(assert_all_called=False)
async def test_a_scan_duplicate_of_the_hirehi_row_goes_back_to_hh(api, respx_mock: respx.MockRouter) -> None:
    """The scan judged the hh.ru vacancy a duplicate of the HireHi row (same title, the HireHi row came first); once the
    card says «Отклик на hh», the hh.ru vacancy is the one that works and the HireHi row its duplicate."""
    client, runtime = api
    await create_search(client)
    mock_site(respx_mock)
    mock_hh(respx_mock)
    vac = job(3201, "Backend engineer", created=ago(hours=1))
    mock_listing(respx_mock, [vac])
    mock_card(respx_mock, vac, label="Отклик на hh")
    async with runtime.session_factory() as session:
        first = hirehi_row(3201, "Backend engineer", employer_id="777")
        first.raw_short_json = json.dumps(parse_listing(listing_html([vac])).items[0].raw(), ensure_ascii=False)
        session.add(first)
        await session.flush()
        twin = Vacancy(
            hh_id="901",
            name="Backend engineer",
            employer_id="777",
            employer_name="Рога и копыта",
            pipeline_status="skipped",
            duplicate_of_id=first.id,
            source_search_ids=",",
            first_seen_at=utcnow(),
            published_at=utcnow() - timedelta(hours=2),
        )
        session.add(twin)
        await session.commit()
    full = {**load_full(), "id": "901", "name": "Backend engineer", "employer": {"id": "777", "name": "Рога и копыта"}}
    hh_full = respx_mock.get(f"{HH}/vacancies/901").mock(return_value=httpx.Response(200, json=full))
    await hirehi.sync_hirehi(runtime, only_due=False)
    got = await rows(runtime)
    assert got["hh901"].duplicate_of_id is None and got["hh901"].pipeline_status not in ("skipped", "new")
    assert hh_full.call_count == 1
    assert got["3201"].duplicate_of_id == got["hh901"].id and got["3201"].pipeline_status == "skipped"


# ---- duplicates between the sources ----------------------------------------------------------------------------------


async def test_duplicates_compare_the_sites_by_the_hh_number_or_the_name(api) -> None:
    from app.services.scanner import find_duplicate

    _, runtime = api
    async with runtime.session_factory() as session:
        hh_row = Vacancy(
            hh_id="1",
            name="Backend engineer",
            employer_id="777",
            employer_name="Рога и копыта",
            pipeline_status="prescored",
            source_search_ids=",",
            first_seen_at=utcnow(),
        )
        session.add(hh_row)
        await session.flush()
        by_number = hirehi_row(4001, "Backend Engineer", employer_id="777", employer_name="Другое имя")
        by_name = hirehi_row(4002, "Backend engineer", employer_id=None, employer_name="РОГА И КОПЫТА")
        session.add_all([by_number, by_name])
        await session.flush()
        assert (await find_duplicate(session, by_number)) is hh_row
        assert (await find_duplicate(session, by_name)) is hh_row
        # an hh.ru vacancy after a HireHi one of a company without the number in the logo: the earlier stays
        lone = hirehi_row(4003, "Data engineer", employer_id=None, employer_name="Рога и копыта")
        session.add(lone)
        await session.flush()
        later = Vacancy(
            hh_id="2",
            name="Data engineer",
            employer_id="777",
            employer_name="Рога и копыта",
            pipeline_status="new",
            source_search_ids=",",
            first_seen_at=utcnow(),
        )
        session.add(later)
        await session.flush()
        assert (await find_duplicate(session, later)) is lone
        # a duplicate of this very vacancy is no twin of it
        lone.duplicate_of_id = later.id
        lone.pipeline_status = "skipped"
        await session.flush()
        assert (await find_duplicate(session, later)) is None


# ---- the tasks of hh.ru ----------------------------------------------------------------------------------------------


@respx.mock(assert_all_called=False)
async def test_the_tasks_of_hh_never_touch_a_hirehi_vacancy(api, respx_mock: respx.MockRouter) -> None:
    from app.services.applier import autopilot_enqueue, enqueue, limits_status, send_one
    from app.services.freshness import check_closed as hh_check_closed
    from app.services.responses_refresh import refresh_responses
    from app.services.scanner import reprocess_all, run_scan

    client, runtime = api
    await create_search(client)  # an enabled HireHi search: the responses pass must not read it
    mock_hh(respx_mock)  # only the token and the dictionaries: any other request of hh.ru fails the test
    old = utcnow() - timedelta(days=5)
    async with runtime.session_factory() as session:
        fresh = hirehi_row(5001, "Backend engineer")
        stale = hirehi_row(5002, "Kafka engineer", status="prescored", card=True, last_seen_at=old, prescore=60)
        ready = hirehi_row(5003, "Python developer", status="suitable", card=True, score=85)
        session.add_all([fresh, stale, ready])
        state = await get_system_state(session)
        state.apply_mode = "api"
        state.consent_accepted_at = utcnow()
        await session.commit()
        ids = (fresh.id, stale.id, ready.id)

    scan = await run_scan(runtime)
    assert scan.get("processed", {}).get("processed") == 0
    closed = await hh_check_closed(runtime)
    assert closed["candidates"] == 0
    passes = await refresh_responses(runtime)
    assert passes["searches"] == 0 and passes["requests"] == 0 and passes["errors"] == 0
    await reprocess_all(runtime, refetch=True)
    async with runtime.session_factory() as session:
        f, s, r = [await session.get(Vacancy, i) for i in ids]
        assert f is not None and f.pipeline_status == "new"  # waits for the reader of HireHi
        assert s is not None and s.pipeline_status == "prescored" and not s.archived  # its stored card, no request
        assert r is not None
        from app.services.letters import add_letter

        await add_letter(session, r, text="Здравствуйте! " * 60, origin="claude")
        await set_setting(session, "manual_resume_id", "resume-1")
        await session.commit()
        outcome = await enqueue(session, runtime.settings, [r.id])
        assert outcome["queued"] == [] and "только вручную" in outcome["rejected"][0]["reason"]
        assert await autopilot_enqueue(session, runtime.settings.model_copy(update={"apply_autopilot": True})) == []
        stray = Application(vacancy_id=r.id, mode="api", status="queued", queued_at=utcnow())
        session.add(stray)
        await session.flush()
        assert await send_one(session, runtime.settings, runtime.hh_client(), stray) == "failed"
        assert "вручную" in (stray.error_text or "")
        session.add(Application(vacancy_id=s.id, mode="assist", status="sent", sent_at=utcnow()))
        await session.commit()
        limits = await limits_status(session, runtime.settings)
        assert limits.today_sent == 0  # a response on another site takes nothing of the limits of hh.ru


# ---- closed vacancies of HireHi --------------------------------------------------------------------------------------


@respx.mock(assert_all_called=False)
async def test_the_check_closes_by_the_term_or_a_removed_page(api, respx_mock: respx.MockRouter) -> None:
    _, runtime = api
    now = utcnow()
    old = now - timedelta(days=3)
    async with runtime.session_factory() as session:
        expired = hirehi_row(6001, "Backend engineer", status="prescored")
        expired.raw_full_json = json.dumps(
            parse_card(card_html(6001, "Backend engineer", valid="2026-01-01T00:00:00")).raw(), ensure_ascii=False
        )
        gone = hirehi_row(6002, "Kafka engineer", status="suitable", card=True, last_seen_at=old, full_fetched_at=old)
        alive = hirehi_row(6003, "Python developer", status="maybe", card=True, last_seen_at=old, full_fetched_at=old)
        recent = hirehi_row(6004, "Data engineer", status="maybe", card=True)
        session.add_all([expired, gone, alive, recent])
        await session.flush()
        late = Application(vacancy_id=gone.id, mode="assist", status="needs_manual", queued_at=now - timedelta(days=4))
        session.add(late)
        await session.commit()
        ids = (expired.id, gone.id, alive.id, recent.id, late.id)
    gone_card = respx_mock.get(url__eq=f"{SITE}/development/kafka-engineer-6002").mock(return_value=httpx.Response(404))
    alive_card = respx_mock.get(url__eq=f"{SITE}/development/python-developer-6003").mock(
        return_value=httpx.Response(200, text=card_html(6003, "Python developer"))
    )
    async with runtime.session_factory() as session:
        summary = await hirehi.check_closed(
            session, runtime.http, runtime.settings, RobotsRules.parse(ROBOTS), {"RUR": 1.0}, now=now
        )
        await session.commit()
    assert summary["expired"] == 1 and summary["gone"] == 1 and summary["checked"] == 2
    assert summary["responses_cancelled"] == 1
    assert gone_card.call_count == 1 and alive_card.call_count == 1  # the recent one is not re-read
    async with runtime.session_factory() as session:
        e, g, a, r = [await session.get(Vacancy, i) for i in ids[:4]]
        app_row = await session.get(Application, ids[4])
        assert e is not None and e.archived and e.pipeline_reason == hirehi.EXPIRED_REASON
        assert g is not None and g.archived and g.pipeline_reason == hirehi.GONE_REASON
        assert g.raw_full_json is not None and json.loads(g.raw_full_json).get("gone_at")
        assert a is not None and not a.archived and a.pipeline_status == "maybe"
        assert r is not None and r.pipeline_status == "maybe"
        assert app_row is not None and app_row.status == "cancelled" and "HireHi" in (app_row.error_text or "")
        # the closed one says where to look; «Вернуть» refuses it
        from app.services.vacancies import VacancyActionError, restore

        with pytest.raises(VacancyActionError, match="HireHi.*Переоценить"):
            await restore(session, e)


async def test_the_nightly_cancel_of_hh_leaves_the_responses_of_hirehi(api) -> None:
    from app.services.freshness import cancel_unconfirmed

    _, runtime = api
    now = utcnow()
    async with runtime.session_factory() as session:
        closed = hirehi_row(6101, "Backend engineer", status="filtered_out", archived=True)
        session.add(closed)
        await session.flush()
        session.add(
            Application(vacancy_id=closed.id, mode="assist", status="needs_manual", queued_at=now - timedelta(days=5))
        )
        await session.commit()
        assert await cancel_unconfirmed(session, now, source="hh") == 0
        assert await cancel_unconfirmed(session, now, source="hirehi") == 1


# ---- the packet, the letter, the response ------------------------------------------------------------------------


async def test_a_hirehi_vacancy_goes_to_claude_and_comes_back_by_its_key(api, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.services.notify import render_event
    from app.services.tg_letters import keyboard

    client, runtime = api
    r = await client.post(
        "/api/profile/resumes/upload", files={"file": ("cv.txt", "Иван, Python, FastAPI".encode(), "text/plain")}
    )
    assert r.status_code == 200
    async with runtime.session_factory() as session:
        row = hirehi_row(7001, "Backend engineer", status="prescored", card=True, prescore=70)
        session.add(row)
        await session.commit()
        vid = row.id
    r = await client.post("/api/batches/create", json={"vacancy_ids": [vid]})
    assert r.status_code == 200, r.text
    packet_file = next((runtime.settings.exchange_dir / "pending").glob("*.json"))
    packet = json.loads(packet_file.read_text(encoding="utf-8"))
    task = packet["tasks"][0]
    assert task["vacancy"]["id"] == "hirehi-7001" and task["vacancy"]["alternate_url"].startswith(SITE)
    assert "Задачи" in task["vacancy"]["description"]
    letter = "Здравствуйте! Меня заинтересовала ваша вакансия. За семь лет я построил платёжный сервис. " * 8
    result = {
        "protocol_version": 1,
        "batch_id": packet["batch_id"],
        "profile_version": 1,
        "results": [
            {
                "vacancy_id": "hirehi-7001",
                "evaluation": {"score": 82, "verdict": "apply", "summary": "Подходит", "matched": ["Python"]},
                "letter": {"text": letter[:900], "language": "ru", "mentions": [], "needs_clarification": []},
                "error": None,
            }
        ],
    }
    (runtime.settings.exchange_dir / "done" / packet_file.name).write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8"
    )
    r = await client.post("/api/batches/import")
    assert r.json()["outcomes"][0]["status"] == "imported", r.text
    detail = (await client.get(f"/api/vacancies/{vid}")).json()
    assert detail["pipeline_status"] == "suitable" and detail["letter"]["text"]
    assert (
        detail["source"] == "hirehi" and detail["source_label"] == "HireHi" and detail["source_apply_label"] == "Отклик"
    )
    async with runtime.session_factory() as session:
        note = (
            await session.execute(
                select(Notification).where(Notification.event_type == "letter", Notification.channel == "ui")
            )
        ).scalar_one()
    payload = json.loads(note.payload_json)
    assert payload["source"] == "hirehi" and payload["apply_url"].startswith(SITE)
    assert "HireHi" in render_event("letter", [payload])
    assert (
        keyboard(vid, payload["apply_url"], payload["source"])["inline_keyboard"][1][0]["text"]
        == "Откликнуться на HireHi"
    )


async def test_the_response_to_a_hirehi_vacancy_is_made_by_hand_on_its_page(api) -> None:
    client, runtime = api
    async with runtime.session_factory() as session:
        row = hirehi_row(7101, "Backend engineer", status="suitable", card=True, score=80)
        session.add(row)
        await session.commit()
        vid = row.id
    assert (await client.post("/api/applications/consent")).status_code == 200
    r = await client.post("/api/applications/assist/open", json={"vacancy_id": vid})
    assert r.status_code == 200, r.text
    opened = r.json()
    assert opened["apply_url"] == f"{SITE}/development/backend-engineer-7101" and opened["source"] == "hirehi"
    assert opened["mode_label"] == "вручную на HireHi"
    detail = (await client.get(f"/api/vacancies/{vid}")).json()
    assert detail["pipeline_status"] == "needs_manual" and detail["status_label"] == "нужно действие на HireHi"
    r = await client.post(f"/api/applications/{opened['id']}/confirm", json={"sent": True})
    assert r.status_code == 200
    assert (await client.get(f"/api/vacancies/{vid}")).json()["pipeline_status"] == "sent"
    status = (await client.get("/api/applications/status")).json()
    assert status["limits"]["today_sent"] == 0
    stats = (await client.get("/api/stats", params={"period": "today"})).json()
    assert {"label": "HireHi"}.items() <= stats["breakdowns"]["by_source"][0].items()


# ---- the API -------------------------------------------------------------------------------------------------------


@respx.mock(assert_all_called=False)
async def test_the_api_of_hirehi_searches(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    options = (await client.get("/api/searches/hirehi/options")).json()
    assert {"id": "backend", "name": "Backend"} in options["subcategories"] and options["min_interval"] == 60
    assert options["state"]["blocked_until"] is None
    s = await create_search(client, interval_min=10)
    assert s["interval_min"] == 60 and s["hirehi"]["section"] == "backend" and "Backend" in s["summary"]
    assert s["params"]["text"] == ""  # no hh.ru parameters for a HireHi search
    bad = {"name": "x", "source": "hirehi", "hirehi": {"city": "kazan", "section": "backend"}}
    r = await client.post("/api/searches", json=bad)
    assert r.status_code == 422 and "категория" in r.json()["detail"]
    r = await client.put(
        f"/api/searches/{s['id']}",
        json={"name": "HireHi: Казань", "source": "hirehi", "hirehi": {"city": "kazan", "formats": ["гибрид"]}},
    )
    assert r.status_code == 200 and r.json()["hirehi"]["city"] == "kazan" and "Казань" in r.json()["summary"]

    mock_site(respx_mock)
    page = listing_html(
        [job(8001, "Backend engineer", created=ago(hours=1)), job(8002, "Sales lead", created=ago(hours=2))]
    )
    respx_mock.get(url__eq=SITE + PARAMS.model_copy(update={"keywords": ["Backend"]}).path(1)).mock(
        return_value=httpx.Response(200, text=page)
    )
    r = await client.post(
        "/api/searches/preview", json={"source": "hirehi", "hirehi": {**PARAMS.model_dump(), "keywords": ["Backend"]}}
    )
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["matched"] == 1 and preview["items"][1]["reason"] == "в названии нет слов поиска"
    assert preview["items"][0]["alternate_url"] == f"{SITE}/development/backend-engineer-8001"
    async with runtime.session_factory() as session:
        assert (await session.execute(select(func.count(Vacancy.id)))).scalar() == 0  # nothing stored

    # «Запустить» of a HireHi search starts its reading, no hh.ru sign-in needed
    respx_mock.get(url__eq=f"{SITE}/kazan-jobs?format=%D0%B3%D0%B8%D0%B1%D1%80%D0%B8%D0%B4").mock(
        return_value=httpx.Response(200, text=listing_html([]))
    )
    mock_hh(respx_mock)
    r = await client.post(f"/api/searches/{s['id']}/run")
    assert r.status_code == 200, r.text
    await runtime.jobs.wait(hirehi.JOB)
    async with runtime.session_factory() as session:
        run = (await session.execute(select(JobRun).where(JobRun.job == hirehi.JOB))).scalar_one()
        assert run.status == "ok", run.error
    r = await client.post("/api/jobs/hirehi")
    assert r.status_code == 200
    await runtime.jobs.wait(hirehi.JOB)


@respx.mock(assert_all_called=False)
async def test_the_list_and_the_card_name_the_site_and_reprocess_reads_the_card_again(
    api, respx_mock: respx.MockRouter
) -> None:
    client, runtime = api
    async with runtime.session_factory() as session:
        row = hirehi_row(9001, "Backend engineer", status="not_suitable", card=True, prescore=20)
        session.add(row)
        await session.commit()
        vid = row.id
    listing = (await client.get("/api/vacancies", params={"group": "all"})).json()
    item = next(v for v in listing["items"] if v["id"] == vid)
    assert item["source"] == "hirehi" and item["source_label"] == "HireHi" and item["hh_id"] is None
    assert item["alternate_url"] == item["apply_alternate_url"] == f"{SITE}/development/backend-engineer-9001"
    mock_site(respx_mock)
    mock_hh(respx_mock)
    card = respx_mock.get(url__eq=f"{SITE}/development/backend-engineer-9001").mock(
        return_value=httpx.Response(200, text=card_html(9001, "Backend engineer", label="Отклик в Telegram"))
    )
    r = await client.post(f"/api/vacancies/{vid}/reprocess")
    assert r.status_code == 200, r.text
    assert card.call_count == 1 and r.json()["source_apply_label"] == "Отклик в Telegram"
    card.mock(return_value=httpx.Response(404))
    r = await client.post(f"/api/vacancies/{vid}/reprocess")
    assert r.json()["archived"] and r.json()["pipeline_reason"] == hirehi.GONE_REASON


# status, a response, what the card answers → the status and `archived` after «Переоценить»
REPROCESS_CASES = [
    ("sent", "sent", "gone", "sent", False),
    ("sent", "sent", "expired", "sent", True),
    ("awaiting_claude", None, "gone", "awaiting_claude", False),
    ("prescored", None, "gone", "filtered_out", True),
    ("prescored", None, "expired", "filtered_out", True),
]


@pytest.mark.parametrize(("status", "app_status", "answer", "after", "archived"), REPROCESS_CASES)
@respx.mock(assert_all_called=False)
async def test_reprocess_keeps_a_response_or_a_packet_and_closes_only_a_free_vacancy(
    api,
    respx_mock: respx.MockRouter,
    status: str,
    app_status: str | None,
    answer: str,
    after: str,
    archived: bool,
) -> None:
    """«Переоценить» with a fresh card, as on hh.ru: a vacancy with a response or in a packet keeps its status and its
    packet, only the card is refreshed; a removed card or a passed term closes only a vacancy nothing holds (a sent
    one used to leave «Отправлено» and could not come back)."""
    client, runtime = api
    jid = 9101
    async with runtime.session_factory() as session:
        row = hirehi_row(jid, "Backend engineer", status=status, card=True, prescore=70)
        if status == "awaiting_claude":
            row.batch_id = "20261108_1000_abcd"
        session.add(row)
        await session.flush()
        if app_status:
            session.add(
                Application(
                    vacancy_id=row.id,
                    mode="assist",
                    status=app_status,
                    letter_text="Здравствуйте!",
                    queued_at=utcnow(),
                    sent_at=utcnow(),
                )
            )
        await session.commit()
        vid = row.id
    mock_site(respx_mock)
    mock_hh(respx_mock)
    page = respx_mock.get(url__eq=f"{SITE}/development/backend-engineer-{jid}")
    if answer == "gone":
        page.mock(return_value=httpx.Response(404))
    else:
        page.mock(
            return_value=httpx.Response(200, text=card_html(jid, "Backend engineer", valid="2026-01-01T00:00:00"))
        )
    r = await client.post(f"/api/vacancies/{vid}/reprocess")
    assert r.status_code == 200, r.text
    data = r.json()
    assert (data["pipeline_status"], data["archived"]) == (after, archived), data["pipeline_reason"]
    assert page.call_count == 1
    async with runtime.session_factory() as session:
        stored = await session.get(Vacancy, vid)
        assert stored is not None
        assert stored.batch_id == ("20261108_1000_abcd" if status == "awaiting_claude" else None)
        if after == status:
            assert stored.pipeline_reason is None


# ---- the scheduler -------------------------------------------------------------------------------------------------


def test_the_scheduler_has_the_tick_of_hirehi_unless_it_is_off() -> None:
    from types import SimpleNamespace

    from app.scheduler import build_scheduler

    on = build_scheduler(SimpleNamespace(settings=Settings(_env_file=None)))  # type: ignore[arg-type, call-arg]
    tick = next(j for j in on.get_jobs() if j.id == "hirehi_sync")
    assert tick.trigger.interval == timedelta(minutes=15)
    off = build_scheduler(SimpleNamespace(settings=Settings(_env_file=None, hirehi_sync_min=0)))  # type: ignore[arg-type, call-arg]
    assert "hirehi_sync" not in {j.id for j in off.get_jobs()}


@respx.mock(assert_all_called=False)
async def test_the_tick_waits_for_a_scan_of_the_group(api, respx_mock: respx.MockRouter, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client, runtime = api
    await create_search(client)
    mock_site(respx_mock)
    mock_hh(respx_mock)
    listing = mock_listing(respx_mock, [])
    monkeypatch.setattr(scheduler_module, "RESPONSES_RETRY_SEC", 0.01)
    release = asyncio.Event()

    async def scan() -> dict[str, Any]:
        await release.wait()
        return {}

    await runtime.jobs.start("scan", scan)
    tick = asyncio.create_task(scheduler_module.hirehi_tick(runtime))
    await asyncio.sleep(0.1)
    assert not tick.done() and listing.call_count == 0  # waits for the scan, does not give up
    release.set()
    await runtime.jobs.wait("scan")
    await asyncio.wait_for(tick, 5)
    await runtime.jobs.wait(hirehi.JOB)
    assert listing.call_count == 1
    assert not await hirehi.sync_due(runtime)  # read: the next one in an hour


# ---- the migration -------------------------------------------------------------------------------------------------


def test_the_migration_keeps_the_vacancies_of_hh_and_refuses_to_lose_those_of_hirehi(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(_env_file=None, data_dir=tmp_path, logs_dir=tmp_path / "logs")  # type: ignore[call-arg]
    cfg = alembic_config(settings)
    command.upgrade(cfg, "0031_listing_responses")
    con = sqlite3.connect(settings.db_path)  # type: ignore[arg-type]
    con.execute(
        "insert into vacancies (hh_id, name, source_search_ids, pipeline_status, first_seen_at, created_at, updated_at, "
        "archived, response_letter_required, has_test, full_fetch_errors, hidden, user_override, claude_attempts) "
        "values ('77', 'Инженер', ',', 'new', '2026-11-01 10:00:00', '2026-11-01 10:00:00', '2026-11-01 10:00:00', "
        "0, 0, 0, 0, 0, 0, 0)"
    )
    con.commit()
    con.close()
    command.upgrade(cfg, "head")
    con = sqlite3.connect(settings.db_path)  # type: ignore[arg-type]
    assert con.execute("select hh_id, source, source_id from vacancies").fetchall() == [("77", "hh", None)]
    con.execute(
        "insert into vacancies (hh_id, source, source_id, name, source_search_ids, pipeline_status, first_seen_at, "
        "created_at, updated_at, archived, response_letter_required, has_test, full_fetch_errors, hidden, user_override, "
        "claude_attempts) values (NULL, 'hirehi', '218432', 'Backend engineer', ',', 'new', '2026-11-08 10:00:00', "
        "'2026-11-08 10:00:00', '2026-11-08 10:00:00', 0, 0, 0, 0, 0, 0, 0)"
    )
    con.commit()
    with pytest.raises(sqlite3.IntegrityError):  # one row per number of a source
        con.execute(
            "insert into vacancies (hh_id, source, source_id, name, source_search_ids, pipeline_status, first_seen_at, "
            "created_at, updated_at, archived, response_letter_required, has_test, full_fetch_errors, hidden, "
            "user_override, claude_attempts) values (NULL, 'hirehi', '218432', 'x', ',', 'new', '2026-11-08 10:00:00', "
            "'2026-11-08 10:00:00', '2026-11-08 10:00:00', 0, 0, 0, 0, 0, 0, 0)"
        )
    con.close()
    with pytest.raises(RuntimeError, match="HireHi"):
        command.downgrade(cfg, "0031_listing_responses")
    con = sqlite3.connect(settings.db_path)  # type: ignore[arg-type]
    con.execute("delete from vacancies where source = 'hirehi'")
    con.commit()
    con.close()
    command.downgrade(cfg, "0031_listing_responses")
    command.upgrade(cfg, "head")


def test_dates_of_the_fixtures_are_utc() -> None:
    assert datetime.fromisoformat(ago(hours=1).replace("Z", "+00:00")).tzinfo == UTC
