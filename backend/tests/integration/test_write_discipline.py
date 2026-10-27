"""SQLite has one writer. Background jobs must not keep a write transaction open while they wait for
hh.ru — otherwise every write of the UI waits up to 30 s and fails with «database is locked»; the jobs that rewrite
vacancies (scan, reprocess, freshness, sync) never run together, whoever starts them."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select

from app.config import Settings
from app.db.base import utcnow
from app.db.models import SavedSearch, Vacancy
from app.main import app
from app.runtime import Runtime
from app.scheduler import scan_tick
from app.services.freshness import check_closed
from app.services.jobs import JobBusy
from app.services.scanner import _json, reprocess_after_criteria_change, reprocess_all, run_scan
from tests.integration.test_api_m2 import _full, _items
from tests.integration.test_api_m3 import _login

# a «slow» hh request lasts until the test's UI write is done (or this long): with no write lock held across the request
# the UI write finishes while the request still hangs; with the lock the two wait for each other until the timeout
HANG_TIMEOUT = 20.0


@pytest.fixture
async def api(settings: Settings):
    runtime = await Runtime.create(settings.model_copy(update={"batch_auto_create": False}))
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


class SlowHH:
    """Stands in for HHClient.public: search pages and vacancy cards; the `slow` ones hang until `release`."""

    def __init__(self, ids: list[str], *, slow: set[str], pages: int = 1, archived: set[str] | None = None) -> None:
        self.ids = ids
        self.slow = slow
        self.pages = pages
        self.archived = archived or set()
        self.in_flight = asyncio.Event()
        self.release = asyncio.Event()
        self.busy = 0  # slow requests in progress right now
        self.requests_made = 0

    async def _slow(self) -> None:
        self.in_flight.set()
        self.busy += 1
        try:
            await asyncio.wait_for(self.release.wait(), timeout=HANG_TIMEOUT)
        except TimeoutError:
            pass
        finally:
            self.busy -= 1

    def arm(self, slow: set[str]) -> None:
        """Next slow request (called synchronously right after a measurement, before the job resumes)."""
        self.slow = slow
        self.in_flight = asyncio.Event()
        self.release = asyncio.Event()

    async def public(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.requests_made += 1
        if path == "/vacancies":
            page = int((params or {}).get("page", 0))
            if f"page{page}" in self.slow:
                await self._slow()
            chunk = self.ids[page :: self.pages]
            return {"found": len(self.ids), "pages": self.pages, "items": _items(chunk)}
        if path.startswith("/vacancies/"):
            hh_id = path.rsplit("/", 1)[1]
            if hh_id in self.slow:
                await self._slow()
            return _full(hh_id, archived=hh_id in self.archived)
        raise RuntimeError(f"not in this test: {path}")  # dictionaries: the scanner works without them


async def _ui_write_while_in_flight(client: httpx.AsyncClient, hh: SlowHH, vacancy_id: int) -> tuple[bool, float]:
    """A UI write started while the job hangs in an hh request. Returns (finished while the request still hung, seconds):
    with a write lock held across the request the write can only finish after the job commits, i.e. after the request
    gave up at HANG_TIMEOUT — independent of how loaded the machine is. Releases the request afterwards."""
    await asyncio.wait_for(hh.in_flight.wait(), timeout=HANG_TIMEOUT)
    started = time.perf_counter()
    r = await client.post(f"/api/vacancies/{vacancy_id}/snooze", json={"days": 1})
    elapsed = time.perf_counter() - started
    in_time = hh.busy > 0
    hh.release.set()
    assert r.status_code == 200, r.text
    return in_time, elapsed


async def _add(runtime: Runtime, hh_id: str, status: str, **fields: Any) -> int:
    async with runtime.session_factory() as session:
        row = Vacancy(
            hh_id=hh_id,
            name=f"Python developer {hh_id}",
            employer_id="1455",
            first_seen_at=utcnow(),
            last_seen_at=fields.pop("last_seen_at", utcnow()),
            pipeline_status=status,
            source_search_ids=",",
            raw_short_json=_json(_items([hh_id])[0]),
            prescore=50,
            **fields,
        )
        session.add(row)
        await session.commit()
        return row.id


# ---- no write lock across hh requests -----------------------------------------------------------------------------


async def test_reprocess_does_not_lock_the_ui_while_fetching(api, monkeypatch) -> None:
    client, runtime = api
    first = await _add(runtime, "9001", "prescored")
    await _add(runtime, "9002", "prescored")
    other = await _add(runtime, "9100", "skipped")
    hh = SlowHH(["9001", "9002"], slow={"9001", "9002"})
    monkeypatch.setattr(runtime, "hh_client", lambda: hh)
    await runtime.jobs.start("reprocess", lambda: reprocess_all(runtime))
    in_time, elapsed = await _ui_write_while_in_flight(client, hh, other)
    await runtime.jobs.wait("reprocess")
    assert in_time, f"the UI write waited {elapsed:.1f} s — until the job's hh request was over"
    async with runtime.session_factory() as session:
        assert (await session.get(Vacancy, first)).raw_full_json  # the job did its work


async def test_scan_does_not_lock_the_ui_between_pages_and_cards(api, monkeypatch) -> None:
    client, runtime = api
    async with runtime.session_factory() as session:
        session.add(SavedSearch(name="S", params_json='{"text": "python"}', interval_min=30))
        await session.commit()
    other = await _add(runtime, "9200", "skipped")
    # page 2 of the search is slow (page 1 is already upserted), then every card fetch is slow
    hh = SlowHH(["9201", "9202", "9203", "9204"], slow={"page1"}, pages=2)
    monkeypatch.setattr(runtime, "hh_client", lambda: hh)
    await runtime.jobs.start("scan", lambda: run_scan(runtime))
    page_in_time, elapsed_page = await _ui_write_while_in_flight(client, hh, other)
    hh.arm({"9203"})
    card_in_time, elapsed_card = await _ui_write_while_in_flight(client, hh, other)
    await runtime.jobs.wait("scan")
    assert page_in_time, f"UI write waited {elapsed_page:.1f} s — until the search page request was over"
    assert card_in_time, f"UI write waited {elapsed_card:.1f} s — until the vacancy card request was over"


async def test_freshness_does_not_lock_the_ui(api, monkeypatch) -> None:
    client, runtime = api
    stale = utcnow() - timedelta(days=5)
    await _add(runtime, "9301", "suitable", last_seen_at=stale)  # closed on hh → written before the next request
    await _add(runtime, "9302", "suitable", last_seen_at=stale + timedelta(minutes=1))
    other = await _add(runtime, "9300", "skipped")
    hh = SlowHH(["9301", "9302"], slow={"9302"}, archived={"9301"})
    monkeypatch.setattr(runtime, "hh_client", lambda: hh)
    await runtime.jobs.start("freshness", lambda: check_closed(runtime))
    in_time, elapsed = await _ui_write_while_in_flight(client, hh, other)
    await runtime.jobs.wait("freshness")
    assert in_time, f"the UI write waited {elapsed:.1f} s — until the freshness check's hh request was over"
    async with runtime.session_factory() as session:
        closed = (await session.execute(select(Vacancy).where(Vacancy.hh_id == "9301"))).scalar_one()
        assert closed.pipeline_status == "filtered_out" and closed.archived


# ---- the vacancy-rewriting jobs never overlap -------------------------------------------------------------------


@respx.mock(assert_all_called=False)
async def test_scan_buttons_wait_for_a_running_reprocess(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    async with runtime.session_factory() as session:
        search = SavedSearch(name="S", params_json="{}", interval_min=30)
        session.add(search)
        await session.commit()
        search_id = search.id
    release = asyncio.Event()

    async def long_reprocess() -> dict[str, Any]:
        await release.wait()
        return {}

    await runtime.jobs.start("reprocess", long_reprocess)
    for method, path in (
        ("POST", "/api/jobs/scan"),
        ("POST", f"/api/searches/{search_id}/run"),
        ("POST", "/api/jobs/freshness"),
        ("POST", "/api/vacancies/add"),
    ):
        r = await client.request(method, path, json={"text": "https://hh.ru/vacancy/123456"} if "add" in path else None)
        assert r.status_code == 409 and "переоценка" in r.json()["detail"], (path, r.text)
    await scan_tick(runtime)  # the scheduler skips quietly
    assert not runtime.jobs.is_running("scan")
    assert await reprocess_after_criteria_change(runtime) is False
    release.set()
    await runtime.jobs.wait("reprocess")
    r = await client.post("/api/vacancies/reprocess-all", json={})
    assert r.status_code == 200  # free again
    await runtime.jobs.wait("reprocess")


async def test_job_runner_group_and_reservation(api) -> None:
    _client, runtime = api
    jobs = runtime.jobs
    gate = asyncio.Event()

    async def wait_gate() -> dict[str, Any]:
        await gate.wait()
        return {}

    # two starts racing: the second one sees the first even before its task exists
    results = await asyncio.gather(
        jobs.start("scan", wait_gate), jobs.start("freshness", wait_gate), return_exceptions=True
    )
    assert sum(isinstance(r, int) for r in results) == 1
    busy = next(r for r in results if isinstance(r, JobBusy))
    assert "сканирование" in str(busy) and busy.running == "scan"
    with pytest.raises(JobBusy, match="Уже выполняется: сканирование"):
        await jobs.start("scan", wait_gate)
    assert jobs.running_exclusive() == "scan" and jobs.conflicting("sync") == "scan"
    await jobs.start("channel_sync", wait_gate)  # outside the group: runs alongside
    gate.set()
    await jobs.wait("scan")
    await jobs.wait("channel_sync")
    assert jobs.conflicting("sync") is None and jobs.running_exclusive() is None
