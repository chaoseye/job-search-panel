"""Saved searches, dictionaries and suggests."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.db.models import SavedSearch
from app.hh.errors import HHError
from app.runtime import Runtime
from app.services import searches as svc
from app.services.dictionaries import search_form_options
from app.services.jobs import JobBusy
from app.services.scanner import run_scan
from app.services.state import auth_public_state, get_hh_auth, get_setting, get_system_state, read_capabilities

router = APIRouter(prefix="/searches", tags=["searches"])


def _suggest_items(items: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for i in items or []:
        if isinstance(i, dict) and i.get("id"):
            out.append({"id": str(i["id"]), "name": str(i.get("text") or i.get("name") or "")})
    return out


async def _get(session: AsyncSession, search_id: int) -> SavedSearch:
    row = await session.get(SavedSearch, search_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Поиск не найден")
    return row


@router.get("")
async def list_searches(session: Annotated[AsyncSession, Depends(get_session)]) -> list[dict[str, Any]]:
    from app.services.selection import profile_names

    names = await profile_names(session)
    return [svc.search_row(s, names) for s in await svc.list_searches(session)]


@router.post("")
async def create_search(
    body: svc.SearchInput, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    from app.services.selection import profile_names

    row = await svc.create_search(session, body)
    await session.commit()
    return svc.search_row(row, await profile_names(session))


@router.put("/{search_id}")
async def update_search(
    search_id: int, body: svc.SearchInput, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    from app.services.selection import profile_names

    row = await _get(session, search_id)
    await svc.update_search(session, row, body)
    await session.commit()
    return svc.search_row(row, await profile_names(session))


@router.delete("/{search_id}")
async def delete_search(search_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    row = await _get(session, search_id)
    await svc.delete_search(session, row)
    await session.commit()
    return {"ok": True}


@router.post("/{search_id}/run")
async def run_search(
    search_id: int,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    await _get(session, search_id)
    if not auth_public_state(await get_hh_auth(session))["logged_in"]:
        raise HTTPException(status_code=400, detail="Сначала войдите в hh.ru")
    try:
        run_id = await runtime.jobs.start("scan", lambda: run_scan(runtime, search_ids=[search_id]))
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_run_id": run_id}


class PreviewRequest(BaseModel):
    params: svc.SearchParams
    source: str = "search"
    # hh pages of the preview: the form shows the first ten, scripts may read more
    page: int = Field(0, ge=0, le=19)
    per_page: int = Field(10, ge=1, le=100)


@router.post("/preview")
async def preview_search(
    body: PreviewRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    if not auth_public_state(await get_hh_auth(session))["logged_in"]:
        raise HTTPException(status_code=400, detail="Сначала войдите в hh.ru")
    path = "/vacancies"
    if body.source == "similar_to_resume":
        caps = read_capabilities(await get_system_state(session))
        if caps.get("resume_similar") == "unavailable":
            raise HTTPException(status_code=409, detail="Подходящие вакансии под резюме недоступны по данным зонда")
        resume_id = await get_setting(session, "manual_resume_id")
        if not resume_id:
            raise HTTPException(status_code=400, detail="Сначала импортируйте резюме из hh.ru (Профиль)")
        path = f"/resumes/{resume_id}/similar_vacancies"
    try:
        return await svc.preview(runtime.hh_client(), body.params, path=path, page=body.page, per_page=body.per_page)
    except HHError as exc:
        raise HTTPException(status_code=502, detail=f"hh.ru: {exc.summary()}") from exc


@router.get("/options")
async def options(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    try:
        data = await search_form_options(session, runtime.hh_client(), runtime.settings.dictionaries_ttl_days)
    except HHError as exc:
        raise HTTPException(status_code=502, detail=f"Справочники hh.ru недоступны: {exc.summary()}") from exc
    caps = read_capabilities(await get_system_state(session))
    data["similar_available"] = caps.get("resume_similar") == "available"
    data["resume_id"] = await get_setting(session, "manual_resume_id")
    await session.commit()
    return data


@router.get("/suggest/areas")
async def suggest_areas(
    text: Annotated[str, Query(min_length=2)],
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> list[dict[str, str]]:
    try:
        data = await runtime.hh_client().public("/suggests/areas", params={"text": text})
    except HHError as exc:
        raise HTTPException(status_code=502, detail=f"hh.ru: {exc.summary()}") from exc
    items = data.get("items") if isinstance(data, dict) else []
    return _suggest_items(items)


@router.get("/suggest/professional_roles")
async def suggest_roles(
    text: Annotated[str, Query(min_length=2)],
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> list[dict[str, str]]:
    try:
        data = await runtime.hh_client().public("/suggests/professional_roles", params={"text": text})
    except HHError as exc:
        raise HTTPException(status_code=502, detail=f"hh.ru: {exc.summary()}") from exc
    items = data.get("items") if isinstance(data, dict) else []
    return _suggest_items(items)
