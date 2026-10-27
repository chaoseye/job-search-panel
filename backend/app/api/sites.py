"""Career sites of chosen companies: the site list, sync (job `sites_sync`), the vacancy feed."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.runtime import Runtime
from app.services import company_sites as svc
from app.services.company_sites import SiteError
from app.services.jobs import JobBusy

router = APIRouter(prefix="/sites", tags=["sites"])


class SiteUpdate(BaseModel):
    enabled: bool | None = None
    notify: bool | None = None
    keywords: str | None = None


class MarkSeenRequest(BaseModel):
    ids: list[int] | None = None
    site_key: str | None = None


async def _run_sync(runtime: Runtime, site_key: str | None = None) -> None:
    try:
        await runtime.jobs.start("sites_sync", lambda: svc.sync_sites(runtime, site_key=site_key))
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail="Проверка сайтов уже выполняется") from exc
    await runtime.jobs.wait("sites_sync")


@router.get("")
async def list_sites(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    items = await svc.list_sites(session)
    await session.commit()  # ensure_sites may create rows for new adapters
    return {
        "items": items,
        "interval_min": runtime.settings.sites_sync_min,
        "cities": svc.cities(runtime.settings),
        "unsupported": svc.UNSUPPORTED,
        "running": runtime.jobs.is_running("sites_sync"),
    }


@router.get("/summary")
async def sites_summary(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    data = await svc.summary(session, runtime.settings)
    await session.commit()
    return data


@router.post("/sync")
async def sync_all(runtime: Annotated[Runtime, Depends(get_runtime)]) -> dict[str, Any]:
    """Reads every enabled site right now (waits for the job) and returns the site list."""
    await _run_sync(runtime)
    async with runtime.session_factory() as session:
        return {"items": await svc.list_sites(session)}


@router.get("/vacancies")
async def vacancies(
    session: Annotated[AsyncSession, Depends(get_session)],
    site: str | None = None,
    only_new: bool = False,
    only_match: bool = True,
    include_hidden: bool = False,
    include_gone: bool = False,
    q: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    return await svc.list_vacancies(
        session,
        site_key=site,
        only_new=only_new,
        only_match=only_match,
        include_hidden=include_hidden,
        include_gone=include_gone,
        q=q,
        page=page,
        per_page=per_page,
    )


@router.post("/vacancies/mark-seen")
async def vacancies_mark_seen(
    body: MarkSeenRequest, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    count = await svc.mark_seen(session, ids=body.ids, site_key=body.site_key)
    await session.commit()
    return {"marked": count}


@router.post("/vacancies/{vacancy_id}/{action}")
async def vacancy_action(
    vacancy_id: int, action: str, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    """seen | hide | restore (hidden → seen)."""
    status = {"seen": "seen", "hide": "hidden", "restore": "seen"}.get(action)
    if status is None:
        raise HTTPException(status_code=404, detail="неизвестное действие")
    try:
        row = await svc.get_vacancy(session, vacancy_id)
        await svc.set_status(session, row, status)
    except SiteError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return svc.vacancy_row(row)


@router.put("/{key}")
async def update_site(
    key: str,
    body: SiteUpdate,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    try:
        site = await svc.get_site(session, key)
    except SiteError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await svc.update_site(session, runtime.settings, site, **body.model_dump())
    await session.commit()
    return next(s for s in await svc.list_sites(session) if s["key"] == key)


@router.post("/{key}/sync")
async def sync_one(key: str, runtime: Annotated[Runtime, Depends(get_runtime)]) -> dict[str, Any]:
    """Reads one site right now (waits for the job) and returns its row."""
    async with runtime.session_factory() as session:
        try:
            site = await svc.get_site(session, key)
        except SiteError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not site.enabled:
            raise HTTPException(status_code=409, detail="Сайт отключён — включите его, чтобы проверять")
        await session.commit()
    await _run_sync(runtime, key)
    async with runtime.session_factory() as session:
        return next(s for s in await svc.list_sites(session) if s["key"] == key)
