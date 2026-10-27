"""Applications: consent, queue validation, dry run, API executor against mocked hh, assist mode, limits."""

from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select

from app.config import Settings
from app.db.base import utcnow
from app.db.models import Application, Vacancy
from app.main import app
from app.runtime import Runtime
from app.services.applier import local_day_bounds, run_apply
from app.services.state import get_system_state
from tests.integration.test_api_m2 import DICTS, _full, _items

BASE = "https://api.hh.ru"
RID = "0123456789abcdef0123456789abcdef012345"


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
        }
    )
    runtime = await Runtime.create(settings)
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


async def _login(client: httpx.AsyncClient, m: respx.MockRouter) -> None:
    m.post(f"{BASE}/token").mock(
        return_value=httpx.Response(200, json={"access_token": "acc", "refresh_token": "ref", "expires_in": 100000})
    )
    m.get(f"{BASE}/me").mock(return_value=httpx.Response(200, json={"id": "1", "is_applicant": True}))
    r = await client.get("/api/auth/hh/login")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    await client.get("/api/auth/hh/callback", params={"code": "c", "state": state})


async def _seed(
    client: httpx.AsyncClient, runtime: Runtime, m: respx.MockRouter, ids: list[str], **full_overrides
) -> dict[str, int]:
    """Login, profile file, resume id, one scanned search → vacancies `prescored`, then a letter for each."""
    await _login(client, m)
    r = await client.post(
        "/api/profile/resumes/upload", files={"file": ("cv.txt", "Иван, Python".encode(), "text/plain")}
    )
    assert r.status_code == 200
    await client.put("/api/settings", json={"apply_mode_override": "api", "resume_id": RID})
    m.get(f"{BASE}/dictionaries").mock(return_value=httpx.Response(200, json=DICTS))
    m.get(f"{BASE}/industries").mock(return_value=httpx.Response(200, json=[]))
    r = await client.post("/api/searches", json={"name": "S", "params": {"text": "python"}, "interval_min": 30})
    sid = r.json()["id"]
    m.get(f"{BASE}/vacancies").mock(
        return_value=httpx.Response(200, json={"found": len(ids), "pages": 1, "items": _items(ids)})
    )
    for i in ids:
        m.get(f"{BASE}/vacancies/{i}").mock(return_value=httpx.Response(200, json=_full(i, **full_overrides)))
    await client.post(f"/api/searches/{sid}/run")
    await runtime.jobs.wait("scan")
    rows = (await client.get("/api/vacancies", params={"group": "prescored"})).json()["items"]
    ids_map = {v["hh_id"]: v["id"] for v in rows}
    for vid in ids_map.values():
        await client.put(
            f"/api/vacancies/{vid}/letter", json={"text": "Здравствуйте! Хорошее письмо без ссылок и маркеров." * 5}
        )
    return ids_map


