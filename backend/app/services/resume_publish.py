"""«Поднять резюме» (F-8.2.6, желательно): POST /resumes/{id}/publish when hh allows it (can_publish_or_update,
next_publish_at ≈ every 4 hours). Only when the probe (or a real call) confirmed `capabilities.resume_publish`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import Resume
from app.hh.client import HHClient
from app.hh.errors import HHAuthError, HHCaptchaRequired, HHError, HHForbidden, HHNotFound
from app.services.audit import audit
from app.services.profile import ProfileError, import_hh_resume, primary_resume
from app.services.state import get_system_state, read_capabilities, update_capability

log = structlog.get_logger("resume_publish")
MIN_INTERVAL = timedelta(hours=4)


def publish_allowed(resume: Resume, now: datetime | None = None, tz: str | None = None) -> tuple[bool, str]:
    """hh rule: not more often than every 4 hours; `next_publish_at` from the resume is the authority. The time in
    the message is local to `tz` (Settings.timezone), not to the computer the panel runs on."""
    now = now or utcnow()
    nxt = as_utc(resume.next_publish_at)
    zone = ZoneInfo(tz) if tz else None
    if resume.can_publish_or_update is False and nxt is not None and nxt > now:
        return False, f"hh разрешит поднять после {nxt.astimezone(zone).strftime('%d.%m %H:%M')}"
    if nxt is not None and nxt > now:
        return False, f"следующее поднятие возможно после {nxt.astimezone(zone).strftime('%d.%m %H:%M')}"
    return True, ""


async def publish_resume(
    session: AsyncSession, settings: Settings, client: HHClient, resume: Resume, *, force: bool = False
) -> dict[str, Any]:
    """Touches the resume on hh.ru and refreshes its `next_publish_at`. Raises ProfileError with a human message."""
    if resume.source != "hh" or not resume.hh_resume_id:
        raise ProfileError("Поднять можно только резюме, импортированное из hh.ru", status=409)
    state = await get_system_state(session)
    caps = read_capabilities(state)
    if caps.get("resume_publish") == "unavailable":
        raise ProfileError("По данным зонда поднятие резюме (POST /resumes/{id}/publish) недоступно приложению", 409)
    if caps.get("resume_publish") != "available" and not force:
        raise ProfileError(
            "Зонд не подтверждал доступность поднятия резюме. Запустите зонд с галочкой «Разрешить проверку "
            "«поднять резюме»» или нажмите «Поднять сейчас» ещё раз, чтобы попробовать без подтверждения",
            409,
        )
    ok, why = publish_allowed(resume, tz=settings.timezone)
    if not ok and not force:
        raise ProfileError(f"Ещё рано: {why}", 409)
    try:
        await client.request("POST", f"/resumes/{resume.hh_resume_id}/publish")
    except (HHAuthError, HHCaptchaRequired):
        raise
    except HHForbidden as exc:
        await update_capability(session, "resume_publish", "unavailable")
        raise ProfileError(f"hh.ru отказал в поднятии резюме: {exc.summary()}", status=502) from exc
    except HHNotFound as exc:
        raise ProfileError(f"Резюме не найдено на hh.ru: {exc.summary()}", status=502) from exc
    except HHError as exc:
        raise ProfileError(f"Не удалось поднять резюме: {exc.summary()}", status=502) from exc
    await update_capability(session, "resume_publish", "available")
    # refresh next_publish_at from hh (best effort)
    try:
        await import_hh_resume(session, settings, client, resume.hh_resume_id)
    except (HHAuthError, HHCaptchaRequired):
        raise
    except ProfileError as exc:
        log.info("resume_refresh_after_publish_failed", error=str(exc))
    nxt = as_utc(resume.next_publish_at)
    if nxt is None or nxt <= utcnow():
        resume.next_publish_at = utcnow() + MIN_INTERVAL
        resume.can_publish_or_update = False
    await audit(
        session,
        "profile",
        f"Резюме «{resume.title or resume.hh_resume_id}» поднято на hh.ru",
        resume_id=resume.hh_resume_id,
    )
    return {
        "ok": True,
        "next_publish_at": (n.isoformat() if (n := as_utc(resume.next_publish_at)) else None),
    }


async def auto_publish_tick(runtime: Any) -> dict[str, Any]:
    """Hourly: touches the primary hh resume when RESUME_AUTO_PUBLISH=true and hh allows it."""
    settings = runtime.settings
    if not settings.resume_auto_publish:
        return {"skipped": "disabled"}
    from app.services.state import auth_public_state, get_hh_auth

    async with runtime.session_factory() as session:
        if not auth_public_state(await get_hh_auth(session))["logged_in"]:
            return {"skipped": "not logged in"}
        state = await get_system_state(session)
        if state.paused_reason or read_capabilities(state).get("resume_publish") != "available":
            return {"skipped": "paused or capability not confirmed"}
        resume = await primary_resume(session, "hh")
        if resume is None:
            return {"skipped": "no hh resume"}
        ok, why = publish_allowed(resume, tz=settings.timezone)
        if not ok:
            return {"skipped": why}
        try:
            result = await publish_resume(session, settings, runtime.hh_client(), resume)
        except ProfileError as exc:
            await session.commit()
            return {"error": str(exc)}
        except (HHAuthError, HHCaptchaRequired) as exc:
            await session.commit()
            return {"error": exc.summary()}
        await session.commit()
        return result
