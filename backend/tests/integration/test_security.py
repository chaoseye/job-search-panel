"""HTTP-level protection of the local panel: the SPA route must not serve files outside
frontend/dist (the project root holds .env and data/panel.db), unknown Host headers are refused (DNS rebinding),
state-changing requests from another site's page are refused (CSRF), a busy database gives a friendly 503."""

from __future__ import annotations

import json
import sqlite3

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.main import FRONTEND_DIST, app
from app.runtime import Runtime


@pytest.fixture
async def api(settings: Settings):
    runtime = await Runtime.create(settings)
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


@pytest.mark.skipif(not (FRONTEND_DIST / "index.html").exists(), reason="frontend/dist is not built")
@pytest.mark.parametrize(
    "path",
    [
        "/..%2F..%2Fbackend%2Fpyproject.toml",
        "/..%5C..%5Cbackend%5Cpyproject.toml",
        "/%2e%2e%2F%2e%2e%2Fbackend%2Fpyproject.toml",
        "/assets/..%2F..%2F..%2Fbackend%2Fpyproject.toml",
        "/..%2F..%2F.env",
        "/..%2F..%2Fdata%2Fpanel.db",
        "/index.html%00.txt",  # a NUL byte must be a 404, not a 500 from Path.resolve()
    ],
)
async def test_spa_route_never_leaves_dist(api, path: str) -> None:
    client, _runtime = api
    r = await client.get(path)
    assert r.status_code == 404, f"{path} → {r.status_code}"
    assert b"[project]" not in r.content and b"SQLite format" not in r.content


@pytest.mark.skipif(not (FRONTEND_DIST / "index.html").exists(), reason="frontend/dist is not built")
async def test_spa_route_still_serves_the_app(api) -> None:
    client, _runtime = api
    index = (FRONTEND_DIST / "index.html").read_bytes()
    assert (await client.get("/")).content == index
    assert (await client.get("/vacancies")).content == index  # client-side route
    asset = next((FRONTEND_DIST / "assets").glob("*.js"), None)
    if asset is not None:
        r = await client.get(f"/assets/{asset.name}")
        assert r.status_code == 200 and r.content == asset.read_bytes()
    assert (await client.get("/favicon-missing.ico")).status_code == 404


async def test_unknown_host_is_refused(api) -> None:
    client, _runtime = api
    r = await client.get("/api/health", headers={"Host": "evil.example"})
    assert r.status_code == 400 and "Host" in r.json()["detail"]
    for host in ("localhost:8000", "127.0.0.1:8000", "[::1]:8000", "test"):
        assert (await client.get("/api/health", headers={"Host": host})).status_code == 200, host


async def test_cross_site_writes_are_refused(api) -> None:
    client, _runtime = api
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    for headers in (
        {"Origin": "http://evil.example", **form},
        {"Origin": "null", **form},
        {"Sec-Fetch-Site": "cross-site", **form},
    ):
        r = await client.post("/api/jobs/pause", headers=headers)
        assert r.status_code == 403, headers
    # the panel itself (same origin), the Vite dev server and non-browser clients pass
    for headers in ({"Origin": "http://localhost:8000"}, {"Origin": "http://127.0.0.1:5173"}, {}):
        r = await client.post("/api/jobs/resume", headers=headers)
        assert r.status_code == 200, headers
    # reads are not blocked by Origin (CORS already hides the response from other sites)
    assert (await client.get("/api/health", headers={"Origin": "http://evil.example"})).status_code == 200


async def test_allowed_hosts_star_turns_the_checks_off(api) -> None:
    client, runtime = api
    runtime.settings = runtime.settings.model_copy(update={"allowed_hosts": "*"})
    assert (await client.get("/api/health", headers={"Host": "evil.example"})).status_code == 200
    r = await client.post("/api/jobs/resume", headers={"Origin": "http://evil.example"})
    assert r.status_code == 200


def test_allowed_host_names() -> None:
    s = Settings(_env_file=None, allowed_hosts="panel.lan, Other.Host")  # type: ignore[call-arg]
    assert s.allowed_host_names == frozenset({"127.0.0.1", "localhost", "::1", "panel.lan", "other.host"})
    s = Settings(_env_file=None, app_host="0.0.0.0", frontend_url="http://devbox:5173")  # type: ignore[call-arg]
    assert s.allowed_host_names == frozenset({"127.0.0.1", "localhost", "::1", "devbox"})
    assert Settings(_env_file=None, allowed_hosts="*").allowed_host_names is None  # type: ignore[call-arg]


async def test_busy_database_is_a_503_not_a_500(api, monkeypatch) -> None:
    client, _runtime = api
    from app.services import vacancies as svc

    async def locked(*_args, **_kwargs):
        raise OperationalError("UPDATE vacancies …", {}, sqlite3.OperationalError("database is locked"))

    monkeypatch.setattr(svc, "status_counts", locked)
    r = await client.get("/api/vacancies/summary")
    assert r.status_code == 503 and r.json()["kind"] == "db_locked"
    assert "повторите через минуту" in r.json()["detail"]


async def test_pasted_result_cannot_name_a_file_outside_done(api) -> None:
    client, runtime = api
    for batch_id in ("../../evil", "..\\..\\evil", "x" * 300, "20260923_0101_zzzz"):
        text = json.dumps({"protocol_version": 1, "batch_id": batch_id, "results": []})
        r = await client.post("/api/batches/import-text", json={"text": text})
        assert r.status_code == 400 and "batch_id" in r.json()["detail"], batch_id
    exchange = runtime.settings.exchange_dir
    assert (
        not any(p.name.startswith("evil") for p in exchange.rglob("*")) and not (exchange.parent / "evil.json").exists()
    )
