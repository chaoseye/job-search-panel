"""M6 maintenance endpoints: backups, pause/resume, local `claude -p` run, resume publish."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.db.models import Resume
from app.runtime import Runtime
from app.services.audit import audit
from app.services.backup import BackupError, list_backups, make_backup
from app.services.claude_cli import ClaudeCliError, autorun_status, run_claude_batch
from app.services.jobs import JobBusy
from app.services.profile import ProfileError, resume_row
from app.services.resume_publish import publish_allowed, publish_resume
from app.services.state import auth_public_state, get_hh_auth, get_system_state, set_pause

router = APIRouter(tags=["maintenance"])


# ---- backups (F-8.12.4) -----------------------------------------------------------------


@router.get("/backups")
async def backups(runtime: Annotated[Runtime, Depends(get_runtime)]) -> dict[str, Any]:
    s = runtime.settings
    return {
        "items": list_backups(s),
        "dir": str(s.backups_dir),
        "keep": s.backup_keep,
        "daily_at": s.backup_at,
        "db_path": str(s.db_path) if s.db_path else None,
    }


@router.post("/backups")
async def create_backup(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    try:
        result = await make_backup(runtime.settings)
    except BackupError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    await audit(session, "backup", f"Резервная копия по кнопке: {result['name']}")
    await session.commit()
    return result


# ---- pause / resume (F-8.12.1) -------------------------------------------------------------


class PauseRequest(BaseModel):
    reason: str | None = "user"


@router.post("/jobs/pause")
async def pause_jobs(body: PauseRequest, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    reason = (body.reason or "user").strip()[:50] or "user"
    await set_pause(session, reason)
    await audit(session, "jobs", f"Фоновые задачи поставлены на паузу ({reason})")
    await session.commit()
    return {"paused_reason": reason}


@router.post("/jobs/resume")
async def resume_jobs(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    state = await get_system_state(session)
    was = state.paused_reason
    await set_pause(session, None)
    runtime.captcha_url = None
    await audit(session, "jobs", f"Пауза снята (была: {was or 'нет'})")
    await session.commit()
    return {"paused_reason": None}


# ---- claude -p autorun (F-9.6.1) -----------------------------------------------------------


@router.get("/claude/status")
async def claude_status(runtime: Annotated[Runtime, Depends(get_runtime)]) -> dict[str, Any]:
    data = autorun_status(runtime.settings)
    data["job_running"] = runtime.jobs.is_running("claude_run")
    return data


class ClaudeRunRequest(BaseModel):
    packet: str | None = None


@router.post("/claude/run")
async def claude_run(body: ClaudeRunRequest, runtime: Annotated[Runtime, Depends(get_runtime)]) -> dict[str, Any]:
    status = autorun_status(runtime.settings)
    if not status["enabled"]:
        raise HTTPException(status_code=409, detail="Автозапуск выключен: задайте CLAUDE_CLI_AUTORUN=true в .env")
    if not status["executable"]:
        raise HTTPException(status_code=409, detail="Команда `claude` не найдена в PATH")
    try:
        run_id = await runtime.jobs.start("claude_run", lambda: run_claude_batch(runtime, packet=body.packet))
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail="Разбор уже выполняется") from exc
    except ClaudeCliError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_run_id": run_id}


# ---- resume publish (F-8.2.6) ---------------------------------------------------------------


@router.post("/profile/resumes/{resume_pk}/publish")
async def publish(
    resume_pk: int,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
    force: bool = False,
) -> dict[str, Any]:
    if not auth_public_state(await get_hh_auth(session))["logged_in"]:
        raise HTTPException(status_code=400, detail="Сначала войдите в hh.ru")
    resume = await session.get(Resume, resume_pk)
    if resume is None:
        raise HTTPException(status_code=404, detail="Резюме не найдено")
    try:
        result = await publish_resume(session, runtime.settings, runtime.hh_client(), resume, force=force)
    except ProfileError as exc:
        await session.commit()
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    await session.commit()
    result["resume"] = resume_row(resume)
    return result


@router.get("/profile/resumes/{resume_pk}/publish")
async def publish_status(
    resume_pk: int,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    resume = await session.get(Resume, resume_pk)
    if resume is None:
        raise HTTPException(status_code=404, detail="Резюме не найдено")
    ok, why = publish_allowed(resume, tz=runtime.settings.timezone)
    return {"allowed": ok, "reason": why}
