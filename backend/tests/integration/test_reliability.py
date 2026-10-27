"""Reliability of the background work: a torn TLS record is retried like any dropped
connection, Telegram keeps retrying transient failures for 24 h, the evening summary and the nightly backup survive a
sleeping laptop, and a scan that achieved nothing is a failed run that reaches Telegram after three in a row."""

from __future__ import annotations

import json
import os
import ssl
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from app.config import Settings
from app.db.models import JobRun, Notification, SavedSearch
from app.hh.client import HHClient
from app.runtime import Runtime
from app.scheduler import build_scheduler, last_summary_slot, summary_catch_up
from app.services import channels as ch
from app.services.backup import catch_up_backup, last_backup_at, make_backup
from app.services.jobs import FAILED_KEY
from app.services.notify import emit_event, render_event, ui_summary
from app.services.scanner import run_scan
from app.services.telegram import TelegramClient, TelegramError, deliver_pending
from tests.conftest import FakeSleep

BASE = "https://api.hh.ru"
TOKEN = "123456789:" + "B" * 35
TLS_ERROR = ssl.SSLError(1, "[SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC] decryption failed or bad record mac")


@pytest.fixture
async def runtime(settings: Settings):
    rt = await Runtime.create(settings)
    yield rt
    await rt.close()


# ---- a torn TLS record is a network error, not a crash --------------------------------------------------------------


@respx.mock(base_url=BASE)
async def test_hh_client_retries_a_tls_record_error(
    client: HHClient, respx_mock: respx.MockRouter, fake_sleep: FakeSleep
) -> None:
    route = respx_mock.get("/vacancies").mock(side_effect=[TLS_ERROR, httpx.Response(200, json={"found": 0})])
    assert await client.get("/vacancies") == {"found": 0}
    assert route.call_count == 2 and fake_sleep.calls == [2.0]


