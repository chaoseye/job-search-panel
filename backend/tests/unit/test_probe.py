from __future__ import annotations

import httpx
import respx

from app.hh.client import HHClient
from app.hh.probe import AVAILABLE, SKIPPED, UNAVAILABLE, UNKNOWN, ProbeOptions, run_probe

BASE = "https://api.hh.ru"


def _ok(payload: dict | None = None) -> httpx.Response:
    return httpx.Response(200, json=payload or {})


def _mock_common(m: respx.MockRouter, *, resumes_mine: httpx.Response, negotiations_post: httpx.Response) -> None:
    m.get("/me").mock(return_value=_ok({"id": "7", "first_name": "Иван", "is_applicant": True}))
    m.get("/dictionaries").mock(return_value=_ok({"experience": []}))
    m.get("/areas/113").mock(return_value=_ok({"id": "113"}))
    m.get("/vacancies").mock(return_value=_ok({"found": 10, "items": [{"id": "555", "name": "Dev"}]}))
    m.get("/vacancies/555").mock(return_value=_ok({"id": "555"}))
    m.get("/vacancies/555/similar_vacancies").mock(return_value=_ok({"items": []}))
    m.get("/resumes/mine").mock(return_value=resumes_mine)
    m.get("/resumes/abc").mock(return_value=_ok({"id": "abc"}))
    m.get("/resumes/abc/similar_vacancies").mock(return_value=_ok({"items": []}))
    m.get("/negotiations").mock(return_value=_ok({"items": [], "found": 0}))
    m.get("/common/chats").mock(return_value=_ok({"items": []}))
    m.post("/negotiations").mock(return_value=negotiations_post)


@respx.mock(base_url=BASE, assert_all_called=False)
async def test_probe_all_available_suggests_api(client: HHClient, respx_mock: respx.MockRouter) -> None:
    _mock_common(
        respx_mock,
        resumes_mine=_ok({"items": [{"id": "abc", "title": "Python dev", "status": {"id": "published"}}]}),
        negotiations_post=httpx.Response(
            400, json={"errors": [{"type": "negotiations", "value": "vacancy_not_found"}]}
        ),
    )
    report = await run_probe(client)
    caps = report.capabilities
    assert caps["me"] == AVAILABLE
    assert caps["vacancies_search"] == AVAILABLE
    assert caps["vacancy_get"] == AVAILABLE
    assert caps["resumes_mine"] == AVAILABLE
    assert caps["resume_get"] == AVAILABLE
    assert caps["resume_similar"] == AVAILABLE
    assert caps["negotiations_list"] == AVAILABLE
    assert caps["negotiations_post"] == AVAILABLE
    assert caps["resume_publish"] == SKIPPED and caps["vacancy_blacklist"] == SKIPPED
    assert report.apply_mode_suggested == "api"
    assert report.resume_ids == [{"id": "abc", "title": "Python dev", "status": "published"}]
    assert report.me is not None and report.me["is_applicant"] is True
    post_req = respx_mock.routes[-1].calls.last.request
    assert post_req.headers["Content-Type"].startswith("multipart/form-data")
    assert 'name="resume_id"' in post_req.content.decode() and "abc" in post_req.content.decode()


@respx.mock(base_url=BASE, assert_all_called=False)
async def test_probe_closed_applicant_api_suggests_assist(client: HHClient, respx_mock: respx.MockRouter) -> None:
    _mock_common(
        respx_mock,
        resumes_mine=httpx.Response(403, json={"errors": [{"type": "forbidden"}]}),
        negotiations_post=httpx.Response(403, json={"errors": [{"type": "forbidden"}]}),
    )
    report = await run_probe(client)
    caps = report.capabilities
    assert caps["resumes_mine"] == UNAVAILABLE
    assert caps["resume_get"] == SKIPPED  # no resume id known
    assert caps["negotiations_post"] == UNAVAILABLE
    assert report.apply_mode_suggested == "assist"


@respx.mock(base_url=BASE, assert_all_called=False)
async def test_probe_manual_resume_id_used_when_mine_closed(client: HHClient, respx_mock: respx.MockRouter) -> None:
    _mock_common(
        respx_mock,
        resumes_mine=httpx.Response(403, json={"errors": [{"type": "forbidden"}]}),
        negotiations_post=httpx.Response(404, json={"errors": [{"type": "not_found"}]}),
    )
    report = await run_probe(client, ProbeOptions(resume_id="abc"))
    caps = report.capabilities
    assert caps["resume_get"] == AVAILABLE
    assert caps["negotiations_post"] == UNKNOWN
    assert report.apply_mode_suggested == "assist"


@respx.mock(base_url=BASE, assert_all_called=False)
async def test_probe_survives_errors_and_runs_optional_steps(client: HHClient, respx_mock: respx.MockRouter) -> None:
    _mock_common(
        respx_mock,
        resumes_mine=_ok({"items": [{"id": "abc", "title": "t", "status": {"id": "published"}}]}),
        negotiations_post=httpx.Response(
            400, json={"errors": [{"type": "bad_argument", "value": "required_applicant_visibility_id"}]}
        ),
    )
    respx_mock.get("/vacancies/555").mock(side_effect=httpx.ConnectError("down"))
    respx_mock.post("/resumes/abc/publish").mock(return_value=httpx.Response(204))
    respx_mock.put("/vacancies/blacklisted/555").mock(return_value=httpx.Response(204))
    respx_mock.delete("/vacancies/blacklisted/555").mock(return_value=httpx.Response(204))
    report = await run_probe(client, ProbeOptions(allow_publish=True, allow_blacklist=True))
    caps = report.capabilities
    assert caps["vacancy_get"] == UNKNOWN  # transport failure after retries
    assert caps["negotiations_post"] == AVAILABLE
    assert caps["resume_publish"] == AVAILABLE
    assert caps["vacancy_blacklist"] == AVAILABLE
    keys = [s.key for s in report.steps]
    assert "vacancy_blacklist_undo" in keys
    assert report.apply_mode_suggested == "api"


@respx.mock(base_url=BASE, assert_all_called=False)
async def test_probe_not_applicant(client: HHClient, respx_mock: respx.MockRouter) -> None:
    _mock_common(
        respx_mock,
        resumes_mine=httpx.Response(403, json={"errors": [{"type": "forbidden"}]}),
        negotiations_post=httpx.Response(403, json={"errors": [{"type": "forbidden"}]}),
    )
    respx_mock.get("/me").mock(return_value=_ok({"id": "7", "is_applicant": False, "is_employer": True}))
    report = await run_probe(client)
    assert report.capabilities["me"] == UNAVAILABLE
    assert "is_applicant=false" in report.steps[0].note
