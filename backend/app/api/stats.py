"""Statistics and CSV export."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.runtime import Runtime
from app.services.stats import PERIODS, compute_stats, export_aggregates_csv, export_applications_csv

router = APIRouter(prefix="/stats", tags=["stats"])


def _check_period(period: str, date_from: str | None) -> None:
    if period not in PERIODS:
        raise HTTPException(status_code=400, detail=f"period должен быть одним из {', '.join(PERIODS)}")
    if period == "custom" and not date_from:
        raise HTTPException(status_code=400, detail="для period=custom нужен date_from")


@router.get("")
async def stats(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
    period: str = Query("7d"),
    date_from: str | None = None,
    date_to: str | None = None,
    no_answer_days: int = Query(7, ge=1, le=90),
) -> dict[str, Any]:
    _check_period(period, date_from)
    try:
        return await compute_stats(
            session,
            runtime.settings,
            period_key=period,
            date_from=date_from,
            date_to=date_to,
            no_answer_days=no_answer_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"неверная дата: {exc}") from exc


@router.get("/export")
async def export(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
    kind: str = Query("applications", pattern="^(applications|aggregates)$"),
    period: str = Query("all"),
    date_from: str | None = None,
    date_to: str | None = None,
) -> Response:
    _check_period(period, date_from)
    fn = export_applications_csv if kind == "applications" else export_aggregates_csv
    try:
        text = await fn(session, runtime.settings, period_key=period, date_from=date_from, date_to=date_to)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"неверная дата: {exc}") from exc
    filename = f"hh_{kind}_{period}.csv"
    return Response(
        content=text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
