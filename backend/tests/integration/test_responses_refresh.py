"""The refresh of the responses counter (job `responses`): hh.ru gives the number only in search results and the scan
sees a vacancy when it is published or raised, so every few hours the panel re-reads the results of the enabled
searches for the last days and updates the counter of the vacancies it has — and nothing else of them."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import httpx
import respx
from sqlalchemy import func, select

from app import scheduler as scheduler_module
from app.db.base import as_utc, utcnow
from app.db.models import AuditLog, JobRun, SavedSearch, Vacancy
from app.scheduler import build_scheduler, responses_tick
from app.services.jobs import FAILED_KEY
from app.services.responses_refresh import JOB, refresh_due, refresh_responses
from app.services.scanner import vacancy_data_from_row
from app.services.state import get_system_state, set_pause
from tests.integration.test_api_m2 import BASE, _login, api  # noqa: F401
from tests.unit.test_vacancy_norm import short_item

__all__ = ["api"]

DAY = timedelta(days=1)


def _item(hh_id: str, responses: int | None) -> dict[str, Any]:
    item = short_item(id=hh_id, name=f"Вакансия {hh_id}", alternate_url=f"https://hh.ru/vacancy/{hh_id}")
    if responses is None:
        item.pop("counters")
    else:
        item["counters"] = {"responses": responses}
    return item


# hh_id: (responses kept by the panel, status, the search item is stored — a vacancy added by a link has none)
KNOWN: dict[str, tuple[int | None, str, bool]] = {
    "101": (5, "maybe", True),  # found in its first half hour: 5 then, 1200 now
    "102": (900, "suitable", True),  # unchanged
    "103": (None, "prescored", False),  # added by a link: no search item, no counter
    "104": (50, "watched", True),  # two searches return it
    "105": (7, "maybe", True),  # no search returns it any more
    "106": (40, "sent", True),  # the counter of any status is refreshed
}
SEEN = utcnow() - 3 * DAY


async def _seed(runtime) -> None:  # type: ignore[no-untyped-def]
    published = utcnow() - DAY
    async with runtime.session_factory() as session:
        for hh_id, (responses, status, stored) in KNOWN.items():
            session.add(
                Vacancy(
                    hh_id=hh_id,
                    name=f"Вакансия {hh_id}",
                    pipeline_status=status,
                    responses_count=responses,
                    raw_short_json=json.dumps(_item(hh_id, responses), ensure_ascii=False) if stored else None,
                    published_at=published,
                    last_seen_at=SEEN,
                )
            )
        for name, text, enabled in (("Python", "python", True), ("Go", "golang", True), ("Java", "java", False)):
            session.add(SavedSearch(name=name, params_json=json.dumps({"text": text}), enabled=enabled))
        await session.commit()


# text of the search → its result pages
PAGES: dict[str, list[list[dict[str, Any]]]] = {
    "python": [
        [_item("101", 1200), _item("102", 900), _item("999", 10)],  # «999» is not in the base: the scan's job
        [_item("103", 30), _item("104", 70)],
    ],
    "golang": [[_item("104", 75), _item("101", 1300), _item("106", 61), _item("107", None)]],
}


def _search_mock(respx_mock: respx.MockRouter) -> respx.Route:
    def answer(request: httpx.Request) -> httpx.Response:
        text = request.url.params.get("text")
        pages = PAGES.get(text or "")
        if pages is None:
            return httpx.Response(400, json={"errors": [{"type": "bad_argument", "value": "text"}]})
        page = int(request.url.params.get("page", "0"))
        return httpx.Response(200, json={"found": 99, "pages": len(pages), "page": page, "items": pages[page]})

    return respx_mock.get(f"{BASE}/vacancies").mock(side_effect=answer)


async def _rows(runtime) -> dict[str, Vacancy]:  # type: ignore[no-untyped-def]
    async with runtime.session_factory() as session:
        return {v.hh_id: v for v in (await session.execute(select(Vacancy))).scalars()}


@respx.mock(assert_all_called=False)
async def test_the_pass_updates_only_the_counter_of_known_vacancies(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    await _seed(runtime)
    route = _search_mock(respx_mock)

    summary = await refresh_responses(runtime)
    assert summary == {"days": 5, "searches": 2, "requests": 3, "items": 9, "updated": 4, "errors": 0}

    # the last five days of each enabled search, the counter asked for, every page; the disabled search is not asked
    asked = [request.url.params for request, _ in route.calls]
    assert [(p["text"], p["page"]) for p in asked] == [("python", "0"), ("python", "1"), ("golang", "0")]
    assert all(
        p["period"] == "5"
        and p["per_page"] == "100"
        and p["responses_count_enabled"] == "true"
        and "date_from" not in p
        for p in asked
    )

    rows = await _rows(runtime)
    assert set(rows) == set(KNOWN)  # nothing new: «999» and «107» are the scan's to take
    counts = {hh_id: row.responses_count for hh_id, row in rows.items()}
    # 101: the first search that returns it wins in a pass; 104: so does its 70; 105: no search returns it
    assert counts == {"101": 1200, "102": 900, "103": 30, "104": 70, "105": 7, "106": 61}
    # the stored search item keeps the new number, so re-processing a vacancy does not bring the old one back
    assert vacancy_data_from_row(rows["101"]).responses_count == 1200
    assert json.loads(rows["101"].raw_short_json or "{}")["name"] == "Вакансия 101"
    assert rows["103"].raw_short_json is None and vacancy_data_from_row(rows["103"]).responses_count is None
    # nothing else of the vacancies changes
    for hh_id, (_responses, status, _stored) in KNOWN.items():
        assert rows[hh_id].pipeline_status == status
        assert as_utc(rows[hh_id].last_seen_at) == SEEN

    async with runtime.session_factory() as session:
        line = (await session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(1))).scalar_one()
    assert (line.level, line.message) == (
        "info",
        "Число откликов обновлено у 4 вакансий: выдача 2 поисков за 5 дн., запросов к hh.ru 3",
    )

    # a second pass with the same answers changes nothing
    assert (await refresh_responses(runtime))["updated"] == 0


@respx.mock(assert_all_called=False)
async def test_a_failing_search_keeps_the_others_and_all_failing_is_a_failed_run(
    api, respx_mock: respx.MockRouter
) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    await _seed(runtime)
    _search_mock(respx_mock)
    async with runtime.session_factory() as session:
        go = (await session.execute(select(SavedSearch).where(SavedSearch.name == "Go"))).scalar_one()
        go.params_json = json.dumps({"text": "unknown"})  # hh.ru answers 400
        await session.commit()

    summary = await refresh_responses(runtime)
    assert (summary["searches"], summary["errors"], summary["updated"]) == (1, 1, 3) and FAILED_KEY not in summary
    async with runtime.session_factory() as session:
        line = (await session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(1))).scalar_one()
    assert line.level == "warning" and "с ошибкой 1: «Go»" in line.message

    async with runtime.session_factory() as session:
        for search in (await session.execute(select(SavedSearch))).scalars():
            search.params_json = json.dumps({"text": "unknown"})
        await session.commit()
    summary = await refresh_responses(runtime)
    assert summary["searches"] == 0 and summary[FAILED_KEY].startswith("все поиски (2) завершились ошибкой")


@respx.mock(assert_all_called=False)
async def test_a_captcha_stops_the_pass_and_pauses_the_panel(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    await _seed(runtime)
    route = respx_mock.get(f"{BASE}/vacancies").mock(
        return_value=httpx.Response(
            403,
            json={
                "errors": [
                    {
                        "type": "captcha_required",
                        "value": "captcha_required",
                        "captcha_url": "https://hh.ru/account/captcha?state=1",
                    }
                ]
            },
        )
    )
    summary = await refresh_responses(runtime)
    assert route.call_count == 1 and summary["updated"] == 0  # the second search is not asked
    assert summary[FAILED_KEY].startswith("обновление числа откликов остановлено")
    async with runtime.session_factory() as session:
        assert (await get_system_state(session)).paused_reason == "captcha"
    # while paused hh.ru is not asked at all
    summary = await refresh_responses(runtime)
    assert summary["skipped"] == "paused: captcha" and route.call_count == 1


@respx.mock(assert_all_called=False)
async def test_a_pause_by_the_user_keeps_hh_unasked(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    await _seed(runtime)
    route = _search_mock(respx_mock)
    async with runtime.session_factory() as session:
        await set_pause(session, "user")
        await session.commit()
    assert (await refresh_responses(runtime))["skipped"] == "paused: user" and route.call_count == 0


@respx.mock(assert_all_called=False)
async def test_the_tick_runs_a_pass_every_few_hours_and_the_button_at_once(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _seed(runtime)
    route = _search_mock(respx_mock)
    async with runtime.session_factory() as session:
        assert await refresh_due(session, 3)  # no pass yet

    # not signed in to hh.ru: neither the tick nor the button asks it
    await responses_tick(runtime)
    assert not runtime.jobs.is_running(JOB) and route.call_count == 0
    assert (await client.post("/api/jobs/responses")).status_code == 400

    await _login(client, respx_mock)
    await responses_tick(runtime)
    await runtime.jobs.wait(JOB)
    assert route.call_count == 3
    async with runtime.session_factory() as session:
        run = (await session.execute(select(JobRun).where(JobRun.job == JOB))).scalar_one()
        assert run.status == "ok" and json.loads(run.stats_json or "{}")["updated"] == 4
        now = as_utc(run.started_at)
        assert now is not None
        assert not await refresh_due(session, 3, now + timedelta(hours=2, minutes=59))
        assert await refresh_due(session, 3, now + timedelta(hours=3))
    # within the interval the tick does nothing
    await responses_tick(runtime)
    assert not runtime.jobs.is_running(JOB) and route.call_count == 3

    # the button of «Журнал» runs a pass right away
    r = await client.post("/api/jobs/responses")
    assert r.status_code == 200 and r.json()["job_run_id"]
    await runtime.jobs.wait(JOB)
    assert route.call_count == 6
    async with runtime.session_factory() as session:
        assert (await session.execute(select(func.count(JobRun.id)).where(JobRun.job == JOB))).scalar() == 2


async def test_the_scheduler_has_the_tick_unless_it_is_turned_off(api) -> None:
    _client, runtime = api
    tick = next(j for j in build_scheduler(runtime).get_jobs() if j.id == "responses_refresh")
    assert tick.trigger.interval == timedelta(minutes=15)
    runtime.settings = runtime.settings.model_copy(update={"responses_refresh_hours": 0})
    assert "responses_refresh" not in {j.id for j in build_scheduler(runtime).get_jobs()}


async def _hold(runtime, job: str) -> asyncio.Event:  # type: ignore[no-untyped-def]
    """Starts a job of the group that runs until the returned event is set (a scan that takes its time)."""
    release = asyncio.Event()

    async def body() -> dict[str, Any]:
        await release.wait()
        return {}

    await runtime.jobs.start(job, body)
    return release


@respx.mock(assert_all_called=False)
async def test_the_tick_waits_for_the_scan_it_falls_on(api, respx_mock: respx.MockRouter, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The tick of the counter and the tick of the scan count from the start of the service (15 = 3 × 5) and fall on
    the same second; a tick that tried once always lost to the scan, and the pass never ran."""
    client, runtime = api
    await _login(client, respx_mock)
    await _seed(runtime)
    route = _search_mock(respx_mock)
    monkeypatch.setattr(scheduler_module, "RESPONSES_RETRY_SEC", 0.01)
    release = await _hold(runtime, "scan")

    tick = asyncio.create_task(responses_tick(runtime))
    await asyncio.sleep(0.1)
    assert not tick.done() and not runtime.jobs.is_running(JOB) and route.call_count == 0  # waits, does not give up
    release.set()
    await runtime.jobs.wait("scan")
    await asyncio.wait_for(tick, 5)
    await runtime.jobs.wait(JOB)
    assert route.call_count == 3
    async with runtime.session_factory() as session:
        run = (await session.execute(select(JobRun).where(JobRun.job == JOB))).scalar_one()
        assert run.status == "ok"


