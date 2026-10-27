"""Non-secret settings view and a few user-editable options."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.db.base import as_utc
from app.runtime import Runtime
from app.services.audit import audit
from app.services.state import effective_apply_mode, get_setting, get_system_state, set_setting

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingsUpdate(BaseModel):
    apply_mode_override: Literal["auto", "api", "assist"] | None = None
    resume_id: str | None = None

    @field_validator("resume_id")
    @classmethod
    def _clean_resume_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if value.startswith("http"):
            value = value.rstrip("/").split("/")[-1].split("?")[0]
        return value


async def settings_view(runtime: Runtime, session: AsyncSession) -> dict[str, Any]:
    s = runtime.settings
    state = await get_system_state(session)
    caps = json.loads(state.capabilities_json) if state.capabilities_json else None
    probe_at = as_utc(state.probe_at)
    consent_at = as_utc(state.consent_accepted_at)
    return {
        "apply_mode": state.apply_mode,
        "apply_mode_override": state.apply_mode_override,
        "apply_mode_env": s.apply_mode,
        "capabilities": caps,
        "probe_at": probe_at.isoformat() if probe_at else None,
        "consent_accepted_at": consent_at.isoformat() if consent_at else None,
        "paused_reason": state.paused_reason,
        "captcha_url": state.captcha_url,
        "resume_id": await get_setting(session, "manual_resume_id"),
        "resumes_hint": await get_setting(session, "resumes_hint") or [],
        "hh": {
            "client_id_set": bool(s.hh_client_id and s.hh_client_secret),
            "redirect_uri": s.hh_redirect_uri,
            "user_agent": s.hh_user_agent,
            "rps": s.hh_rps,
        },
        "limits": {
            "apply_max_per_day": s.apply_max_per_day,
            "apply_max_per_hour": s.apply_max_per_hour,
            "apply_window": s.apply_window,
            "apply_autopilot": s.apply_autopilot,
            "apply_dry_run": s.apply_dry_run,
        },
        "telegram_configured": bool(s.telegram_bot_token and s.telegram_chat_id),
    }


@router.get("")
async def get_settings_view(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    return await settings_view(runtime, session)


@router.put("")
async def update_settings(
    body: SettingsUpdate,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    state = await get_system_state(session)
    if body.apply_mode_override is not None:
        state.apply_mode_override = body.apply_mode_override
        caps = json.loads(state.capabilities_json) if state.capabilities_json else {}
        suggested = "api" if caps.get("negotiations_post") == "available" else "assist"
        state.apply_mode = effective_apply_mode(runtime.settings, state, suggested)
        await audit(session, "settings", f"Режим отклика: {state.apply_mode} (override={body.apply_mode_override})")
    if body.resume_id is not None:
        await set_setting(session, "manual_resume_id", body.resume_id or None)
    await session.commit()
    return await settings_view(runtime, session)
