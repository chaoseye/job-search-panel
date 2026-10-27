"""Background jobs: list runs, trigger scan / sync."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.db.base import as_utc
from app.runtime import Runtime
from app.services.jobs import JobBusy, job_run_row, recent_job_runs
from app.services.scanner import run_scan
from app.services.state import auth_public_state, get_hh_auth, get_system_state

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("")
async def list_jobs(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 20,
) -> dict[str, Any]:
    runs = await recent_job_runs(session, min(limit, 200))
    state = await get_system_state(session)
    last_scan = as_utc(state.last_scan_at)
    last_sync = as_utc(state.last_sync_at)
    return {
        "running": runtime.jobs.running_jobs(),
        "runs": [job_run_row(r) for r in runs],
        "last_scan_at": last_scan.isoformat(timespec="seconds") if last_scan else None,
        "last_sync_at": last_sync.isoformat(timespec="seconds") if last_sync else None,
        "paused_reason": state.paused_reason,
        "failures": dict(runtime.jobs.failures),
    }


@router.post("/scan")
async def start_scan(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    if not auth_public_state(await get_hh_auth(session))["logged_in"]:
        raise HTTPException(status_code=400, detail="Сначала войдите в hh.ru")
    try:
        run_id = await runtime.jobs.start("scan", lambda: run_scan(runtime))
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_run_id": run_id}


@router.post("/freshness")
async def start_freshness(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
    stale_days: int | None = Query(None, ge=0, le=365, description="0 = перепроверить всех кандидатов"),
) -> dict[str, Any]:
    """Re-checks stale candidates on hh.ru right now (the daily job `freshness`)."""
    from app.services.freshness import check_closed

    if not auth_public_state(await get_hh_auth(session))["logged_in"]:
        raise HTTPException(status_code=400, detail="Сначала войдите в hh.ru")
    try:
        run_id = await runtime.jobs.start("freshness", lambda: check_closed(runtime, stale_days=stale_days))
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_run_id": run_id}


@router.post("/sync")
async def start_sync(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
    full: bool = Query(False, description="полный проход status=all (иначе инкремент от last_sync_at)"),
) -> dict[str, Any]:
    from app.services.sync import run_sync

    if not auth_public_state(await get_hh_auth(session))["logged_in"]:
        raise HTTPException(status_code=400, detail="Сначала войдите в hh.ru")
    try:
        run_id = await runtime.jobs.start("sync", lambda: run_sync(runtime, full=full))
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_run_id": run_id}
