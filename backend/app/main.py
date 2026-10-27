"""FastAPI application: API under /api, built SPA from frontend/dist at /."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime

import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError
from starlette.middleware.base import RequestResponseEndpoint

from app import __version__
from app.api import (
    analytics,
    applications,
    auth,
    batches,
    board,
    channels,
    health,
    jobs,
    mail,
    maintenance,
    negotiations,
    notifications,
    probe,
    profile,
    searches,
    selection,
    sites,
    stats,
    studios,
    tenders,
    vacancies,
    web_login,
)
from app.api import settings as settings_api
from app.config import ROOT_DIR, Settings, describe_settings_error, get_settings
from app.exchange.watcher import import_once, watch_done_dir
from app.hh.errors import HHError
from app.runtime import Runtime
from app.scheduler import build_scheduler
from app.services.jobs import job_label
from app.web_access import is_public, local_gate, public_gate

log = structlog.get_logger("main")
FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"


def _report_bad_settings(exc: ValidationError) -> None:
    """The panel started hidden (desktop shortcut, autostart) has no console: the reason goes to logs/app.log, where
    `run.ps1 open` sends the user when the panel does not answer."""
    text = describe_settings_error(exc)
    try:
        sys.stderr.write(text + "\n")
    except Exception:  # noqa: BLE001 - no console at all
        pass
    try:
        logs = ROOT_DIR / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "app.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} settings_invalid\n{text}\n")
    except OSError:
        pass


async def startup_records(runtime: Runtime) -> None:
    """At start, before the scheduler and the start-up import: the «Панель запущена» record, then the repair of the file
    paths a move left in the base (services/relocate.py). In this order: record_start tells a clean stop by the newest
    record of its area, and the repair writes one of its own — otherwise the first start after a move reads «без
    штатной остановки»."""
    try:
        from app.services.lifecycle import record_start

        async with runtime.session_factory() as session:
            await record_start(session, runtime.settings, __version__)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - a journal record never blocks the start
        log.warning("start_record_failed", error=str(exc))
    try:
        from app.services.relocate import relocate_stored_paths

        # before the start-up import: a result written during a move needs its packet found on this machine
        async with runtime.session_factory() as session:
            await relocate_stored_paths(session, runtime.settings)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - a path repair never blocks the start
        log.warning("relocate_paths_failed", error=str(exc))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        settings = get_settings()
    except ValidationError as exc:
        _report_bad_settings(exc)
        raise
    runtime = await Runtime.create(settings)
    app.state.runtime = runtime
    await startup_records(runtime)
    scheduler = build_scheduler(runtime)
    scheduler.start()
    app.state.scheduler = scheduler
    try:
        imported = await import_once(runtime, require_stable=False)
        if imported:
            log.info("startup_import", files=[o["file"] for o in imported])
    except Exception as exc:  # noqa: BLE001
        log.warning("startup_import_failed", error=str(exc))
    stop = asyncio.Event()
    watcher = asyncio.create_task(watch_done_dir(runtime, stop), name="exchange-watcher")
    # token check and negotiations sync at start (no-ops when not logged in / method unavailable)
    from app.scheduler import auth_check, sync_tick

    async def _startup_checks() -> None:
        try:
            await auth_check(runtime)
        except Exception as exc:  # noqa: BLE001
            log.warning("startup_auth_check_failed", error=str(exc))
        await sync_tick(runtime)
        try:
            from app.services.backup import catch_up_backup

            await catch_up_backup(runtime)  # the nightly copy missed while the laptop slept or the panel was off
        except Exception as exc:  # noqa: BLE001
            log.warning("startup_backup_catch_up_failed", error=str(exc))

    startup = asyncio.create_task(_startup_checks(), name="startup-checks")
    log.info("app_started", version=__version__)
    try:
        yield
    finally:
        stop.set()
        scheduler.shutdown(wait=False)
        # a stop during the startup checks (a sync, a catch-up backup) must not leave them writing into a closed engine
        for task in (watcher, startup):
            task.cancel()
        await asyncio.gather(watcher, startup, return_exceptions=True)
        try:
            from app.services.lifecycle import record_stop

            async with runtime.session_factory() as session:
                await record_stop(session)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - nor stops the shutdown
            log.warning("stop_record_failed", error=str(exc))
        await runtime.close()
        log.info("app_stopped")


app = FastAPI(title="Job Search Panel", version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _settings_of(request: Request) -> Settings:
    runtime = getattr(request.app.state, "runtime", None)
    return runtime.settings if runtime is not None else get_settings()


@app.middleware("http")
async def access_gate(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Two entrances (app/web_access.py): the local one (the laptop, the SSH tunnel) as before; the public one (the
    reverse proxy of PANEL_PUBLIC_URL) only with a session from the bot's one-time link."""
    settings = _settings_of(request)
    if is_public(request.scope, settings):
        return await public_gate(request, call_next, settings)
    return await local_gate(request, call_next, settings)


