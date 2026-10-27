"""API tests through the ASGI app with an isolated runtime (no scheduler)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from httpx import ASGITransport

from app.config import Settings
from app.main import app
from app.runtime import Runtime
from app.services.state import get_setting


@pytest.fixture
async def api(settings: Settings):
    runtime = await Runtime.create(settings)
    app.state.runtime = runtime
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


async def test_health_and_status(api) -> None:
    client, _ = api
    r = await client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    r = await client.get("/api/auth/status")
    body = r.json()
    assert body["logged_in"] is False
    assert body["client_id_set"] is True
    assert body["redirect_uri"] == "http://localhost:8000/api/auth/hh/callback"


async def test_login_redirects_to_hh(api) -> None:
    client, runtime = api
    r = await client.get("/api/auth/hh/login")
    assert r.status_code == 302
    loc = urlparse(r.headers["location"])
    assert loc.netloc == "hh.ru" and loc.path == "/oauth/authorize"
    q = parse_qs(loc.query)
    async with runtime.session_factory() as session:
        pending = await get_setting(session, "oauth_pending")
    assert pending["state"] == q["state"][0]
    assert q["role"] == ["applicant"]


async def test_callback_bad_state(api) -> None:
    client, _ = api
    r = await client.get("/api/auth/hh/callback", params={"code": "x", "state": "wrong"})
    assert r.status_code == 302
    assert r.headers["location"] == "/settings?auth=error&reason=bad_state"


async def test_callback_access_denied(api) -> None:
    client, _ = api
    r = await client.get("/api/auth/hh/callback", params={"error": "access_denied", "state": "s"})
    assert r.headers["location"] == "/settings?auth=error&reason=access_denied"


@respx.mock
async def test_full_login_flow(api) -> None:
    client, runtime = api
    respx.post("https://api.hh.ru/token").mock(
        return_value=httpx.Response(200, json={"access_token": "acc", "refresh_token": "ref", "expires_in": 1209600})
    )
    me_route = respx.get("https://api.hh.ru/me").mock(
        return_value=httpx.Response(
            200, json={"id": "42", "first_name": "Иван", "last_name": "Петров", "is_applicant": True}
        )
    )
    r = await client.get("/api/auth/hh/login")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = await client.get("/api/auth/hh/callback", params={"code": "c0de", "state": state})
    assert r.status_code == 302 and r.headers["location"] == "/settings?auth=ok"
    assert me_route.calls.last.request.headers["Authorization"] == "Bearer acc"

    status = (await client.get("/api/auth/status")).json()
    assert status["logged_in"] is True and status["state"] == "ok"
    assert status["user"]["first_name"] == "Иван" and status["hh_user_id"] == "42"

    # pending state is consumed
    async with runtime.session_factory() as session:
        assert await get_setting(session, "oauth_pending") is None

    # logout revokes and clears
    respx.delete("https://api.hh.ru/token").mock(return_value=httpx.Response(204))
    r = await client.post("/api/auth/logout")
    assert r.json() == {"ok": True, "revoked": True}
    assert (await client.get("/api/auth/status")).json()["logged_in"] is False


@respx.mock
async def test_login_not_applicant(api) -> None:
    client, _ = api
    respx.post("https://api.hh.ru/token").mock(
        return_value=httpx.Response(200, json={"access_token": "acc", "refresh_token": "ref", "expires_in": 100})
    )
    respx.get("https://api.hh.ru/me").mock(return_value=httpx.Response(200, json={"id": "1", "is_applicant": False}))
    r = await client.get("/api/auth/hh/login")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = await client.get("/api/auth/hh/callback", params={"code": "c", "state": state})
    assert r.headers["location"] == "/settings?auth=error&reason=not_applicant"


async def test_probe_requires_login(api) -> None:
    client, _ = api
    r = await client.post("/api/probe/run", json={})
    assert r.status_code == 400


async def test_settings_override(api) -> None:
    client, _ = api
    r = await client.put(
        "/api/settings", json={"apply_mode_override": "api", "resume_id": "https://hh.ru/resume/abcdef0123?x=1"}
    )
    body = r.json()
    assert body["apply_mode_override"] == "api" and body["apply_mode"] == "api"
    assert body["resume_id"] == "abcdef0123"
    r = await client.put("/api/settings", json={"apply_mode_override": "auto"})
    assert r.json()["apply_mode"] == "assist"  # no probe yet -> suggested assist


@respx.mock
async def test_probe_run_persists_report(api) -> None:
    client, runtime = api
    respx.post("https://api.hh.ru/token").mock(
        return_value=httpx.Response(200, json={"access_token": "acc", "refresh_token": "ref", "expires_in": 100000})
    )
    respx.get("https://api.hh.ru/me").mock(return_value=httpx.Response(200, json={"id": "1", "is_applicant": True}))
    r = await client.get("/api/auth/hh/login")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    await client.get("/api/auth/hh/callback", params={"code": "c", "state": state})

    for path in (
        "/dictionaries",
        "/areas/113",
        "/vacancies/555",
        "/vacancies/555/similar_vacancies",
        "/negotiations",
        "/common/chats",
    ):
        respx.get(f"https://api.hh.ru{path}").mock(return_value=httpx.Response(200, json={"items": []}))
    respx.get("https://api.hh.ru/vacancies").mock(
        return_value=httpx.Response(200, json={"found": 1, "items": [{"id": "555"}]})
    )
    respx.get("https://api.hh.ru/resumes/mine").mock(
        return_value=httpx.Response(403, json={"errors": [{"type": "forbidden"}]})
    )
    respx.post("https://api.hh.ru/negotiations").mock(
        return_value=httpx.Response(403, json={"errors": [{"type": "forbidden"}]})
    )

    r = await client.post("/api/probe/run", json={})
    assert r.status_code == 200
    report = r.json()
    assert report["apply_mode_suggested"] == "assist" and report["apply_mode_effective"] == "assist"
    assert (runtime.settings.data_dir / "probe_report.json").exists()
    assert (await client.get("/api/probe/report")).json()["capabilities"]["negotiations_post"] == "unavailable"
    view = (await client.get("/api/settings")).json()
    assert view["capabilities"]["vacancies_search"] == "available" and view["probe_at"]


@respx.mock
async def test_scheduled_probe_notices_reopened_methods(api) -> None:
    """The weekly re-probe stores the new verdict and notifies when hh reopens a closed method."""
    from app.services.probe_runner import scheduled_probe

    client, runtime = api
    assert (await scheduled_probe(runtime)) == {"skipped": "не выполнен вход"}
    respx.post("https://api.hh.ru/token").mock(
        return_value=httpx.Response(200, json={"access_token": "acc", "refresh_token": "ref", "expires_in": 100000})
    )
    respx.get("https://api.hh.ru/me").mock(return_value=httpx.Response(200, json={"id": "1", "is_applicant": True}))
    r = await client.get("/api/auth/hh/login")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    await client.get("/api/auth/hh/callback", params={"code": "c", "state": state})
    for path in ("/dictionaries", "/areas/113", "/vacancies/555", "/vacancies/555/similar_vacancies", "/common/chats"):
        respx.get(f"https://api.hh.ru{path}").mock(return_value=httpx.Response(200, json={"items": []}))
    respx.get("https://api.hh.ru/vacancies").mock(
        return_value=httpx.Response(200, json={"found": 1, "items": [{"id": "555"}]})
    )
    respx.get("https://api.hh.ru/resumes/mine").mock(
        return_value=httpx.Response(403, json={"errors": [{"type": "forbidden"}]})
    )
    negotiations = respx.get("https://api.hh.ru/negotiations").mock(
        return_value=httpx.Response(403, json={"errors": [{"type": "forbidden"}]})
    )
    respx.post("https://api.hh.ru/negotiations").mock(
        return_value=httpx.Response(403, json={"errors": [{"type": "forbidden"}]})
    )
    # first probe: everything applicant-side is closed, nothing to report
    first = await scheduled_probe(runtime)
    assert first["changes"] == 0 and first["apply_mode"] == "assist"
    feed = (await client.get("/api/notifications", params={"channel": "ui", "limit": 5})).json()
    assert all(n["event_type"] != "capability" for n in feed)
    # hh reopens the negotiations list → the second probe reports the change
    negotiations.mock(return_value=httpx.Response(200, json={"items": [], "found": 0}))
    second = await scheduled_probe(runtime)
    assert second["changes"] == 1
    feed = (await client.get("/api/notifications", params={"channel": "ui", "limit": 5})).json()
    event = next(n for n in feed if n["event_type"] == "capability")
    assert "negotiations_list → available" in event["summary"]
    assert (await client.get("/api/probe/report")).json()["capabilities"]["negotiations_list"] == "available"