async def test_telegram_and_channels_treat_tls_errors_as_network(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(side_effect=TLS_ERROR)
    respx_mock.get("https://t.me/s/some_channel").mock(side_effect=TLS_ERROR)
    async with httpx.AsyncClient() as http:
        with pytest.raises(TelegramError) as tg:
            await TelegramClient(http, TOKEN).call("sendMessage", chat_id=1, text="x")
        assert tg.value.transient and TOKEN not in str(tg.value)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        with pytest.raises(ch.ChannelError, match="сеть"):
            await ch.fetch_preview(http, settings, "some_channel")


# ---- Telegram: transient failures are retried until the message is a day old ----------------------------------------


async def test_telegram_retries_for_a_day_then_gives_up(runtime: Runtime, respx_mock: respx.MockRouter) -> None:
    settings = runtime.settings.model_copy(update={"telegram_bot_token": TOKEN, "telegram_chat_id": "42"})
    respx_mock.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(side_effect=httpx.ConnectError("offline"))
    now = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    async with runtime.session_factory() as session:
        fresh = Notification(
            channel="telegram", event_type="captcha", payload_json="{}", status="queued", attempts=0, created_at=now
        )
        old = Notification(
            channel="telegram",
            event_type="captcha",
            payload_json="{}",
            status="queued",
            attempts=0,
            created_at=now - timedelta(hours=25),
        )
        session.add_all([fresh, old])
        await session.commit()
        # an evening offline: 20 delivery passes spread over 5 hours — the fresh message is still queued
        for i in range(20):
            await deliver_pending(session, settings, runtime.http, now=now + timedelta(minutes=15 * i), force=True)
            await session.commit()
        await session.refresh(fresh)
        await session.refresh(old)
        assert fresh.status == "queued" and fresh.attempts >= 10, (fresh.status, fresh.attempts)
        assert old.status == "failed" and "24" in (old.error or "")


# ---- the scheduler waits for a sleeping laptop; a missed backup is made at start ------------------------------------


async def test_cron_jobs_survive_a_sleeping_laptop(runtime: Runtime) -> None:
    jobs = {j.id: j for j in build_scheduler(runtime).get_jobs()}
    # the summary is on time within 10 minutes; later it comes from the catch-up, marked as late
    assert jobs["daily_summary"].misfire_grace_time == 10 * 60
    assert jobs["summary_catch_up"].trigger.interval == timedelta(minutes=15)
    assert jobs["backup_daily"].misfire_grace_time == 20 * 60 * 60


def test_last_summary_slot_is_the_latest_due_moment() -> None:
    settings = Settings(_env_file=None, timezone="Europe/Moscow", telegram_daily_summary_at="21:00")  # type: ignore[call-arg]
    msk = timedelta(hours=3)
    at = lambda d, h, m: datetime(2026, 9, d, h, m, tzinfo=UTC) - msk  # noqa: E731 - Moscow time → UTC
    assert last_summary_slot(settings, at(26, 20, 59)) == at(25, 21, 0)
    assert last_summary_slot(settings, at(26, 21, 0)) == at(26, 21, 0)
    assert last_summary_slot(settings, at(26, 23, 25)) == at(26, 21, 0)
    assert last_summary_slot(settings, at(27, 0, 30)) == at(26, 21, 0)
    early = settings.model_copy(update={"telegram_daily_summary_at": "07:30"})
    assert last_summary_slot(early, at(27, 7, 29)) == at(26, 7, 30)


async def test_missed_evening_summary_is_sent_once_and_marked_late(runtime: Runtime) -> None:
    """The panel was switched off at 21:00 (from the evening until late at night): the summary comes once it is back —
    that evening or the next morning, for the missed day, with a note; never twice, never on a fresh panel."""
    runtime.settings = runtime.settings.model_copy(
        update={
            "timezone": "Europe/Moscow",
            "telegram_daily_summary_at": "21:00",
            "telegram_bot_token": TOKEN,
            "telegram_chat_id": "42",
        }
    )
    msk = timedelta(hours=3)

    def at(day: int, hour: int, minute: int) -> datetime:
        return datetime(2026, 9, day, hour, minute, tzinfo=UTC) - msk

    async def summaries() -> list[Notification]:
        async with runtime.session_factory() as session:
            rows = await session.execute(
                select(Notification).where(Notification.event_type == "daily").order_by(Notification.id)
            )
            return list(rows.scalars())

    # a fresh panel that never sent a summary waits for its first regular one
    assert await summary_catch_up(runtime, now=at(26, 23, 25)) is False
    async with runtime.session_factory() as session:
        await emit_event(session, runtime.settings, "daily", {"date": "2026-09-25"}, now=at(25, 21, 0))
        await session.commit()

    # up to 21:15 the regular job may still run (its grace is 10 minutes): no catch-up
    assert await summary_catch_up(runtime, now=at(26, 21, 5)) is False
    # the same evening: the numbers of today, a note in the text and in the feed
    assert await summary_catch_up(runtime, now=at(26, 23, 25)) is True
    rows = await summaries()
    ui, tg = (next(r for r in rows[-2:] if r.channel == c) for c in ("ui", "telegram"))
    payload = json.loads(ui.payload_json)
    assert payload["date"] == "2026-09-26" and payload["late"] == "21:00" and tg.status == "queued"
    assert "С опозданием: в 21:00 панель не работала" in render_event("daily", [payload], timezone="Europe/Moscow")
    assert ui_summary("daily", payload) == "Вечерняя сводка за 2026-09-26 (с опозданием)"
    # sent → the ticks until the next evening do nothing, whether the panel keeps running or restarts
    assert await summary_catch_up(runtime, now=at(26, 23, 40)) is False
    assert await summary_catch_up(runtime, now=at(27, 9, 0)) is False
    count = len(await summaries())

    # the next evening is missed too and the panel comes back only in the morning: yesterday's summary, once
    assert await summary_catch_up(runtime, now=at(28, 9, 0)) is True
    payload = json.loads((await summaries())[-2].payload_json)
    assert payload["date"] == "2026-09-27" and payload["late"] == "21:00"
    assert await summary_catch_up(runtime, now=at(28, 9, 15)) is False
    assert len(await summaries()) == count + 2  # one ui row and one Telegram row


async def test_backup_catch_up_at_start(runtime: Runtime) -> None:
    assert last_backup_at(runtime.settings) is None
    assert await catch_up_backup(runtime) is True  # never backed up → now
    await runtime.jobs.wait("backup")
    first = last_backup_at(runtime.settings)
    assert first is not None
    assert await catch_up_backup(runtime) is False  # fresh copy → nothing to do
    # the newest copy is 30 hours old (the laptop slept through 03:30 yesterday)
    newest = sorted(runtime.settings.backups_dir.glob("panel_*.db"))[-1]
    stamp = time.time() - 30 * 3600
    os.utime(newest, (stamp, stamp))
    assert await catch_up_backup(runtime) is True
    await runtime.jobs.wait("backup")
    assert len(list(runtime.settings.backups_dir.glob("panel_*.db"))) == 2
    await make_backup(runtime.settings)  # still works alongside


# ---- a scan that achieved nothing is a failed run -------------------------------------------------------------------


@respx.mock(assert_all_called=False)
async def test_scan_with_every_search_failing_is_a_failed_run(runtime: Runtime, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{BASE}/vacancies").mock(return_value=httpx.Response(503, json={"errors": [{"type": "x"}]}))
    respx_mock.get(url__regex=rf"{BASE}/(dictionaries|industries).*").mock(return_value=httpx.Response(503))
    async with runtime.session_factory() as session:
        session.add_all(
            [
                SavedSearch(name="A", params_json='{"text": "python"}', interval_min=30),
                SavedSearch(name="B", params_json='{"text": "go"}', interval_min=30),
            ]
        )
        await session.commit()
    for _ in range(3):
        stats = await runtime.jobs.run_inline("scan", lambda: run_scan(runtime))
        assert FAILED_KEY in stats and "все поиски (2)" in stats[FAILED_KEY]
    async with runtime.session_factory() as session:
        runs = (await session.execute(select(JobRun).where(JobRun.job == "scan"))).scalars().all()
        assert [r.status for r in runs] == ["error"] * 3 and all(r.stats_json for r in runs)
        # the third failure in a row reaches the feed (and Telegram when configured): job_error
        errors = (
            (await session.execute(select(Notification).where(Notification.event_type == "job_error"))).scalars().all()
        )
        assert errors, "no job_error after three failed scans"
    # hh is back: an ordinary run resets the streak
    respx_mock.get(f"{BASE}/vacancies").mock(
        return_value=httpx.Response(200, json={"found": 0, "pages": 0, "items": []})
    )
    stats = await runtime.jobs.run_inline("scan", lambda: run_scan(runtime))
    assert FAILED_KEY not in stats and runtime.jobs.failures["scan"] == 0


@respx.mock(assert_all_called=False)
async def test_scan_stopped_by_a_captcha_is_a_failed_run(runtime: Runtime, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(url__regex=rf"{BASE}/(dictionaries|industries).*").mock(return_value=httpx.Response(503))
    respx_mock.get(f"{BASE}/vacancies").mock(
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
    async with runtime.session_factory() as session:
        session.add(SavedSearch(name="A", params_json='{"text": "python"}', interval_min=30))
        await session.commit()
    stats = await runtime.jobs.run_inline("scan", lambda: run_scan(runtime))
    assert stats.get("aborted") and "остановлено" in stats[FAILED_KEY]
    async with runtime.session_factory() as session:
        run = (await session.execute(select(JobRun).where(JobRun.job == "scan"))).scalar_one()
        assert run.status == "error"