async def _consent(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/applications/consent")
    assert r.status_code == 200 and r.json()["consent_accepted_at"]


def _mock_post(m: respx.MockRouter, responses: list[httpx.Response]) -> respx.Route:
    return m.post(f"{BASE}/negotiations").mock(side_effect=responses)


@respx.mock(assert_all_called=False)
async def test_consent_mode_and_validation(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    ids = await _seed(client, runtime, respx_mock, ["1", "2"])
    # consent required
    r = await client.post("/api/applications/queue", json={"vacancy_ids": [ids["1"]]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "consent_required"
    status = (await client.get("/api/applications/status")).json()
    assert status["apply_mode"] == "api" and status["consent_accepted_at"] is None and status["resume_id"] == RID
    assert status["limits"]["remaining_day"] == 5 and status["limits"]["can_send"] is True
    await _consent(client)

    # validation: no letter for a letter-required vacancy, test required, direct, already queued
    v1 = ids["1"]
    r = await client.post("/api/applications/check", json={"vacancy_ids": [v1]})
    assert r.json()["items"][0]["ok"] is True
    async with runtime.session_factory() as session:
        row = await session.get(Vacancy, ids["2"])
        row.test_required = True
        await session.commit()
    r = await client.post("/api/applications/check", json={"vacancy_ids": [ids["2"]]})
    assert "тест" in r.json()["items"][0]["problems"][0]
    r = await client.post("/api/applications/check", json={"vacancy_ids": [ids["2"]], "mode": "assist"})
    assert r.json()["items"][0]["ok"] is True  # assist mode allows tests (done on hh.ru)

    # assist mode refuses queue
    await client.put("/api/settings", json={"apply_mode_override": "assist"})
    r = await client.post("/api/applications/queue", json={"vacancy_ids": [v1]})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "mode"
    await client.put("/api/settings", json={"apply_mode_override": "api"})


@respx.mock(assert_all_called=False)
async def test_dry_run_queue(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    runtime.settings = runtime.settings.model_copy(update={"apply_dry_run": True})
    ids = await _seed(client, runtime, respx_mock, ["1", "2"])
    await _consent(client)
    r = await client.post("/api/applications/queue", json={"vacancy_ids": [ids["1"], ids["2"], 999]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["queued"]) == sorted([ids["1"], ids["2"]]) and body["rejected"][0]["vacancy_id"] == 999
    assert body["job_started"] is True
    await runtime.jobs.wait("apply")
    listing = (await client.get("/api/applications")).json()
    assert listing["total"] == 2 and all(a["status"] == "sent" and a["dry_run"] for a in listing["items"])
    assert all(a["hh_negotiation_id"] is None for a in listing["items"])
    assert not any(c.request.url.path == "/negotiations" for c in respx_mock.calls)  # nothing sent to hh
    assert (await client.get("/api/vacancies", params={"group": "sent"})).json()["total"] == 2
    # dry-run sends do not count against limits
    assert (await client.get("/api/applications/status")).json()["limits"]["today_sent"] == 0
    # second queue attempt for the same vacancy is rejected
    r = await client.post("/api/applications/queue", json={"vacancy_ids": [ids["1"]]})
    assert r.json()["queued"] == [] and "уже есть отклик" in r.json()["rejected"][0]["reason"]


@respx.mock(assert_all_called=False)
async def test_api_send_outcomes(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    ids = await _seed(client, runtime, respx_mock, ["1", "2", "3", "4", "5"])
    await _consent(client)
    route = _mock_post(
        respx_mock,
        [
            httpx.Response(201, headers={"Location": "/negotiations/777"}),
            httpx.Response(303, headers={"Location": "https://employer.example/respond"}),
            httpx.Response(403, json={"errors": [{"type": "negotiations", "value": "already_applied"}]}),
            httpx.Response(403, json={"errors": [{"type": "negotiations", "value": "test_required"}]}),
            httpx.Response(
                400, json={"errors": [{"type": "bad_argument", "value": "required_applicant_visibility_id"}]}
            ),
        ],
    )
    r = await client.post("/api/applications/queue", json={"vacancy_ids": [ids[i] for i in ("1", "2", "3", "4", "5")]})
    assert len(r.json()["queued"]) == 5
    await runtime.jobs.wait("apply")
    assert route.call_count == 5
    req = route.calls[0].request
    assert req.headers["Content-Type"].startswith("multipart/form-data")
    body = req.content.decode()
    assert 'name="vacancy_id"' in body and 'name="resume_id"' in body and RID in body and 'name="message"' in body

    apps = {a["vacancy"]["hh_id"]: a for a in (await client.get("/api/applications")).json()["items"]}
    assert apps["1"]["status"] == "sent" and apps["1"]["hh_negotiation_id"] == "777"
    assert apps["1"]["negotiation_url"].endswith("id=777") and apps["1"]["mode"] == "api"
    assert apps["2"]["status"] == "needs_manual" and apps["2"]["response_url"] == "https://employer.example/respond"
    assert apps["3"]["status"] == "sent" and apps["3"]["mode"] == "external"
    assert apps["4"]["status"] == "needs_manual" and "тест" in apps["4"]["error_text"]
    assert (
        apps["5"]["status"] == "failed"
        and "visibility" in apps["5"]["error_value"]
        and "видимости резюме" in apps["5"]["error_text"]
    )

    statuses = {
        v["hh_id"]: v["pipeline_status"]
        for v in (await client.get("/api/vacancies", params={"group": "all"})).json()["items"]
    }
    assert statuses["1"] == "sent" and statuses["2"] == "needs_manual" and statuses["3"] == "already_applied"
    assert statuses["4"] == "needs_manual"
    limits = (await client.get("/api/applications/status")).json()["limits"]
    assert limits["today_sent"] == 2 and limits["remaining_day"] == 3  # api + external count

    # retry the failed one -> queued again and sent
    _mock_post(respx_mock, [httpx.Response(201, headers={"Location": "/negotiations/778"})])
    r = await client.post(f"/api/applications/{apps['5']['id']}/retry")
    assert r.json()["status"] == "queued"
    await runtime.jobs.wait("apply")
    assert (await client.get("/api/applications", params={"status": "sent"})).json()["total"] == 3

    # cancel a needs_manual application restores the vacancy status
    r = await client.post(f"/api/applications/{apps['4']['id']}/cancel")
    assert r.json()["status"] == "cancelled"
    assert (await client.get(f"/api/vacancies/{ids['4']}")).json()["pipeline_status"] == "prescored"
    # withdraw a sent one
    respx_mock.delete(f"{BASE}/negotiations/active/777").mock(return_value=httpx.Response(204))
    r = await client.post(f"/api/applications/{apps['1']['id']}/withdraw")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert (await client.get(f"/api/vacancies/{ids['1']}")).json()["pipeline_status"] == "skipped"


@respx.mock(assert_all_called=False)
async def test_limit_exceeded_pauses_queue(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    ids = await _seed(client, runtime, respx_mock, ["1", "2"])
    await _consent(client)
    route = _mock_post(
        respx_mock, [httpx.Response(403, json={"errors": [{"type": "negotiations", "value": "limit_exceeded"}]})]
    )
    await client.post("/api/applications/queue", json={"vacancy_ids": [ids["1"], ids["2"]]})
    await runtime.jobs.wait("apply")
    assert route.call_count == 1
    status = (await client.get("/api/applications/status")).json()
    assert status["limits"]["paused_until"] and "лимит" in status["limits"]["paused_reason"]
    assert status["limits"]["can_send"] is False
    listing = (await client.get("/api/applications")).json()
    assert all(a["status"] == "queued" for a in listing["items"])
    # run again: nothing sent while paused
    await client.post("/api/applications/run")
    await runtime.jobs.wait("apply")
    assert route.call_count == 1
    run = (await client.get("/api/jobs")).json()["runs"][0]
    assert "приостановлены" in run["stats"]["stopped"]


@respx.mock(assert_all_called=False)
async def test_transient_errors_and_captcha(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    ids = await _seed(client, runtime, respx_mock, ["1", "2"])
    await _consent(client)
    # 502 x5 (client retries 4 times per call) -> one attempt consumed; after 3 attempts -> failed
    route = _mock_post(respx_mock, [httpx.Response(502)] * 15)
    await client.post("/api/applications/queue", json={"vacancy_ids": [ids["1"]]})
    await runtime.jobs.wait("apply")
    row = (await client.get("/api/applications")).json()["items"][0]
    assert row["status"] == "queued" and row["attempts"] == 1
    await client.post("/api/applications/run")
    await runtime.jobs.wait("apply")
    await client.post("/api/applications/run")
    await runtime.jobs.wait("apply")
    row = (await client.get("/api/applications")).json()["items"][0]
    assert row["status"] == "failed" and row["attempts"] == 3
    assert route.call_count == 15

    # captcha stops the loop and pauses the panel
    _mock_post(
        respx_mock,
        [
            httpx.Response(
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
        ],
    )
    await client.post("/api/applications/queue", json={"vacancy_ids": [ids["2"]]})
    await runtime.jobs.wait("apply")
    assert (await client.get("/api/settings")).json()["paused_reason"] == "captcha"
    row = next(a for a in (await client.get("/api/applications")).json()["items"] if a["vacancy"]["hh_id"] == "2")
    assert row["status"] == "queued"


@respx.mock(assert_all_called=False)
async def test_window_and_limits_block(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    ids = await _seed(client, runtime, respx_mock, ["1", "2", "3"])
    await _consent(client)
    # pretend 5 applications were sent today (limit 5): right after the local midnight, so that the test does not
    # depend on the hour it runs at («now minus a few minutes» fell on yesterday just after midnight)
    day_start, _ = local_day_bounds(runtime.settings)
    async with runtime.session_factory() as session:
        for i in range(5):
            session.add(
                Application(
                    vacancy_id=ids["3"], mode="external", status="sent", sent_at=day_start + timedelta(seconds=1 + i)
                )
            )
        await session.commit()
    status = (await client.get("/api/applications/status")).json()["limits"]
    assert (
        status["today_sent"] == 5 and status["remaining_day"] == 0 and "дневной лимит" in status["blocked_reasons"][0]
    )
    route = _mock_post(respx_mock, [httpx.Response(201, headers={"Location": "/negotiations/1"})])
    await client.post("/api/applications/queue", json={"vacancy_ids": [ids["1"]]})
    await runtime.jobs.wait("apply")
    assert route.call_count == 0
    # outside the window
    later = utcnow().astimezone(ZoneInfo(runtime.settings.timezone)) + timedelta(hours=2)  # never contains «now»
    window = f"{later:%H:%M}-{later + timedelta(minutes=1):%H:%M}"
    runtime.settings = runtime.settings.model_copy(update={"apply_window": window})
    async with runtime.session_factory() as session:
        await session.execute(Application.__table__.delete().where(Application.mode == "external"))
        await session.commit()
    await client.post("/api/applications/run")
    await runtime.jobs.wait("apply")
    assert route.call_count == 0
    assert "вне окна" in (await client.get("/api/jobs")).json()["runs"][0]["stats"]["stopped"]


@respx.mock(assert_all_called=False)
async def test_assist_mode(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    ids = await _seed(client, runtime, respx_mock, ["1", "2"])
    await client.put("/api/settings", json={"apply_mode_override": "assist"})
    r = await client.post("/api/applications/assist/open", json={"vacancy_id": ids["1"]})
    assert r.status_code == 409  # consent first
    await _consent(client)
    r = await client.post("/api/applications/assist/open", json={"vacancy_id": ids["1"]})
    assert r.status_code == 200, r.text
    opened = r.json()
    assert opened["mode"] == "assist" and opened["status"] == "needs_manual"
    assert opened["apply_url"].startswith("https://hh.ru/applicant/vacancy_response?vacancyId=")
    assert opened["letter_text"].startswith("Здравствуйте!")
    assert (await client.get(f"/api/vacancies/{ids['1']}")).json()["pipeline_status"] == "needs_manual"
    # opening again reuses the same application
    r = await client.post("/api/applications/assist/open", json={"vacancy_id": ids["1"]})
    assert r.json()["id"] == opened["id"]
    # confirm sent
    r = await client.post(f"/api/applications/{opened['id']}/confirm", json={"sent": True})
    assert r.json()["status"] == "sent" and r.json()["confirmed_by_user"] is True
    assert (await client.get(f"/api/vacancies/{ids['1']}")).json()["pipeline_status"] == "sent"
    assert (await client.get("/api/applications/status")).json()["limits"]["today_sent"] == 1
    # decline restores the vacancy
    r = await client.post("/api/applications/assist/open", json={"vacancy_id": ids["2"]})
    r = await client.post(f"/api/applications/{r.json()['id']}/confirm", json={"sent": False})
    assert r.json()["status"] == "cancelled"
    assert (await client.get(f"/api/vacancies/{ids['2']}")).json()["pipeline_status"] == "prescored"


@respx.mock(assert_all_called=False)
async def test_autopilot_enqueues_suitable(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    runtime.settings = runtime.settings.model_copy(update={"apply_autopilot": True, "apply_dry_run": True})
    ids = await _seed(client, runtime, respx_mock, ["1", "2"])
    await _consent(client)
    async with runtime.session_factory() as session:
        v1 = await session.get(Vacancy, ids["1"])
        v1.pipeline_status, v1.score = "suitable", 90
        v2 = await session.get(Vacancy, ids["2"])
        v2.pipeline_status, v2.score = "suitable", 60  # below autopilot threshold
        await session.commit()
    from app.scheduler import apply_tick

    await apply_tick(runtime)
    await runtime.jobs.wait("apply")
    items = (await client.get("/api/applications")).json()["items"]
    assert len(items) == 1 and items[0]["vacancy"]["hh_id"] == "1" and items[0]["autopilot"] is True
    assert items[0]["status"] == "sent" and items[0]["dry_run"] is True


async def test_run_apply_without_consent(api) -> None:
    client, runtime = api
    summary = await run_apply(runtime)
    assert summary["stopped"] == "нет согласия пользователя"
    async with runtime.session_factory() as session:
        state = await get_system_state(session)
        assert state.consent_accepted_at is None
        assert (await session.execute(select(Application))).scalars().all() == []
    _ = json  # keep import used for fixture helpers


@respx.mock(assert_all_called=False)
async def test_cancel_during_delay_is_respected(
    api, respx_mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row cancelled from the UI while the job sleeps between sends must not be sent (stale identity map)."""
    client, runtime = api
    runtime.settings = runtime.settings.model_copy(update={"apply_min_delay_sec": 1, "apply_max_delay_sec": 1})
    ids = await _seed(client, runtime, respx_mock, ["1", "2"])
    await _consent(client)
    route = _mock_post(respx_mock, [httpx.Response(201, headers={"Location": "/negotiations/1"})] * 2)

    async def fake_sleep(_delay: float) -> None:
        # cancel the still-queued application while the job "sleeps"
        rows = (await client.get("/api/applications", params={"status": "queued"})).json()["items"]
        assert len(rows) == 1
        r = await client.post(f"/api/applications/{rows[0]['id']}/cancel")
        assert r.status_code == 200 and r.json()["status"] == "cancelled"

    monkeypatch.setattr("app.services.applier.asyncio.sleep", fake_sleep)
    r = await client.post("/api/applications/queue", json={"vacancy_ids": [ids["1"], ids["2"]]})
    assert len(r.json()["queued"]) == 2
    await runtime.jobs.wait("apply")
    assert route.call_count == 1
    statuses = sorted(a["status"] for a in (await client.get("/api/applications")).json()["items"])
    assert statuses == ["cancelled", "sent"]
    vac = {
        v["hh_id"]: v["pipeline_status"]
        for v in (await client.get("/api/vacancies", params={"group": "all"})).json()["items"]
    }
    assert sorted(vac.values()) == ["prescored", "sent"]
