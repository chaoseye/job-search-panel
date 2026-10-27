"""Backups of panel.db: daily copy into data/backups/, keep the last N, manual «Сделать копию».

Uses the SQLite online backup API, which is safe with WAL and a running server (no file copy of a live database).
Every copy passes `PRAGMA integrity_check` before it counts: the backup API copies pages as they are, so a damaged
base gives a damaged copy, and fourteen of those would push the last good one out of the rotation.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from app.config import Settings
from app.db.base import utcnow

log = structlog.get_logger("backup")
PREFIX = "panel_"
SUFFIX = ".db"
# the daily copy is considered missed after this long (BACKUP_AT is once a day; a little slack for a late run)
CATCH_UP_AFTER = timedelta(hours=26)
# a copy that failed the check is kept under this suffix for a look (only the newest: one is a full base) and is
# neither listed nor counted by the rotation
BAD_SUFFIX = ".bad"
INTEGRITY_MAX_ERRORS = 20


class BackupError(Exception):
    pass


class BackupCorrupt(BackupError):
    """The copy was made but failed `PRAGMA integrity_check` — most likely the base itself is damaged."""


def integrity_errors(con: sqlite3.Connection) -> list[str]:
    """`PRAGMA integrity_check` messages; empty when the database is sound."""
    rows = [str(r[0]) for r in con.execute(f"PRAGMA integrity_check({INTEGRITY_MAX_ERRORS})").fetchall()]
    return [] if rows == ["ok"] else rows


def _backup_sync(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        target = sqlite3.connect(tmp)
        try:
            source.backup(target)
            errors = integrity_errors(target)
        finally:
            target.close()
    finally:
        source.close()
    if errors:
        for old in dest.parent.glob(f"{PREFIX}*{SUFFIX}{BAD_SUFFIX}"):
            old.unlink(missing_ok=True)
        bad = dest.with_name(dest.name + BAD_SUFFIX)
        tmp.replace(bad)
        raise BackupCorrupt(
            f"копия {bad.name} не прошла проверку целостности ({len(errors)}): " + "; ".join(errors[:3])[:500]
        )
    tmp.replace(dest)


def list_backups(settings: Settings) -> list[dict[str, Any]]:
    folder = settings.backups_dir
    if not folder.exists():
        return []
    items = []
    for path in sorted(folder.glob(f"{PREFIX}*{SUFFIX}"), reverse=True):
        stat = path.stat()
        items.append(
            {
                "name": path.name,
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
            }
        )
    return items


def prune_backups(settings: Settings, keep: int | None = None) -> list[str]:
    keep = settings.backup_keep if keep is None else keep
    folder = settings.backups_dir
    if not folder.exists() or keep < 0:
        return []
    files = sorted(folder.glob(f"{PREFIX}*{SUFFIX}"), reverse=True)
    removed = []
    for path in files[keep:]:
        try:
            path.unlink()
            removed.append(path.name)
        except OSError as exc:
            log.warning("backup_prune_failed", file=path.name, error=str(exc))
    return removed


async def make_backup(settings: Settings, *, now: datetime | None = None) -> dict[str, Any]:
    """Creates data/backups/panel_YYYYMMDD_HHMMSS.db and prunes old copies. Returns the new entry + removed names."""
    src = settings.db_path
    if src is None:
        raise BackupError("резервное копирование поддерживается только для SQLite (DATABASE_URL)")
    if not src.exists():
        raise BackupError(f"файл базы не найден: {src}")
    stamp = (now or utcnow()).astimezone().strftime("%Y%m%d_%H%M%S")
    dest = settings.backups_dir / f"{PREFIX}{stamp}{SUFFIX}"
    counter = 1
    while dest.exists():
        counter += 1
        dest = settings.backups_dir / f"{PREFIX}{stamp}_{counter}{SUFFIX}"
    try:
        await asyncio.to_thread(_backup_sync, src, dest)
    except sqlite3.Error as exc:
        dest.with_suffix(".tmp").unlink(missing_ok=True)
        raise BackupError(f"не удалось сделать копию: {exc}") from exc
    removed = prune_backups(settings)
    size = dest.stat().st_size
    log.info("backup_created", file=dest.name, size=size, removed=removed)
    return {"name": dest.name, "size_bytes": size, "path": str(dest), "removed": removed}


def last_backup_at(settings: Settings) -> datetime | None:
    """Time of the newest copy in data/backups/ (UTC), None when there is none."""
    folder = settings.backups_dir
    files = list(folder.glob(f"{PREFIX}*{SUFFIX}")) if folder.exists() else []
    if not files:
        return None
    return datetime.fromtimestamp(max(p.stat().st_mtime for p in files), tz=UTC)


async def catch_up_backup(runtime: Any, *, now: datetime | None = None) -> bool:
    """At start: the daily copy is overdue (the laptop slept through BACKUP_AT, or the panel was not running then) →
    make it now. Returns True when a backup job was started."""
    from app.services.jobs import JobBusy

    if runtime.settings.db_path is None:
        return False
    last = last_backup_at(runtime.settings)
    if last is not None and (now or utcnow()) - last < CATCH_UP_AFTER:
        return False
    try:
        await runtime.jobs.start("backup", lambda: daily_backup_job(runtime))
    except JobBusy:
        return False
    log.info("backup_catch_up", last=last.isoformat() if last else None)
    return True


async def backup_job(runtime: Any) -> dict[str, Any]:
    """The copy + an audit record (button «Сделать копию», CLI `backup`). A copy that fails the integrity check
    reaches Telegram at once (`job_error`): waiting for three failed nights would let the rotation eat good copies."""
    from app.services.audit import audit

    try:
        result = await make_backup(runtime.settings)
    except BackupCorrupt as exc:
        await _report_corrupt(runtime, str(exc))
        raise
    async with runtime.session_factory() as session:
        await audit(session, "backup", f"Резервная копия базы: {result['name']} ({result['size_bytes'] // 1024} КБ)")
        await session.commit()
    return result


async def daily_backup_job(runtime: Any) -> dict[str, Any]:
    """The nightly job (BACKUP_AT, or the catch-up at start): the copy, then the upkeep of the base — only after a copy
    that passed the check (services/maintenance.py)."""
    from app.services.maintenance import run_maintenance

    result = await backup_job(runtime)
    result["maintenance"] = await run_maintenance(runtime)
    return result


async def _report_corrupt(runtime: Any, error: str) -> None:
    from app.services.audit import audit
    from app.services.notify import emit_event

    log.error("backup_corrupt", error=error)
    try:
        async with runtime.session_factory() as session:
            await audit(session, "backup", f"Резервная копия повреждена: {error}", level="error")
            await emit_event(session, runtime.settings, "job_error", {"job": "backup", "error": error, "failures": 1})
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - the backup error itself is what the job reports
        log.warning("backup_corrupt_report_failed", error=str(exc))
