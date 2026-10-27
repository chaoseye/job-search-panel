"""Run the capability probe and read the last report."""

from __future__ import annotations

from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.hh.probe import ProbeOptions
from app.runtime import Runtime
from app.services.probe_runner import run_and_store_probe
from app.services.state import auth_public_state, get_hh_auth, get_setting

log = structlog.get_logger("api.probe")
router = APIRouter(prefix="/probe", tags=["probe"])


class ProbeRequest(BaseModel):
    allow_publish: bool = False
    allow_blacklist: bool = False
    resume_id: str | None = None


@router.post("/run")
async def probe_run(
    body: ProbeRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    row = await get_hh_auth(session)
    if not auth_public_state(row)["logged_in"]:
        raise HTTPException(status_code=400, detail="Сначала войдите в hh.ru")

    resume_id = body.resume_id or await get_setting(session, "manual_resume_id")
    options = ProbeOptions(
        allow_publish=body.allow_publish,
        allow_blacklist=body.allow_blacklist,
        resume_id=resume_id or None,
    )
    report_dict, _before = await run_and_store_probe(runtime, session, options, manual_resume_id=body.resume_id)
    await session.commit()
    return report_dict


@router.get("/report")
async def probe_report(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any] | None:
    report = await get_setting(session, "probe_report")
    return report if isinstance(report, dict) else None
