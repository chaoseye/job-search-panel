"""Named selection-criteria sets (several questionnaires that differ only in vacancy selection)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.runtime import Runtime
from app.services import selection as svc
from app.services.preferences import CRITERIA_FIELDS

router = APIRouter(prefix="/selection-profiles", tags=["selection"])


class ProfileCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    criteria: dict[str, Any] | None = None
    copy_from: int | None = None


class ProfileUpdate(BaseModel):
    name: str | None = None
    criteria: dict[str, Any] | None = None
    # own resume for the set: an id, or 0 to go back to the primary resume
    resume_id: int | None = None


def _raise(exc: svc.SelectionError) -> None:
    raise HTTPException(status_code=exc.status, detail=str(exc))


@router.get("")
async def list_profiles(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    rows = await svc.list_profiles(session)
    return {"items": [await svc.profile_row(session, r) for r in rows], "criteria_fields": list(CRITERIA_FIELDS)}


@router.post("")
async def create_profile(body: ProfileCreate, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    try:
        row = await svc.create_profile(session, body.name, body.criteria, copy_from=body.copy_from)
    except svc.SelectionError as exc:
        _raise(exc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await session.commit()
    return await svc.profile_row(session, row)


@router.put("/{pk}")
async def update_profile(
    pk: int,
    body: ProfileUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    runtime: Annotated[Runtime, Depends(get_runtime)],
) -> dict[str, Any]:
    try:
        row = await svc.get_profile(session, pk)
        await svc.update_profile(
            session,
            row,
            name=body.name,
            criteria=body.criteria,
            resume_id=body.resume_id or None,
            clear_resume=body.resume_id == 0,
        )
    except svc.SelectionError as exc:
        _raise(exc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.criteria:
        from app.services.scanner import reprocess_after_criteria_change

        await session.commit()
        await reprocess_after_criteria_change(runtime, profile_id=pk)
    if body.resume_id is not None:
        from app.services.profile import rebuild_profile

        await rebuild_profile(session, runtime.settings)
    await session.commit()
    return await svc.profile_row(session, row)


@router.delete("/{pk}")
async def delete_profile(pk: int, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    try:
        row = await svc.get_profile(session, pk)
    except svc.SelectionError as exc:
        _raise(exc)
    result = await svc.delete_profile(session, row)
    await session.commit()
    return {"ok": True, **result}
