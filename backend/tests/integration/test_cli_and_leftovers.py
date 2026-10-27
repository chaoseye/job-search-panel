"""The fifth audit batch: the CLI next to a running panel goes through its API (no second Runtime — its hh app token
would revoke the panel's one), reads the database «attached»; plus the leftovers — Telegram budget per message, error
text of network failures, TIMEZONE in the bot and the resume hints, the captcha task kept alive, the «Отклики» list
without sorting whole rows, dry run / full sync through the API, reminder settings checked."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError
from sqlalchemy import func, select
from typer.testing import CliRunner

from app import cli
from app.cli import running_panel as real_running_panel
from app.config import Settings
from app.db.base import utcnow
from app.db.models import Application, JobRun, Negotiation, Notification, Resume, SystemState, Vacancy
from app.hh.errors import HHCaptchaRequired
from app.net import error_text
from app.runtime import Runtime
from app.services import applier
from app.services.resume_publish import publish_allowed
from app.services.telegram import _sent_last_minute
from tests.integration.test_api_m2 import _login
from tests.integration.test_api_m4 import _consent, _seed, api  # noqa: F401

__all__ = ["api"]  # fixture shared with test_api_m4

PANEL = "http://127.0.0.1:8123"


def _run_async(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def panel(monkeypatch: pytest.MonkeyPatch, settings: Settings):  # type: ignore[no-untyped-def]
    """A «running panel» mocked with respx; a full in-process Runtime must not be created meanwhile."""
    monkeypatch.setattr(cli, "running_panel", lambda: PANEL)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "PANEL_WAIT_POLL_SEC", 0)
    real_create = Runtime.create

    async def only_attached(*args: Any, **kwargs: Any) -> Runtime:
        assert kwargs.get("attached") is True, "a full Runtime next to a running panel"
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(Runtime, "create", only_attached)
    with respx.mock(base_url=PANEL, assert_all_called=False) as mock:
        yield mock


def _job(mock: respx.MockRouter, start_path: str, *, stats: dict[str, Any], status: str = "ok") -> respx.Route:
    route = mock.post(start_path).mock(return_value=httpx.Response(200, json={"job_run_id": 7}))
    runs_running = {"runs": [{"id": 7, "status": "running", "stats": None, "error": None}]}
    runs_done = {"runs": [{"id": 7, "status": status, "stats": stats, "error": "boom" if status != "ok" else None}]}
    mock.get("/api/jobs").mock(
        side_effect=[httpx.Response(200, json=runs_running), httpx.Response(200, json=runs_done)]
    )
    return route


def test_scan_and_sync_go_through_the_panel(panel: respx.MockRouter) -> None:
    route = _job(panel, "/api/jobs/scan", stats={"new": 3})
    res = CliRunner().invoke(cli.app, ["scan"])
    assert res.exit_code == 0, res.output
    assert route.called and '"new": 3' in res.output

    one = _job(panel, "/api/searches/5/run", stats={"new": 1})
    assert CliRunner().invoke(cli.app, ["scan", "--search", "5"]).exit_code == 0 and one.called

    res = CliRunner().invoke(cli.app, ["scan", "--only-due"])
    assert res.exit_code == 0 and "сама сканирует" in res.output

    sync = _job(panel, "/api/jobs/sync", stats={"items": 2})
    assert CliRunner().invoke(cli.app, ["sync", "--full"]).exit_code == 0
    assert sync.calls.last.request.url.params["full"] == "true"


def test_apply_through_the_panel_keeps_dry_run_and_limit(panel: respx.MockRouter) -> None:
    route = _job(panel, "/api/applications/run", stats={"sent": 0})
    assert CliRunner().invoke(cli.app, ["apply"]).exit_code == 0
    assert dict(route.calls.last.request.url.params) == {"dry_run": "true"}

    route = _job(panel, "/api/applications/run", stats={"sent": 2})
    assert CliRunner().invoke(cli.app, ["apply", "--real", "--max", "2"]).exit_code == 0
    assert dict(route.calls.last.request.url.params) == {"dry_run": "false", "max_items": "2"}


def test_a_failed_job_and_a_refusal_end_with_a_code(panel: respx.MockRouter) -> None:
    _job(panel, "/api/jobs/scan", stats={"errors": 1}, status="error")
    res = CliRunner().invoke(cli.app, ["scan"])
    assert res.exit_code == 1 and "boom" in res.output

    panel.post("/api/jobs/sync").mock(
        return_value=httpx.Response(409, json={"detail": "Сейчас выполняется скан — подождите"})
    )
    res = CliRunner().invoke(cli.app, ["sync"])
    assert res.exit_code == 2 and "Панель отказала (409): Сейчас выполняется скан" in res.output


def test_probe_backup_import_logout_profile_through_the_panel(panel: respx.MockRouter, tmp_path: Path) -> None:
    probe = panel.post("/api/probe/run").mock(
        return_value=httpx.Response(
            200,
            json={
                "steps": [{"key": "me", "method": "GET", "status": 200, "capability": "available", "note": ""}],
                "apply_mode_effective": "assist",
                "apply_mode_suggested": "assist",
            },
        )
    )
    res = CliRunner().invoke(cli.app, ["probe", "--resume-id", "abc"])
    assert res.exit_code == 0 and "available" in res.output and "Режим отклика: assist" in res.output
    assert json.loads(probe.calls.last.request.content)["resume_id"] == "abc"

    panel.post("/api/backups").mock(return_value=httpx.Response(200, json={"name": "panel_x.db"}))
    res = CliRunner().invoke(cli.app, ["backup"])
    assert res.exit_code == 0 and "panel_x.db" in res.output

    panel.post("/api/batches/import").mock(return_value=httpx.Response(200, json={"outcomes": [{"status": "ok"}]}))
    assert '"status": "ok"' in CliRunner().invoke(cli.app, ["import"]).output
    result = tmp_path / "batch_x.json"
    result.write_text('﻿{"batch_id": "x"}', encoding="utf-8")
    text = panel.post("/api/batches/import-text").mock(
        return_value=httpx.Response(200, json={"outcome": {"status": "imported"}})
    )
    assert CliRunner().invoke(cli.app, ["import", str(result)]).exit_code == 0
    assert json.loads(text.calls.last.request.content) == {"text": '{"batch_id": "x"}'}

    logout = panel.post("/api/auth/logout").mock(return_value=httpx.Response(200, json={"ok": True}))
    assert CliRunner().invoke(cli.app, ["logout"]).exit_code == 0 and logout.called

    rebuild = panel.post("/api/profile/rebuild").mock(return_value=httpx.Response(200, json={}))
    panel.get("/api/profile/md").mock(
        return_value=httpx.Response(200, text="# Профиль", headers={"content-type": "text/plain"})
    )
    res = CliRunner().invoke(cli.app, ["profile-md", "--rebuild"])
    assert res.exit_code == 0 and "# Профиль" in res.output and rebuild.called


@pytest.fixture
def seeded(settings: Settings) -> Settings:
    async def seed() -> None:
        rt = await Runtime.create(settings)
        async with rt.session_factory() as session:
            for i, prescore in enumerate((40, 70, 55)):
                session.add(Vacancy(hh_id=str(i), name=f"V{i}", pipeline_status="prescored", prescore=prescore))
            session.add(JobRun(job="scan", status="running"))  # a job the panel is running right now
            await session.commit()
        await rt.close()

    _run_async(seed())
    return settings


def test_attached_commands_do_not_touch_the_panels_jobs(seeded: Settings, panel: respx.MockRouter) -> None:
    create = panel.post("/api/batches/create").mock(
        return_value=httpx.Response(
            200, json={"created": [{"batch_id": "b1", "tasks_count": 2, "packet_path": "p.json"}]}
        )
    )
    res = CliRunner().invoke(cli.app, ["batch", "--limit", "2"])
    assert res.exit_code == 0, res.output
    assert "b1: задач 2" in res.output
    ids = json.loads(create.calls.last.request.content)["vacancy_ids"]
    assert len(ids) == 2  # the two best prescores, read from the database attached

    res = CliRunner().invoke(cli.app, ["stats", "--period", "all"])
    assert res.exit_code == 0 and '"sent": 0' in res.output
    res = CliRunner().invoke(cli.app, ["status"])
    assert res.exit_code == 0 and '"apply_mode"' in res.output

    async def still_running() -> list[str]:
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(seeded.db_url)
        async with engine.connect() as conn:
            rows = (await conn.execute(select(JobRun.status))).scalars().all()
        await engine.dispose()
        return list(rows)

    assert _run_async(still_running()) == ["running"]  # not marked «interrupted» by the CLI


def test_migrate_and_serve_refuse_next_to_the_panel(panel: respx.MockRouter) -> None:
    res = CliRunner().invoke(cli.app, ["migrate"])
    assert res.exit_code == 2 and "Панель запущена" in res.output
    res = CliRunner().invoke(cli.app, ["serve"])
    assert res.exit_code == 0 and "уже запущена" in res.output


def test_without_a_panel_a_full_runtime_still_marks_stale_jobs(seeded: Settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(cli, "get_settings", lambda: seeded)
    res = CliRunner().invoke(cli.app, ["status"])
    assert res.exit_code == 0, res.output

    async def statuses() -> list[str]:
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(seeded.db_url)
        async with engine.connect() as conn:
            rows = (await conn.execute(select(JobRun.status))).scalars().all()
        await engine.dispose()
        return list(rows)

    assert _run_async(statuses()) == ["error"]  # no panel: a «running» row is a leftover of a dead process


def test_running_panel_detection(settings: Settings, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(cli, "get_settings", lambda: settings.model_copy(update={"app_port": 8124}))
    health = "http://127.0.0.1:8124/api/health"
    with respx.mock() as m:
        m.get(health).mock(return_value=httpx.Response(200, json={"ok": True, "version": "0.1.0"}))
        assert real_running_panel() == "http://127.0.0.1:8124"
    with respx.mock() as m:
        m.get(health).mock(return_value=httpx.Response(200, json={"hello": "another app"}))
        assert real_running_panel() is None
    with respx.mock() as m:
        m.get(health).mock(side_effect=httpx.ConnectError("refused"))
        assert real_running_panel() is None


# ---- leftovers -------------------------------------------------------------------------------------------------------


@pytest.fixture
async def runtime(settings: Settings):
    rt = await Runtime.create(settings)
    try:
        yield rt
    finally:
        await rt.close()


async def test_telegram_budget_counts_messages_not_rows(runtime: Runtime) -> None:
    now = utcnow()
    async with runtime.session_factory() as session:
        for i in range(10):  # ten channel posts merged into one message
            session.add(
                Notification(
                    channel="telegram",
                    event_type="channel_post",
                    status="sent",
                    sent_at=now,
                    text="десять постов",
                    payload_json=json.dumps({"i": i}),
                )
            )
        for event, text in (("applied", "отклик"), ("invitation", "приглашение")):
            session.add(Notification(channel="telegram", event_type=event, status="sent", sent_at=now, text=text))
        session.add(
            Notification(
                channel="telegram", event_type="applied", status="sent", sent_at=now - timedelta(minutes=5), text="old"
            )
        )
        await session.commit()
        assert await _sent_last_minute(session, now) == 3


def test_error_text_never_empty() -> None:
    assert error_text(httpx.ReadTimeout("")) == "ReadTimeout"
    assert error_text(ValueError("нет сети")) == "ValueError: нет сети"


def test_resume_hint_uses_the_panel_timezone() -> None:
    resume = Resume(source="hh", can_publish_or_update=False, next_publish_at=datetime(2026, 9, 23, 10, 0, tzinfo=UTC))
    now = datetime(2026, 9, 23, 8, 0, tzinfo=UTC)
    assert "13:00" in publish_allowed(resume, now, tz="Europe/Moscow")[1]
    assert "20:00" in publish_allowed(resume, now, tz="Asia/Vladivostok")[1]


async def test_bot_status_uses_the_panel_timezone(runtime: Runtime) -> None:
    from app.services.bot_commands import _status_text

    async with runtime.session_factory() as session:
        state = await session.get(SystemState, 1)
        if state is None:
            state = SystemState(id=1)
            session.add(state)
        state.last_sync_at = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
        await session.commit()
        text = await _status_text(session, runtime.settings.model_copy(update={"timezone": "Asia/Vladivostok"}))
    assert "синхронизация: 23.09 20:00" in text


async def test_captcha_pause_task_is_kept_until_saved(runtime: Runtime) -> None:
    await runtime.on_captcha(HHCaptchaRequired("https://hh.ru/account/captcha?state=1", None))
    assert len(runtime._background) == 1  # referenced while it runs
    await asyncio.gather(*runtime._background)
    await asyncio.sleep(0)
    assert runtime._background == set()
    async with runtime.session_factory() as session:
        assert (await session.get(SystemState, 1)).paused_reason == "captcha"  # type: ignore[union-attr]


async def test_applications_list_pages_and_stage_counts(runtime: Runtime) -> None:
    now = utcnow()
    async with runtime.session_factory() as session:
        for i in range(14):
            v = Vacancy(hh_id=f"v{i}", name=f"Вакансия {i % 5}", employer_name=f"E{i % 3}", score=50 + i % 4)
            session.add(v)
            await session.flush()
            neg_id = f"n{i}" if i % 2 else None
            session.add(
                Application(
                    vacancy_id=v.id,
                    mode="api",
                    status="sent" if i % 7 else "queued",
                    sent_at=now - timedelta(days=i),
                    hh_negotiation_id=neg_id,
                )
            )
            if neg_id:
                session.add(
                    Negotiation(
                        hh_id=neg_id,
                        viewed_by_opponent=i % 3 == 0,
                        has_updates=i % 5 == 0,
                        state_id="invitation" if i % 4 == 1 else "response",
                    )
                )
        await session.commit()

        for sort in applier.SORTS:
            for order in ("asc", "desc"):
                got: list[int] = []
                for page in range(3):
                    out = await applier.list_applications(session, sort=sort, order=order, page=page, per_page=5)
                    got += [row["id"] for row in out["items"]]
                direction = applier.asc if order == "asc" else applier.desc
                expected = (
                    (
                        await session.execute(
                            select(Application.id)
                            .join(Vacancy, Vacancy.id == Application.vacancy_id, isouter=True)
                            .join(Negotiation, Negotiation.hh_id == Application.hh_negotiation_id, isouter=True)
                            .order_by(direction(applier._sort_key(sort)).nulls_last(), applier.desc(Application.id))
                        )
                    )
                    .scalars()
                    .all()
                )
                assert got == list(expected), (sort, order)

        counts = out["stage_counts"]
        for name in applier.STAGES:
            plain = (
                await session.execute(
                    select(func.count(Application.id))
                    .select_from(Application)
                    .join(Negotiation, Negotiation.hh_id == Application.hh_negotiation_id, isouter=True)
                    .where(applier._stage_condition(name, days=7, now=now))
                )
            ).scalar()
            assert counts[name] == plain, name


@respx.mock(assert_all_called=False)
async def test_run_apply_dry_run_through_the_api(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    ids = await _seed(client, runtime, respx_mock, ["1"])
    await _consent(client)
    post = respx_mock.post("https://api.hh.ru/negotiations").mock(return_value=httpx.Response(201))
    async with runtime.session_factory() as session:
        await applier.enqueue(session, runtime.settings, [ids["1"]])
        await session.commit()
    r = await client.post("/api/applications/run", params={"dry_run": True, "max_items": 1})
    assert r.status_code == 200
    await runtime.jobs.wait("apply")
    assert not post.called
    async with runtime.session_factory() as session:
        row = (await session.execute(select(Application))).scalars().one()
        assert row.status == "sent" and row.dry_run is True


@respx.mock(assert_all_called=False)
async def test_full_sync_through_the_api(api, respx_mock: respx.MockRouter, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    client, runtime = api
    await _login(client, respx_mock)
    seen: list[bool] = []

    async def fake_sync(rt: Runtime, *, full: bool = False) -> dict[str, Any]:
        seen.append(full)
        return {}

    monkeypatch.setattr("app.services.sync.run_sync", fake_sync)
    assert (await client.post("/api/jobs/sync", params={"full": True})).status_code == 200
    await runtime.jobs.wait("sync")
    assert seen == [True]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("resume_bump_reminder_at", "8:45, 25:00"),
        ("resume_bump_reminder_at", "вечером"),
        ("resume_bump_reminder_days", "bogus"),
    ],
)
def test_reminder_settings_are_checked(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})  # type: ignore[call-arg]


def test_reminder_settings_accept_the_documented_forms() -> None:
    for at, days in (("08:45,13:00,17:15", "mon-fri"), ("", "*"), ("9:00", "Monday, wed; FRI"), ("21:30", "sat-mon")):
        Settings(_env_file=None, resume_bump_reminder_at=at, resume_bump_reminder_days=days)  # type: ignore[call-arg]
