"""Миграции Alembic (вверх/вниз/вверх, одна голова, модели совпадают со схемой), проводка планировщика,
лимитер, JobRunner, .env.example ↔ Settings."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from app.config import ROOT_DIR, Settings
from app.db.base import Base
from app.db.session import alembic_config
from app.hh.ratelimit import TokenBucket
from app.runtime import Runtime
from app.scheduler import _parse_hhmm, build_scheduler
from app.services.jobs import JobBusy, JobRunner
from tests.conftest import FakeClock, FakeSleep


def test_migrations_up_down_up_and_single_head(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    cfg = alembic_config(settings)
    script = ScriptDirectory.from_config(cfg)
    assert len(script.get_heads()) == 1, "у истории миграций должна быть одна голова"
    revisions = list(script.walk_revisions())
    assert len(revisions) >= 19
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    engine = create_engine(settings.sync_db_url)
    with engine.connect() as conn:
        left = set(inspect(conn).get_table_names()) - {"alembic_version"}
    engine.dispose()
    assert left == set(), f"после downgrade base остались таблицы: {sorted(left)}"
    command.upgrade(cfg, "head")
    # и по одному шагу назад/вперёд — каждый downgrade обратим
    for _ in range(3):
        command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")


def test_models_match_migrated_schema(settings: Settings) -> None:
    """Автогенерация Alembic не должна видеть разницы между моделями и схемой после `upgrade head`."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    cfg = alembic_config(settings)
    command.upgrade(cfg, "head")
    engine = create_engine(settings.sync_db_url)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": False, "render_as_batch": True})
            diff = compare_metadata(ctx, Base.metadata)
    finally:
        engine.dispose()
    # SQLite не хранит длину строк и типы «как есть» — сравниваем только структуру: таблицы, колонки, индексы
    structural = [
        d for d in diff if (d[0] if isinstance(d, tuple) else d[0][0]) not in ("modify_type", "modify_default")
    ]
    assert structural == [], "\n".join(str(d) for d in structural)


def _runtime_stub(**overrides: object) -> SimpleNamespace:
    settings = Settings(_env_file=None, **overrides)  # type: ignore[call-arg]
    return SimpleNamespace(settings=settings)


def test_scheduler_default_wiring() -> None:
    scheduler = build_scheduler(_runtime_stub())  # type: ignore[arg-type]
    ids = {j.id for j in scheduler.get_jobs()}
    expected = {
        "auth_check",
        "scan_tick",
        "apply_tick",
        "exchange_tick",
        "sync_tick",
        "notify_tick",
        "daily_summary",
        "summary_catch_up",
        "backup_daily",
        "resume_touch",
        "bot_commands",
        "channel_sync",
        "sites_sync",
        "ws_tenders_sync",
        "freshness_daily",
        "probe_tick",
    }
    assert expected <= ids, expected - ids
    assert (
        "claude_tick" not in ids
        and "mail_sync" not in ids
        and "hot_digest" not in ids
        and "hot_digest_catch_up" not in ids
    )
    assert not any(i.startswith("resume_bump_") for i in ids)
    daily = next(j for j in scheduler.get_jobs() if j.id == "daily_summary")
    assert str(daily.trigger.fields[5]) == "21" and str(daily.trigger.fields[6]) == "0"


def test_scheduler_optional_jobs() -> None:
    scheduler = build_scheduler(
        _runtime_stub(  # type: ignore[arg-type]
            claude_cli_autorun=True,
            hot_digest_at="09:00",
            resume_bump_reminder_at="08:45,13:00,17:15",
            channel_sync_min=0,
            sites_sync_min=0,
            ws_tenders_sync_min=0,
            probe_interval_days=0,
            freshness_stale_days=0,
            imap_host="imap.example.com",
            imap_user="u",
            imap_password="p",
        )
    )
    ids = {j.id for j in scheduler.get_jobs()}
    assert {
        "claude_tick",
        "hot_digest",
        "hot_digest_catch_up",
        "mail_sync",
        "resume_bump_0845",
        "resume_bump_1300",
        "resume_bump_1715",
    } <= ids
    assert not {"channel_sync", "sites_sync", "ws_tenders_sync", "probe_tick", "freshness_daily"} & ids


def test_parse_hhmm_clamps_and_defaults() -> None:
    assert _parse_hhmm("21:00") == (21, 0) and _parse_hhmm("25:99") == (23, 59)
    assert _parse_hhmm("garbage") == (21, 0) and _parse_hhmm("", (3, 30)) == (3, 30)


async def test_token_bucket_sleeps_exactly_for_the_deficit() -> None:
    clock, sleeper = FakeClock(), FakeSleep()
    bucket = TokenBucket(2.0, burst=3, clock=clock, sleep=sleeper)
    for _ in range(3):
        await bucket.acquire()
    assert sleeper.calls == []
    for _ in range(4):
        await bucket.acquire()
    assert sleeper.calls == pytest.approx([0.5, 0.5, 0.5, 0.5]) and bucket.waits == 4
    clock.advance(10)
    await bucket.acquire()
    assert len(sleeper.calls) == 4  # после паузы ведро полное


async def test_job_runner_exclusion_and_failure_counter(settings: Settings) -> None:
    runtime = await Runtime.create(settings)
    try:
        runner = JobRunner(runtime.session_factory)
        hits: list[tuple[str, str]] = []

        async def on_fail(job: str, error: str) -> None:
            hits.append((job, error))

        runner.on_repeated_failure = on_fail
        gate = asyncio.Event()

        async def slow() -> dict:
            await gate.wait()
            return {"ok": 1}

        await runner.start("x", slow)
        with pytest.raises(JobBusy):
            await runner.start("x", slow)
        assert runner.is_running("x") and runner.running_jobs() == ["x"]
        gate.set()
        await runner.wait("x")
        assert not runner.is_running("x") and runner.failures.get("x") == 0

        async def boom() -> dict:
            raise RuntimeError("nope")

        for _ in range(3):
            await runner.run_inline("y", boom)
        assert runner.failures["y"] == 3 and hits == [("y", "RuntimeError: nope")]
        await runner.run_inline("y", slow)  # gate is set → returns at once
        assert runner.failures["y"] == 0
    finally:
        await runtime.close()


def _env_example_keys() -> list[str]:
    text = (ROOT_DIR / ".env.example").read_text(encoding="utf-8")
    return [m.group(1) for m in re.finditer(r"^([A-Z][A-Z0-9_]+)=", text, re.MULTILINE)]


def test_env_example_keys_exist_in_settings() -> None:
    fields = set(Settings.model_fields)
    unknown = [k for k in _env_example_keys() if k.lower() not in fields]
    assert unknown == [], f"ключи .env.example без поля в Settings: {unknown}"


# исправлено сессией «_a» 2026-09-23 (fix/input-validation-and-parsing): дубль APPLY_MAX_PER_EMPLOYER_7D убран
def test_env_example_has_no_duplicate_keys() -> None:
    keys = _env_example_keys()
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert dupes == [], dupes


def test_env_example_loads_as_settings(tmp_path: Path) -> None:
    s = Settings(_env_file=str(ROOT_DIR / ".env.example"), data_dir=tmp_path)  # type: ignore[call-arg]
    assert s.app_port == 8000 and s.user_agent_is_placeholder and s.public_user_agent == "JobSearchPanel/1.0"
    assert s.db_path == tmp_path / "panel.db" and s.exchange_dir == tmp_path / "exchange"
