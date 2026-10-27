"""Найденные при аудите дефекты, записанные тестами на ожидаемое поведение.

Каждый тест был помечен `xfail(strict=True)`: падал, пока дефект был, после исправления начал проходить и pytest
сообщил XPASS(strict) — маркер снят (строка «# исправлено …» над тестом), тест остаётся регрессионным.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import respx
from httpx import ASGITransport
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings
from app.db.models import Application, CoverLetter, Vacancy
from app.main import app
from app.runtime import Runtime
from app.scheduler import build_scheduler
from app.services import applier
from tests.integration.test_api_m2 import DICTS, _items, _login
from tests.integration.test_api_m4 import _consent, _seed

BASE = "https://api.hh.ru"


@pytest.fixture
async def api(settings: Settings):
    settings = settings.model_copy(
        update={
            "apply_min_delay_sec": 0,
            "apply_max_delay_sec": 0,
            "apply_window": "00:00-23:59",
            "apply_max_per_employer_7d": 1,
            "batch_auto_create": False,
            "apply_dry_run": True,
        }
    )
    runtime = await Runtime.create(settings)
    app.state.runtime = runtime
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


async def _enqueue_without_running(runtime: Runtime, vacancy_ids: list[int]) -> list[int]:
    async with runtime.session_factory() as session:
        result = await applier.enqueue(session, runtime.settings, vacancy_ids)
        await session.commit()
    return list(result["queued"])


# исправлено сессией «_a» 2026-09-23: skip/hide/hide_employer отменяют queued-отклик
@respx.mock(assert_all_called=False)
async def test_skipping_a_queued_vacancy_cancels_its_application(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    ids = await _seed(client, runtime, respx_mock, ["1"])
    await _consent(client)
    assert await _enqueue_without_running(runtime, [ids["1"]]) == [ids["1"]]
    r = await client.post(f"/api/vacancies/{ids['1']}/skip", json={"reason": "передумал"})
    assert r.status_code == 200 and r.json()["pipeline_status"] == "skipped"
    summary = await applier.run_apply(runtime)
    assert summary["sent"] == 0, summary
    row = (await client.get("/api/applications")).json()["items"][0]
    assert row["status"] == "cancelled", row["status"]
    assert (await client.get(f"/api/vacancies/{ids['1']}")).json()["pipeline_status"] == "skipped"


# исправлено сессией «_a» 2026-09-23: to-batch при открытом отклике → 409
@respx.mock(assert_all_called=False)
async def test_to_batch_refuses_a_sent_vacancy(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    ids = await _seed(client, runtime, respx_mock, ["1"])
    await _consent(client)
    await _enqueue_without_running(runtime, [ids["1"]])
    await applier.run_apply(runtime)
    assert (await client.get(f"/api/vacancies/{ids['1']}")).json()["pipeline_status"] == "sent"
    r = await client.post(f"/api/vacancies/{ids['1']}/to-batch")
    assert r.status_code == 409, (r.status_code, r.json().get("pipeline_status"))
    assert (await client.get(f"/api/vacancies/{ids['1']}")).json()["pipeline_status"] == "sent"


# исправлено сессией «_a» 2026-09-23 (fix/input-validation-and-parsing): сухие прогоны не считаются в лимите на работодателя
@respx.mock(assert_all_called=False)
async def test_employer_limit_ignores_dry_runs(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    ids = await _seed(client, runtime, respx_mock, ["1", "2"])  # один работодатель в фикстуре
    await _consent(client)
    await _enqueue_without_running(runtime, [ids["1"]])
    await applier.run_apply(runtime)  # сухой прогон → sent, dry_run=True
    assert (await client.get("/api/applications/status")).json()["limits"]["today_sent"] == 0
    r = await client.post("/api/applications/check", json={"vacancy_ids": [ids["2"]]})
    item = r.json()["items"][0]
    assert item["ok"] is True, item["problems"]


# исправлено сессией «_a» 2026-09-23 (fix/input-validation-and-parsing): parse_window терпит «25:00-…»
def test_in_window_tolerates_an_invalid_window() -> None:
    assert applier.in_window(datetime.now(UTC), "25:00-21:00") in (True, False)


# исправлено сессией «_a» 2026-09-23 (fix/input-validation-and-parsing): неверная дата → 400
async def test_applications_list_rejects_a_bad_date_gracefully(api) -> None:
    client, _ = api
    r = await client.get("/api/applications", params={"date_from": "2026-13-45"})
    assert r.status_code in (400, 422), r.status_code


# исправлено сессией «_a» (d77959b): misfire_grace_time 3 ч / 20 ч — маркер снят, тест регрессионный
def test_cron_jobs_survive_a_late_wakeup(settings: Settings) -> None:
    scheduler = build_scheduler(SimpleNamespace(settings=settings))  # type: ignore[arg-type]
    jobs = {j.id: j for j in scheduler.get_jobs()}
    for job_id in ("daily_summary", "backup_daily"):
        grace = jobs[job_id].misfire_grace_time
        assert grace is not None and grace >= 60, f"{job_id}: misfire_grace_time={grace}"


# исправлено сессией «_a» 2026-09-23 (fix/input-validation-and-parsing): автопилот берёт только вакансии с готовым письмом
@respx.mock(assert_all_called=False)
async def test_autopilot_requires_a_letter(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    runtime.settings = runtime.settings.model_copy(update={"apply_autopilot": True})
    ids = await _seed(client, runtime, respx_mock, ["1"], response_letter_required=False)
    await _consent(client)
    async with runtime.session_factory() as session:
        for letter in (await session.execute(select(CoverLetter))).scalars():
            await session.delete(letter)
        v = await session.get(Vacancy, ids["1"])
        v.pipeline_status, v.score, v.response_letter_required = "suitable", 95, False
        await session.commit()
    async with runtime.session_factory() as session:
        queued = await applier.autopilot_enqueue(session, runtime.settings)
        await session.commit()
    assert queued == [], queued


# исправлено сессией «_a» 2026-09-23: batch_id проверяется по ^\d{8}_\d{4}_[0-9a-f]{4}$
async def test_import_text_rejects_batch_id_with_path_separators(api) -> None:
    client, runtime = api
    payload = {"protocol_version": 1, "batch_id": "../../../../traversal_b", "profile_version": 1, "results": []}
    r = await client.post("/api/batches/import-text", json={"text": json.dumps(payload)})
    assert r.status_code == 400, (r.status_code, r.text[:200])
    stray = [p for p in runtime.settings.data_dir.rglob("*traversal_b*")]
    assert stray == [], stray


# исправлено сессией «_a» 2026-09-23 (fix/input-validation-and-parsing): Settings проверяет окно, пороги, время, перечисления
def test_settings_reject_impossible_values() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, apply_window="25:00-21:00")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, score_threshold_maybe=80, score_threshold_suitable=70)  # type: ignore[call-arg]


# исправлено сессией «_a» (d77959b): скан, прерванный капчей, записывается ошибкой — маркер снят
@respx.mock(assert_all_called=False)
async def test_scan_aborted_by_captcha_is_not_reported_as_ok(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    respx_mock.get(f"{BASE}/dictionaries").mock(return_value=httpx.Response(200, json=DICTS))
    respx_mock.get(f"{BASE}/industries").mock(return_value=httpx.Response(200, json=[]))
    r = await client.post("/api/searches", json={"name": "S", "params": {"text": "python"}, "interval_min": 30})
    sid = r.json()["id"]
    respx_mock.get(f"{BASE}/vacancies").mock(
        return_value=httpx.Response(
            403,
            json={"errors": [{"type": "captcha_required", "captcha_url": "https://hh.ru/account/captcha?state=1"}]},
        )
    )
    await client.post(f"/api/searches/{sid}/run")
    await runtime.jobs.wait("scan")
    assert (await client.get("/api/settings")).json()["paused_reason"] == "captcha"
    run = (await client.get("/api/jobs")).json()["runs"][0]
    assert run["job"] == "scan" and run["status"] != "ok", run


# исправлено сессией «_a» 2026-09-23 (fix/input-validation-and-parsing): близнец из поиска-наблюдения не делает дублем
@respx.mock(assert_all_called=False)
async def test_repost_of_a_watched_vacancy_is_evaluated(api, respx_mock: respx.MockRouter) -> None:
    from app.services.scanner import find_duplicate

    client, runtime = api
    ids = await _seed(client, runtime, respx_mock, ["1"])
    async with runtime.session_factory() as session:
        twin = await session.get(Vacancy, ids["1"])
        twin.pipeline_status = "watched"
        new = Vacancy(hh_id="2", name=twin.name, employer_id=twin.employer_id, employer_name=twin.employer_name)
        session.add(new)
        await session.flush()
        assert await find_duplicate(session, new) is None
        await session.rollback()
    _ = _items, Application
