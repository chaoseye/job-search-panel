"""The board «Воронка»: the columns of a pipeline (vacancies, tenders of workspace.ru, studios) and one column opened as
a list."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.runtime import Runtime
from app.services import board as vacancies
from app.services import board_studios as studios
from app.services import board_tenders as tenders

router = APIRouter(prefix="/board", tags=["board"])

Source = Literal["vacancies", "tenders", "studios"]
# the columns of each pipeline and the bands of its first column
SHAPE: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "vacancies": (vacancies.COLUMN_KEYS, vacancies.BANDS),
    "tenders": (tenders.COLUMN_KEYS, tenders.BANDS),
    "studios": (studios.COLUMN_KEYS, studios.BANDS),
}


@router.get("")
async def board(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
    source: Source = "vacancies",
) -> dict[str, Any]:
    if source == "tenders":
        return await tenders.board_view(session, runtime.settings)
    if source == "studios":
        return await studios.board_view(session, runtime.settings)
    return await vacancies.board_view(session, runtime.settings)


@router.get("/column/{key}")
async def column(
    key: str,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
    source: Source = "vacancies",
    band: str | None = Query(None, description="только для первой колонки: группы её источника"),
    offset: int = Query(0, ge=0),
    limit: int = Query(60, ge=1, le=200),
) -> dict[str, Any]:
    keys, bands = SHAPE[source]
    if key not in keys:
        raise HTTPException(status_code=422, detail=f"Неизвестная колонка: {key}")
    if band is not None and (key != "new" or band not in bands):
        raise HTTPException(status_code=400, detail=f"Неизвестная группа колонки: {band}")
    if source == "tenders":
        return await tenders.column_items(session, runtime.settings, key, band=band, offset=offset, limit=limit)
    if source == "studios":
        return await studios.column_items(session, runtime.settings, key, band=band, offset=offset, limit=limit)
    return await vacancies.column_items(session, runtime.settings, key, band=band, offset=offset, limit=limit)
