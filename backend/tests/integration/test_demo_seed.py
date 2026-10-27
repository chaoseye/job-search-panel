"""The demo base (`python -m app.cli demo-seed`, services/demo.py): only into an empty base, nothing leaves the
machine, and every page of the panel has something to show."""

from __future__ import annotations

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select
from typer.testing import CliRunner

from app.config import Settings
from app.db.models import Vacancy
from app.main import app
from app.runtime import Runtime
from app.services.demo import HH_ID_BASE, DemoRefused, seed_demo


@pytest.fixture
async def api(settings: Settings):
    runtime = await Runtime.create(settings.model_copy(update={"batch_auto_create": False}))
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


@respx.mock  # any request to the outside world fails the test
async def test_demo_fills_every_page(api) -> None:
    client, runtime = api
    async with runtime.session_factory() as session:
        counts = await seed_demo(session, runtime.settings)
        await session.commit()
    assert counts["vacancies"] >= 60 and counts["applications"] >= 20 and counts["tenders"] and counts["studios"]

    # the vacancy numbers are out of hh.ru's range: a card's link leads nowhere real
    async with runtime.session_factory() as session:
        ids = [int(v) for v in (await session.execute(select(Vacancy.hh_id))).scalars()]
    assert min(ids) >= HH_ID_BASE

    board = (await client.get("/api/board")).json()
    columns = {c["key"]: c["count"] for c in board["columns"]}
    assert columns["new"] > 0 and columns["confirm"] == 2 and columns["sent"] > 0 and columns["answer"] > 0
    for source in ("tenders", "studios"):
        r = await client.get("/api/board", params={"source": source})
        assert r.status_code == 200 and any(c["count"] for c in r.json()["columns"]), source

    ready = (await client.get("/api/vacancies", params={"group": "suitable", "sort": "score"})).json()
    assert ready["total"] > 0
    detail = (await client.get(f"/api/vacancies/{ready['items'][0]['id']}")).json()
    assert detail["evaluation"]["summary"] and detail["letter"]["text"].startswith("Здравствуйте!")
    clarify = (await client.get("/api/vacancies", params={"group": "needs_clarification"})).json()
    assert clarify["total"] > 0  # letters with [[УТОЧНИТЬ]] are part of the demo

    applications = (await client.get("/api/applications")).json()
    assert applications["total"] >= 20
    stats = await client.get("/api/stats")
    assert stats.status_code == 200
    for by in ("search", "title", "stack"):
        segments = (await client.get("/api/analytics/segments", params={"by": by})).json()
        assert segments["items"], by
    assert (await client.get("/api/tenders")).json()["items"]
    assert (await client.get("/api/studios")).json()["items"]
    feed = (await client.get("/api/notifications", params={"channel": "ui"})).json()
    assert feed and all(row["status"] == "sent" for row in feed)  # no bot in the demo: the feed only


async def test_demo_goes_only_into_an_empty_base(api) -> None:
    _client, runtime = api
    async with runtime.session_factory() as session:
        await seed_demo(session, runtime.settings)
        await session.commit()
    async with runtime.session_factory() as session:
        with pytest.raises(DemoRefused, match="DATA_DIR"):
            await seed_demo(session, runtime.settings)


def test_cli_refuses_next_to_a_running_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import cli

    monkeypatch.setattr(cli, "running_panel", lambda: "http://127.0.0.1:8000")
    result = CliRunner().invoke(cli.app, ["demo-seed"])
    assert result.exit_code == 2 and "Панель запущена" in result.output
