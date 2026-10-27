"""Daily upkeep of panel.db after the nightly copy: routine rows of the journal, the event feed
and the job history older than JOURNAL_KEEP_DAYS go, and the file is compacted when much of it is free.

Kept whatever the age: journal records about a vacancy (`vacancy_id` in the context — the vacancy card shows them as its
history), notifications still waiting for Telegram, the last run of every job (the «Журнал» tab shows when a rare job —
the probe, a re-evaluation — ran last). Nothing here touches vacancies, applications, letters or posts.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.db.models import AuditLog, JobRun, Notification
from app.services.audit import VACANCY_KEY_LIKE, audit

log = structlog.get_logger("maintenance")

# VACUUM rewrites the whole file (a few seconds, the writers of the panel wait meanwhile): only when it pays off
VACUUM_MIN_FREE_BYTES = 16 * 1024 * 1024
VACUUM_MIN_FREE_SHARE = 0.2


async def prune_old_rows(session: AsyncSession, settings: Settings, *, now: datetime | None = None) -> dict[str, int]:
    """Deletes the routine rows older than JOURNAL_KEEP_DAYS (0 = keep everything). Returns the counts per table."""
    days = settings.journal_keep_days
    if days <= 0:
        return {}
    cutoff = (now or utcnow()) - timedelta(days=days)
    last_runs = select(func.max(JobRun.id)).group_by(JobRun.job).scalar_subquery()
    statements = {
        "audit_log": delete(AuditLog).where(
            AuditLog.ts < cutoff,
            or_(AuditLog.context_json.is_(None), AuditLog.context_json.not_like(VACANCY_KEY_LIKE)),
        ),
        "notifications": delete(Notification).where(Notification.created_at < cutoff, Notification.status != "queued"),
        "job_runs": delete(JobRun).where(
            JobRun.started_at < cutoff, JobRun.status != "running", JobRun.id.not_in(last_runs)
        ),
    }
    counts: dict[str, int] = {}
    for table, stmt in statements.items():
        result = await session.execute(stmt.execution_options(synchronize_session=False))
        counts[table] = int(getattr(result, "rowcount", 0) or 0)
    return counts


def db_space(path: Path) -> tuple[int, int]:
    """(file size, free bytes inside it) of an SQLite database, from its page counters."""
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        page_size = int(con.execute("PRAGMA page_size").fetchone()[0])
        pages = int(con.execute("PRAGMA page_count").fetchone()[0])
        free = int(con.execute("PRAGMA freelist_count").fetchone()[0])
    finally:
        con.close()
    return pages * page_size, free * page_size


def vacuum_worth_it(size: int, free: int) -> bool:
    return free >= VACUUM_MIN_FREE_BYTES and size > 0 and free / size >= VACUUM_MIN_FREE_SHARE


def _vacuum_sync(path: Path) -> None:
    # its own connection outside any transaction (VACUUM refuses to run inside one); WAL: the rewritten pages go through
    # the log, the checkpoint moves them back and truncates both files
    con = sqlite3.connect(path, timeout=60, isolation_level=None)
    try:
        con.execute("VACUUM")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()


async def run_maintenance(runtime: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """After a good nightly copy: prune, refresh the planner statistics, compact when worth it. Returns what was done
    (goes to the job's stats)."""
    settings: Settings = runtime.settings
    summary: dict[str, Any] = {}
    async with runtime.session_factory() as session:
        summary["deleted"] = await prune_old_rows(session, settings, now=now)
        await session.execute(text("PRAGMA optimize"))  # may write sqlite_stat1: inside the committed transaction
        await session.commit()
    path = settings.db_path
    if path is not None and path.exists():
        size, free = await asyncio.to_thread(db_space, path)
        summary["size_bytes"], summary["free_bytes"] = size, free
        busy = runtime.jobs.running_exclusive()
        if vacuum_worth_it(size, free) and busy is not None:
            summary["vacuum"] = f"отложен: идёт {busy}"
        elif vacuum_worth_it(size, free):
            await asyncio.to_thread(_vacuum_sync, path)
            summary["vacuum"] = "done"
            summary["size_after_bytes"] = (await asyncio.to_thread(db_space, path))[0]
    deleted = sum(summary["deleted"].values())
    if deleted or summary.get("vacuum") == "done":
        parts = []
        if deleted:
            d = summary["deleted"]
            parts.append(
                f"удалены старые записи: журнал {d['audit_log']}, лента {d['notifications']}, запуски {d['job_runs']}"
            )
        if summary.get("vacuum") == "done":
            parts.append(f"база сжата {_mb(summary['size_bytes'])} → {_mb(summary['size_after_bytes'])} МБ")
        async with runtime.session_factory() as session:
            await audit(session, "backup", "Обслуживание базы: " + "; ".join(parts))
            await session.commit()
    log.info("maintenance_done", **{k: v for k, v in summary.items() if k != "deleted"}, deleted=summary["deleted"])
    return summary


def _mb(value: int) -> str:
    return f"{value / 1024 / 1024:.1f}".replace(".", ",")
