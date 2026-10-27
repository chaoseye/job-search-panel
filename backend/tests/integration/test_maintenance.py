"""Upkeep of the base: the checked nightly copy, the retention of routine rows,
VACUUM when much of the file is free, no `branded_description` in stored vacancies, the start/stop records in the
journal, the undelivered Telegram messages on the dashboard."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from alembic import command
from httpx import ASGITransport
from sqlalchemy import select

from app.config import Settings
from app.db.base import utcnow
from app.db.models import AuditLog, JobRun, Notification, Vacancy
from app.db.session import alembic_config
from app.hh.vacancy import UNUSED_FULL_FIELDS, storable_full
from app.main import app
from app.runtime import Runtime
from app.services import backup as bk
from app.services import lifecycle, maintenance
from app.services.notify import render_event
from app.services.scanner import fetch_full
from tests.unit.test_vacancy_norm import load_full


@pytest.fixture
async def runtime(settings: Settings):
    rt = await Runtime.create(settings)
    yield rt
    await rt.close()


@pytest.fixture
async def api(runtime: Runtime):
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


# ---- retention ------------------------------------------------------------------------------------------------------


async def _seed_journal(runtime: Runtime) -> None:
    now = utcnow()
    old, fresh = now - timedelta(days=120), now - timedelta(days=10)
    async with runtime.session_factory() as session:
        session.add_all(
            [
                AuditLog(ts=old, area="scan", message="старый скан"),
                AuditLog(ts=old, area="apply", message="старый отклик", context_json='{"vacancy_id": 7}'),
                AuditLog(ts=fresh, area="scan", message="свежий скан"),
                Notification(channel="ui", event_type="digest", payload_json="{}", status="sent", created_at=old),
                Notification(
                    channel="telegram", event_type="digest", payload_json="{}", status="queued", created_at=old
                ),
                Notification(channel="ui", event_type="digest", payload_json="{}", status="sent", created_at=fresh),
                JobRun(job="scan", started_at=old, finished_at=old, status="ok"),
                JobRun(job="scan", started_at=old, finished_at=old, status="error"),
                JobRun(job="scan", started_at=fresh, finished_at=fresh, status="ok"),
                JobRun(job="probe", started_at=old, finished_at=old, status="ok"),  # its only (last) run
                JobRun(job="sync", started_at=old, status="running"),
            ]
        )
        await session.commit()


async def test_prune_keeps_vacancy_history_unsent_messages_and_last_runs(runtime: Runtime) -> None:
    await _seed_journal(runtime)
    async with runtime.session_factory() as session:
        counts = await maintenance.prune_old_rows(session, runtime.settings)
        await session.commit()
        messages = set((await session.execute(select(AuditLog.message))).scalars())
        notifications = [(n.channel, n.status) for n in (await session.execute(select(Notification))).scalars()]
        runs = sorted((r.job, r.status) for r in (await session.execute(select(JobRun))).scalars())
    assert counts == {"audit_log": 1, "notifications": 1, "job_runs": 2}
    assert messages == {"старый отклик", "свежий скан"}
    assert sorted(notifications) == [("telegram", "queued"), ("ui", "sent")]
    assert runs == [("probe", "ok"), ("scan", "ok"), ("sync", "running")]


async def test_keep_days_zero_keeps_everything(runtime: Runtime) -> None:
    await _seed_journal(runtime)
    async with runtime.session_factory() as session:
        settings = runtime.settings.model_copy(update={"journal_keep_days": 0})
        assert await maintenance.prune_old_rows(session, settings) == {}


# ---- the nightly job: checked copy → prune → VACUUM ------------------------------------------------------------------


async def _bloat(runtime: Runtime, megabytes: int) -> None:
    """Old routine journal rows of `megabytes`: the prune frees them, VACUUM gives the space back."""
    old = utcnow() - timedelta(days=200)
    async with runtime.session_factory() as session:
        session.add_all(
            AuditLog(ts=old, area="scan", message="x" * (100 * 1024) + str(i)) for i in range(megabytes * 10)
        )
        await session.commit()


async def test_nightly_job_copies_prunes_and_compacts(runtime: Runtime) -> None:
    await _bloat(runtime, 24)
    result = await bk.daily_backup_job(runtime)
    copy = runtime.settings.backups_dir / result["name"]
    assert copy.exists() and not list(runtime.settings.backups_dir.glob("*.bad"))
    m = result["maintenance"]
    assert m["deleted"]["audit_log"] == 240
    assert m["vacuum"] == "done" and m["size_after_bytes"] < m["size_bytes"] - 20 * 1024 * 1024
    assert runtime.settings.db_path is not None
    size, free = maintenance.db_space(runtime.settings.db_path)
    assert size == m["size_after_bytes"] and free < 1024 * 1024
    async with runtime.session_factory() as session:
        journal = list((await session.execute(select(AuditLog.message).where(AuditLog.area == "backup"))).scalars())
    assert any(t.startswith("Резервная копия базы") for t in journal)
    assert any(
        t.startswith("Обслуживание базы: удалены старые записи: журнал 240") and "база сжата" in t for t in journal
    )


async def test_vacuum_waits_for_a_running_scan(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    await _bloat(runtime, 20)
    monkeypatch.setattr(runtime.jobs, "running_exclusive", lambda: "scan")
    summary = await maintenance.run_maintenance(runtime)
    assert summary["vacuum"] == "отложен: идёт scan" and "size_after_bytes" not in summary


async def test_small_free_space_is_not_worth_a_vacuum(runtime: Runtime) -> None:
    await _bloat(runtime, 1)
    summary = await maintenance.run_maintenance(runtime)
    assert summary["deleted"]["audit_log"] == 10 and "vacuum" not in summary


def _damage(db: Path) -> None:
    """A base with a broken index page: the backup API copies it as it is."""
    con = sqlite3.connect(db)
    con.execute("create table t(id integer primary key, x text)")
    con.execute("create index ix_t on t(x)")
    con.executemany("insert into t(x) values (?)", [(f"value {i:06d}",) for i in range(5000)])
    con.commit()
    root = con.execute("select rootpage from sqlite_master where name = 'ix_t'").fetchone()[0]
    page = con.execute("pragma page_size").fetchone()[0]
    con.close()
    raw = bytearray(db.read_bytes())
    offset = (root + 1) * page + 200
    raw[offset : offset + 64] = b"\x00" * 64
    db.write_bytes(bytes(raw))


async def test_a_damaged_copy_is_set_aside(tmp_path: Path, settings: Settings) -> None:
    s = settings.model_copy(update={"data_dir": tmp_path / "broken"})
    assert s.db_path is not None
    s.db_path.parent.mkdir(parents=True)
    _damage(s.db_path)
    s.backups_dir.mkdir(parents=True)
    good = s.backups_dir / "panel_20260901_033000.db"
    good.write_bytes(b"an older good copy")
    (s.backups_dir / "panel_20260902_033000.db.bad").write_bytes(b"an older damaged copy")

    with pytest.raises(bk.BackupCorrupt, match="не прошла проверку целостности"):
        await bk.make_backup(s)
    files = sorted(p.name for p in s.backups_dir.iterdir())
    # the damaged copy is kept for a look (only the newest), never counted by the rotation or listed
    assert len(files) == 2 and files[0] == good.name and files[1].endswith(".db.bad") and "20260902" not in files[1]
    assert [b["name"] for b in bk.list_backups(s)] == [good.name]


async def test_backup_job_reports_a_damaged_copy_without_waiting(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def corrupt(settings: Settings) -> dict[str, object]:
        raise bk.BackupCorrupt("копия panel_x.db.bad не прошла проверку целостности (1): Tree 3 page 5")

    monkeypatch.setattr(bk, "make_backup", corrupt)
    with pytest.raises(bk.BackupCorrupt):
        await bk.daily_backup_job(runtime)
    async with runtime.session_factory() as session:
        errors = list(
            (await session.execute(select(Notification).where(Notification.event_type == "job_error"))).scalars()
        )
        journal = list((await session.execute(select(AuditLog).where(AuditLog.level == "error"))).scalars())
    assert len(errors) == 1 and json.loads(errors[0].payload_json)["failures"] == 1
    assert journal and journal[0].message.startswith("Резервная копия повреждена")


def test_single_failure_reads_right() -> None:
    text = render_event("job_error", [{"job": "backup", "error": "копия не прошла проверку", "failures": 1}])
    assert "ошибки подряд" not in text and "<b>backup</b>: копия не прошла проверку" in text
    three = render_event("job_error", [{"job": "scan", "error": "timeout", "failures": 3}])
    assert "3 ошибки подряд" in three


# ---- branded_description --------------------------------------------------------------------------------------------


def test_stored_card_has_no_branded_description() -> None:
    raw = {**load_full(), "branded_description": "<style>.x{}</style><div>много вёрстки</div>"}
    stored = storable_full(raw)
    assert all(k not in stored for k in UNUSED_FULL_FIELDS) and stored["description"] == raw["description"]


async def test_fetch_full_stores_the_card_without_it(runtime: Runtime) -> None:
    class Client:
        async def public(self, path: str) -> dict[str, object]:
            return {**load_full(), "id": "5", "branded_description": "<div>" + "в" * 50_000 + "</div>"}

    row = Vacancy(hh_id="5", name="Python")
    data = await fetch_full(Client(), row)  # type: ignore[arg-type]
    assert data is not None and data.description_text
    assert row.raw_full_json is not None and "branded_description" not in json.loads(row.raw_full_json)


def test_migration_drops_it_from_stored_cards(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, logs_dir=tmp_path / "logs")  # type: ignore[call-arg]
    cfg = alembic_config(settings)
    command.upgrade(cfg, "0021_vacancy_layout")
    con = sqlite3.connect(settings.db_path)  # type: ignore[arg-type]
    card = {"id": "1", "description": "<p>Описание</p>", "branded_description": "<div>вёрстка</div>", "name": "Юрист"}
    broken = '{"branded_description": "<div>'  # not JSON: left as it is
    for hh_id, raw in (("1", json.dumps(card, ensure_ascii=False)), ("2", broken), ("3", '{"name": "без вёрстки"}')):
        con.execute(
            "insert into vacancies (hh_id, name, source_search_ids, pipeline_status, first_seen_at, created_at, "
            "updated_at, archived, response_letter_required, has_test, full_fetch_errors, hidden, user_override, "
            "claude_attempts, raw_full_json) values (?, 'x', ',', 'new', '2026-09-01 10:00:00', '2026-09-01 10:00:00', "
            "'2026-09-01 10:00:00', 0, 0, 0, 0, 0, 0, 0, ?)",
            (hh_id, raw),
        )
    con.commit()
    con.close()
    command.upgrade(cfg, "head")
    con = sqlite3.connect(settings.db_path)  # type: ignore[arg-type]
    rows = dict(con.execute("select hh_id, raw_full_json from vacancies").fetchall())
    con.close()
    assert json.loads(rows["1"]) == {k: v for k, v in card.items() if k != "branded_description"}
    assert rows["2"] == broken and json.loads(rows["3"]) == {"name": "без вёрстки"}


# ---- start / stop in the journal --------------------------------------------------------------------------------------


async def test_start_record_tells_how_long_the_panel_stood(runtime: Runtime) -> None:
    now = utcnow()
    async with runtime.session_factory() as session:
        first = await lifecycle.record_start(session, runtime.settings, "1.0", now=now)
        await session.commit()
    assert first.startswith("Панель запущена (версия 1.0)")

    async with runtime.session_factory() as session:
        await lifecycle.record_stop(session)
        await session.commit()
        clean = await lifecycle.record_start(
            session, runtime.settings, "1.0", now=utcnow() + timedelta(hours=3, minutes=12)
        )
        await session.commit()
    assert "остановлена штатно" in clean and "перерыв 3 ч 12 мин" in clean

    # a killed process leaves no stop record: the next start says so; the last activity is the newest run start
    async with runtime.session_factory() as session:
        session.add(JobRun(job="scan", started_at=utcnow() + timedelta(days=1), status="ok"))
        await session.commit()
        crashed = await lifecycle.record_start(
            session, runtime.settings, "1.1", now=utcnow() + timedelta(days=3, hours=2)
        )
        await session.commit()
    assert "без штатной остановки" in crashed and "перерыв 2 дн. 2 ч" in crashed


# ---- undelivered Telegram messages ----------------------------------------------------------------------------------


async def test_delivery_summary_counts_the_last_week(runtime: Runtime, api: httpx.AsyncClient) -> None:
    now = utcnow()
    async with runtime.session_factory() as session:
        session.add_all(
            [
                Notification(
                    channel="telegram",
                    event_type="digest",
                    payload_json="{}",
                    status="failed",
                    error="старое",
                    created_at=now - timedelta(days=9),
                ),
                Notification(
                    channel="telegram",
                    event_type="digest",
                    payload_json="{}",
                    status="failed",
                    error="Forbidden: bot was blocked",
                    created_at=now - timedelta(days=2),
                ),
                Notification(
                    channel="telegram",
                    event_type="letter",
                    payload_json="{}",
                    status="failed",
                    error="Bad Request: chat not found",
                    created_at=now - timedelta(hours=1),
                ),
                Notification(
                    channel="telegram", event_type="daily", payload_json="{}", status="queued", created_at=now
                ),
                Notification(channel="ui", event_type="daily", payload_json="{}", status="sent", created_at=now),
            ]
        )
        await session.commit()
    r = await api.get("/api/notifications/summary")
    assert r.status_code == 200
    data = r.json()
    assert (data["days"], data["failed"], data["queued"]) == (7, 2, 1)
    assert data["last_failed"]["label"] == "Вакансия с письмом"
    assert data["last_failed"]["error"] == "Bad Request: chat not found" and data["last_failed"]["created_at"]
