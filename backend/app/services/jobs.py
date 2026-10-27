"""In-process background jobs with job_runs bookkeeping and per-job mutual exclusion."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import as_utc, utcnow
from app.db.models import JobRun

log = structlog.get_logger("jobs")

JobFn = Callable[[], Awaitable[dict[str, Any]]]
FailureHook = Callable[[str, str], Awaitable[None]]
FAILURES_TO_NOTIFY = 3

JOB_LABELS: dict[str, str] = {
    "scan": "сканирование",
    "reprocess": "переоценка по критериям",
    "freshness": "проверка закрытых вакансий",
    "sync": "синхронизация откликов",
    "apply": "отправка откликов",
    "claude_run": "разбор пакетов (claude -p)",
    "channel_sync": "проверка Telegram-каналов",
    "sites_sync": "проверка сайтов компаний",
    "studios_find": "поиск студий",
    "studio_contacts": "контакты студий",
    "ws_tenders_sync": "проверка тендеров workspace.ru",
    "mail_sync": "чтение писем hh.ru",
    "backup": "резервная копия",
    "probe": "зонд hh.ru",
}
# Jobs that rewrite the same vacancy rows and call hh.ru: at most one of them at a time, whoever starts them (tick,
# button, criteria change). SQLite has one writer — a scan started by the button during a re-evaluation would fail
# with «database is locked».
EXCLUSIVE_GROUP = frozenset({"scan", "reprocess", "freshness", "sync"})
# a job body that finished its loop but achieved nothing (every search failed, hh.ru demanded a captcha) returns its
# stats with this key: the run is recorded as an error with the stats kept, and FAILURES_TO_NOTIFY such runs in a row
# reach Telegram (`job_error`) — before, a scan against an unreachable hh.ru was «ok» forever
FAILED_KEY = "job_failed"


def job_label(job: str) -> str:
    return JOB_LABELS.get(job, job)


class JobBusy(Exception):
    """The job itself or a conflicting one is running; str(exc) is a message for the user."""

    def __init__(self, job: str, running: str | None = None) -> None:
        self.job = job
        self.running = running or job
        if self.running == job:
            message = f"Уже выполняется: {job_label(job)}"
        else:
            message = (
                f"Сейчас выполняется задача «{job_label(self.running)}» — "
                f"запустите «{job_label(job)}» после её окончания"
            )
        super().__init__(message)


class JobRunner:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # jobs between the busy check and their task creation (start() awaits the job_runs insert in between)
        self._reserved: set[str] = set()
        self.failures: dict[str, int] = {}
        # called once when a job fails FAILURES_TO_NOTIFY times in a row (set by Runtime → Telegram `job_error`)
        self.on_repeated_failure: FailureHook | None = None

    def is_running(self, job: str) -> bool:
        task = self._tasks.get(job)
        return task is not None and not task.done()

    def running_jobs(self) -> list[str]:
        return [name for name, task in self._tasks.items() if not task.done()]

    def _busy(self, job: str) -> bool:
        return self.is_running(job) or job in self._reserved

    def conflicting(self, job: str) -> str | None:
        """The running job that blocks `job`: itself, or another one of EXCLUSIVE_GROUP."""
        if self._busy(job):
            return job
        if job in EXCLUSIVE_GROUP:
            for other in sorted(EXCLUSIVE_GROUP - {job}):
                if self._busy(other):
                    return other
        return None

    def running_exclusive(self) -> str | None:
        """Any running job of EXCLUSIVE_GROUP (for actions that rewrite vacancies outside a job)."""
        return next((job for job in sorted(EXCLUSIVE_GROUP) if self._busy(job)), None)

    async def start(self, job: str, fn: JobFn) -> int:
        busy = self.conflicting(job)
        if busy is not None:
            raise JobBusy(job, busy)
        self._reserved.add(job)
        try:
            async with self._sf() as session:
                run = JobRun(job=job, status="running")
                session.add(run)
                await session.commit()
                run_id = run.id
            self._tasks[job] = asyncio.create_task(self._run_background(job, run_id, fn), name=f"job:{job}:{run_id}")
        finally:
            self._reserved.discard(job)
        return run_id

    async def _run_background(self, job: str, run_id: int, fn: JobFn) -> None:
        await self._execute(job, run_id, fn)

    async def run_inline(self, job: str, fn: JobFn) -> dict[str, Any]:
        """Runs the job in the current task (CLI/tests) with the same bookkeeping."""
        busy = self.conflicting(job)
        if busy is not None:
            raise JobBusy(job, busy)
        async with self._sf() as session:
            run = JobRun(job=job, status="running")
            session.add(run)
            await session.commit()
            run_id = run.id
        return await self._execute(job, run_id, fn, return_stats=True) or {}

    async def _execute(self, job: str, run_id: int, fn: JobFn, *, return_stats: bool = False) -> dict[str, Any] | None:
        stats: dict[str, Any] = {}
        error: str | None = None
        try:
            stats = await fn() or {}
            if stats.get(FAILED_KEY):
                error = str(stats[FAILED_KEY])
                log.warning("job_failed_softly", job=job, run_id=run_id, error=error)
        except asyncio.CancelledError:
            error = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - job failures are recorded, never propagated
            error = f"{type(exc).__name__}: {exc}"
            log.exception("job_failed", job=job, run_id=run_id)
        finally:
            async with self._sf() as session:
                run = await session.get(JobRun, run_id)
                if run is not None:
                    run.finished_at = utcnow()
                    run.status = "error" if error else "ok"
                    run.stats_json = json.dumps(stats, ensure_ascii=False, default=str) if stats else None
                    run.error = error
                    await session.commit()
            log.info("job_finished", job=job, run_id=run_id, status="error" if error else "ok", stats=stats)
            await self._track_failure(job, error)
        return stats if return_stats else None

    async def _track_failure(self, job: str, error: str | None) -> None:
        if error is None:
            self.failures[job] = 0
            return
        if error == "cancelled":
            return
        self.failures[job] = self.failures.get(job, 0) + 1
        if self.failures[job] == FAILURES_TO_NOTIFY and self.on_repeated_failure is not None:
            try:
                await self.on_repeated_failure(job, error)
            except Exception as exc:  # noqa: BLE001
                log.warning("failure_hook_failed", job=job, error=str(exc))

    async def wait(self, job: str) -> None:
        task = self._tasks.get(job)
        if task is not None:
            await asyncio.shield(task)

    async def shutdown(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()


def job_run_row(run: JobRun) -> dict[str, Any]:
    started = as_utc(run.started_at)
    finished = as_utc(run.finished_at)
    return {
        "id": run.id,
        "job": run.job,
        "status": run.status,
        "started_at": started.isoformat(timespec="seconds") if started else None,
        "finished_at": finished.isoformat(timespec="seconds") if finished else None,
        "duration_sec": int((finished - started).total_seconds()) if started and finished else None,
        "stats": json.loads(run.stats_json) if run.stats_json else None,
        "error": run.error,
    }


async def recent_job_runs(session: AsyncSession, limit: int = 20) -> list[JobRun]:
    rows = await session.execute(select(JobRun).order_by(desc(JobRun.id)).limit(limit))
    return list(rows.scalars().all())


async def mark_stale_runs(session: AsyncSession) -> int:
    """On startup: runs left in `running` by a previous process are marked as interrupted."""
    rows = (await session.execute(select(JobRun).where(JobRun.status == "running"))).scalars().all()
    for run in rows:
        run.status = "error"
        run.error = "interrupted (process restarted)"
        run.finished_at = utcnow()
    return len(rows)
