"""Shared runtime objects (engine, http client, limiter, token store) for API, scheduler and CLI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.session import enable_wal, make_engine, make_session_factory, run_migrations
from app.hh.client import HHClient
from app.hh.errors import HHCaptchaRequired
from app.hh.ratelimit import TokenBucket
from app.logging_setup import setup_logging
from app.net import ssl_context
from app.services.audit import audit
from app.services.jobs import JobRunner, mark_stale_runs
from app.services.state import DbTokenStore, captcha_url_with_backurl, set_pause
from app.services.web_login import LoginAttempts

log = structlog.get_logger("runtime")


@dataclass
class Runtime:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    http: httpx.AsyncClient
    limiter: TokenBucket
    token_store: DbTokenStore
    jobs: JobRunner
    captcha_url: str | None = None
    # sign-in attempts of the public address (POST /login), per process
    login_attempts: LoginAttempts = field(default_factory=LoginAttempts, repr=False)
    _background: set[asyncio.Task[None]] = field(default_factory=set, repr=False)

    @classmethod
    async def create(
        cls, settings: Settings | None = None, *, attached: bool = False, log_file: str = "app.log"
    ) -> Runtime:
        """`attached=True` — a CLI command next to a running panel: the panel already migrated the database and owns
        the job runs, so no migrations, no «interrupted» marks on its running jobs, no rewrite of the guidelines."""
        settings = settings or get_settings()
        setup_logging(settings.log_level, settings.logs_dir, log_file)
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        (settings.data_dir / "uploads").mkdir(exist_ok=True)
        for sub in ("", "pending", "done", "imported", "rejected", "tmp"):
            (settings.exchange_dir / sub).mkdir(parents=True, exist_ok=True)
        if not attached:
            from app.exchange.guidelines_md import ensure_guidelines

            ensure_guidelines(settings)
            await run_migrations(settings)
        engine = make_engine(settings.db_url)
        await enable_wal(engine)
        session_factory = make_session_factory(engine)
        http = httpx.AsyncClient(timeout=httpx.Timeout(30.0), verify=ssl_context())
        limiter = TokenBucket(settings.hh_rps, burst=3)
        token_store = DbTokenStore(session_factory, settings, http)
        jobs = JobRunner(session_factory)
        jobs.on_repeated_failure = lambda job, error: _notify_job_error(session_factory, settings, job, error)
        if not attached:
            async with session_factory() as session:
                stale = await mark_stale_runs(session)
                await session.commit()
            if stale:
                log.warning("stale_job_runs_marked", count=stale)
        log.info("runtime_ready", db=settings.sync_db_url)
        return cls(settings, engine, session_factory, http, limiter, token_store, jobs)

    def hh_client(self) -> HHClient:
        factor = max(0.0, self.settings.hh_backoff_factor)

        async def sleep(seconds: float) -> None:
            if seconds * factor > 0:
                await asyncio.sleep(seconds * factor)

        return HHClient(
            self.settings,
            token_provider=self.token_store,
            http=self.http,
            limiter=self.limiter,
            on_captcha=self.on_captcha,
            sleep=sleep,
        )

    @property
    def captcha_backurl(self) -> str:
        return f"http://localhost:{self.settings.app_port}/api/auth/captcha-done"

    async def on_captcha(self, err: HHCaptchaRequired) -> None:
        """Called from inside HHClient. Never writes through the caller's session: the caller may hold the
        SQLite write lock, so the pause is kept in memory and persisted by a background task."""
        url = err.captcha_url or err.fallback_url
        full = captcha_url_with_backurl(url, self.captcha_backurl) if url else None
        self.captcha_url = full
        log.warning("captcha_required", url=full)
        task = asyncio.create_task(self.persist_captcha(full), name="persist-captcha")
        # the loop keeps only a weak reference to a task: without ours the pause may be collected before it is saved
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def persist_captcha(self, url: str | None) -> None:
        try:
            async with self.session_factory() as session:
                await set_pause(session, "captcha", url)
                await audit(session, "hh", "hh.ru требует капчу; фоновые задачи на паузе", level="warning", url=url)
                from app.services.notify import emit_event

                await emit_event(session, self.settings, "captcha", {"url": url})
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("captcha_persist_failed", error=str(exc))

    async def close(self) -> None:
        await self.jobs.shutdown()
        await self.http.aclose()
        await self.engine.dispose()


async def _notify_job_error(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings, job: str, error: str
) -> None:
    """Third consecutive failure of a job → `job_error` notification."""
    from app.services.jobs import FAILURES_TO_NOTIFY
    from app.services.notify import emit_event

    try:
        async with session_factory() as session:
            payload = {"job": job, "error": error, "failures": FAILURES_TO_NOTIFY}
            await emit_event(session, settings, "job_error", payload)
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("job_error_notify_failed", error=str(exc))
