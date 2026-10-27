"""Candidate profile endpoints (M1)."""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.runtime import Runtime
from app.services import profile as svc
from app.services.preferences import Preferences
from app.services.state import auth_public_state, get_hh_auth

log = structlog.get_logger("api.profile")
router = APIRouter(prefix="/profile", tags=["profile"])


async def _logged_in(session: AsyncSession) -> bool:
    return bool(auth_public_state(await get_hh_auth(session))["logged_in"])


async def _view(runtime: Runtime, session: AsyncSession) -> dict[str, Any]:
    return await svc.profile_view(session, runtime.settings, hh_logged_in=await _logged_in(session))


def _raise(exc: svc.ProfileError) -> None:
    raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.get("")
async def get_profile(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    return await _view(runtime, session)


@router.get("/md", response_class=PlainTextResponse)
async def get_profile_md(session: Annotated[AsyncSession, Depends(get_session)]) -> str:
    active = await svc.active_profile(session)
    if active is None:
        raise HTTPException(status_code=404, detail="Профиль ещё не собран")
    return active.profile_md


@router.post("/rebuild")
async def rebuild(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    await svc.rebuild_profile(session, runtime.settings, force=True)
    await session.commit()
    return await _view(runtime, session)


@router.get("/preferences")
async def get_preferences(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    from app.services.preferences import get_preferences as _get

    return (await _get(session)).model_dump()


@router.put("/preferences")
async def put_preferences(
    body: Preferences,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    from app.services.preferences import criteria_of
    from app.services.preferences import get_preferences as _get
    from app.services.scanner import reprocess_after_criteria_change

    criteria_changed = criteria_of(await _get(session)) != criteria_of(body)
    await svc.update_preferences(session, runtime.settings, body)
    await session.commit()
    view = await _view(runtime, session)
    view["reprocess_started"] = criteria_changed and await reprocess_after_criteria_change(runtime)
    return view


class ImportRequest(BaseModel):
    resume_id: str


@router.post("/hh/list")
async def hh_list(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    if not await _logged_in(session):
        raise HTTPException(status_code=400, detail="Сначала войдите в hh.ru")
    try:
        items = await svc.hh_list_resumes(session, runtime.hh_client())
    except svc.ProfileError as exc:
        await session.commit()
        _raise(exc)
    await session.commit()
    return {"items": items}


@router.post("/hh/import")
async def hh_import(
    body: ImportRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    if not await _logged_in(session):
        raise HTTPException(status_code=400, detail="Сначала войдите в hh.ru")
    try:
        await svc.import_hh_resume(session, runtime.settings, runtime.hh_client(), body.resume_id)
    except svc.ProfileError as exc:
        await session.commit()
        _raise(exc)
    await svc.rebuild_profile(session, runtime.settings)
    await session.commit()
    return await _view(runtime, session)


@router.post("/resumes/upload")
async def upload_resume(
    file: Annotated[UploadFile, File(...)],
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
    make_primary: bool = True,
) -> dict[str, Any]:
    data = await file.read()
    try:
        await svc.upload_resume_file(
            session, runtime.settings, file.filename or "resume.txt", data, make_primary=make_primary
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await svc.rebuild_profile(session, runtime.settings)
    await session.commit()
    return await _view(runtime, session)


@router.put("/resumes/{resume_pk}/primary")
async def make_primary(
    resume_pk: int,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    try:
        await svc.set_primary(session, resume_pk)
    except svc.ProfileError as exc:
        _raise(exc)
    await svc.rebuild_profile(session, runtime.settings)
    await session.commit()
    return await _view(runtime, session)


@router.delete("/resumes/{resume_pk}")
async def delete_resume(
    resume_pk: int,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    try:
        await svc.delete_resume(session, resume_pk)
    except svc.ProfileError as exc:
        _raise(exc)
    await svc.rebuild_profile(session, runtime.settings)
    await session.commit()
    return await _view(runtime, session)