@respx.mock(assert_all_called=False)
async def test_the_tick_gives_up_when_the_group_stays_busy(api, respx_mock: respx.MockRouter, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client, runtime = api
    await _login(client, respx_mock)
    monkeypatch.setattr(scheduler_module, "RESPONSES_RETRY_SEC", 0.01)
    monkeypatch.setattr(scheduler_module, "RESPONSES_WAIT_SEC", 0.05)
    release = await _hold(runtime, "reprocess")
    await asyncio.wait_for(responses_tick(runtime), 5)  # the re-evaluation outlasts the wait: the next tick tries again
    assert not runtime.jobs.is_running(JOB)
    async with runtime.session_factory() as session:
        assert (await session.execute(select(func.count(JobRun.id)).where(JobRun.job == JOB))).scalar() == 0
    release.set()
    await runtime.jobs.wait("reprocess")


@respx.mock(assert_all_called=False)
async def test_the_tick_does_not_wait_for_a_pass_of_its_own(api, respx_mock: respx.MockRouter) -> None:
    """A pass started more than RESPONSES_REFRESH_HOURS ago is still running (hh.ru slow): the tick is due, but it
    neither waits the ten minutes for it nor starts another."""
    client, runtime = api
    await _login(client, respx_mock)
    release = await _hold(runtime, JOB)
    async with runtime.session_factory() as session:
        run = (await session.execute(select(JobRun).where(JobRun.job == JOB))).scalar_one()
        run.started_at = utcnow() - timedelta(hours=4)
        await session.commit()
    await asyncio.wait_for(responses_tick(runtime), 1)  # at once, though RESPONSES_WAIT_SEC is ten minutes
    release.set()
    await runtime.jobs.wait(JOB)
    async with runtime.session_factory() as session:
        assert (await session.execute(select(func.count(JobRun.id)).where(JobRun.job == JOB))).scalar() == 1