app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(probe.router, prefix="/api")
app.include_router(settings_api.router, prefix="/api")
app.include_router(profile.router, prefix="/api")
app.include_router(searches.router, prefix="/api")
app.include_router(vacancies.router, prefix="/api")
app.include_router(board.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")
app.include_router(batches.router, prefix="/api")
app.include_router(applications.router, prefix="/api")
app.include_router(negotiations.router, prefix="/api")
app.include_router(stats.router, prefix="/api")
app.include_router(notifications.router, prefix="/api")
app.include_router(maintenance.router, prefix="/api")
app.include_router(selection.router, prefix="/api")
app.include_router(analytics.router, prefix="/api")
app.include_router(mail.router, prefix="/api")
app.include_router(channels.router, prefix="/api")
app.include_router(sites.router, prefix="/api")
app.include_router(studios.router, prefix="/api")
app.include_router(tenders.router, prefix="/api")
app.include_router(web_login.api_router, prefix="/api")
# the sign-in page of the public address: outside /api (a page, not the API) and before the SPA route below
app.include_router(web_login.router)


@app.exception_handler(HHError)
async def hh_error_handler(_request: object, exc: HHError) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={"detail": exc.summary(), "hh_status": exc.status, "errors": exc.errors, "kind": type(exc).__name__},
    )


@app.exception_handler(OverflowError)
async def overflow_handler(request: Request, exc: OverflowError) -> JSONResponse:
    """An id or a number past SQLite's 64-bit INTEGER (/api/vacancies/99999999999999999999) — such a row cannot
    exist; a 422 instead of a bare 500."""
    log.info("number_overflow", path=request.url.path, error=str(exc)[:200])
    return JSONResponse(status_code=422, content={"detail": "Число вне допустимого диапазона"})


@app.exception_handler(OperationalError)
async def db_error_handler(request: Request, exc: OperationalError) -> JSONResponse:
    """SQLite busy past its 30 s timeout: a friendly 503 naming the running jobs instead of a bare 500."""
    if "database is locked" not in str(exc):
        log.error("db_error", error=str(exc).splitlines()[0][:300])
        return JSONResponse(status_code=500, content={"detail": "Ошибка базы данных — подробности в logs/app.log"})
    runtime = getattr(request.app.state, "runtime", None)
    running = [job_label(j) for j in runtime.jobs.running_jobs()] if runtime is not None else []
    detail = "База данных занята фоновой задачей" + (f" ({', '.join(running)})" if running else "")
    log.warning("db_locked", path=request.url.path, running=running)
    return JSONResponse(status_code=503, content={"detail": detail + " — повторите через минуту", "kind": "db_locked"})


if FRONTEND_DIST.exists():
    assets = FRONTEND_DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        # `..`, `%2F..`, `..\` must never leave frontend/dist (the project root holds .env and data/panel.db)
        try:
            candidate = (FRONTEND_DIST / full_path).resolve()
            inside = candidate.is_relative_to(FRONTEND_DIST.resolve())
            is_file = inside and bool(full_path) and candidate.is_file()
        except (OSError, ValueError):  # NUL bytes, reserved device names and the like
            raise HTTPException(status_code=404) from None
        if not inside:
            raise HTTPException(status_code=404)
        if is_file:
            return FileResponse(candidate)
        # a missing file with an extension (favicon.ico, robots.txt) is a 404, not the SPA shell
        if "." in full_path.rsplit("/", 1)[-1]:
            raise HTTPException(status_code=404)
        return FileResponse(FRONTEND_DIST / "index.html")
