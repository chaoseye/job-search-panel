"""SPA fallback: only meaningful when frontend/dist exists (after `npm run build`)."""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from app.config import Settings
from app.main import FRONTEND_DIST, app
from app.runtime import Runtime

pytestmark = pytest.mark.skipif(not FRONTEND_DIST.exists(), reason="frontend not built")


@pytest.fixture
async def api(settings: Settings):
    runtime = await Runtime.create(settings)
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    await runtime.close()


async def test_spa_routes(api: httpx.AsyncClient) -> None:
    r = await api.get("/settings")
    assert r.status_code == 200 and '<div id="root">' in r.text
    r = await api.get("/")
    assert r.status_code == 200
    r = await api.head("/")
    assert r.status_code == 200
    r = await api.get("/favicon.ico")  # a real file in dist (frontend/public), not the SPA shell
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/")
    r = await api.get("/missing.ico")  # a missing file with an extension is a 404, not index.html
    assert r.status_code == 404
    r = await api.get("/api/does-not-exist")
    assert r.status_code == 404
