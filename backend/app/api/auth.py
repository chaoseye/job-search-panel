"""OAuth login/callback/status/logout and the captcha return endpoint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.hh.auth import (
    build_authorize_url,
    exchange_code,
    generate_pkce,
    generate_state,
    revoke_token,
)
from app.hh.errors import HHError, HHOAuthTokenError
from app.hh.models import MeInfo
from app.runtime import Runtime
from app.services.audit import audit
from app.services.state import (
    auth_public_state,
    clear_tokens,
    delete_setting,
    get_hh_auth,
    get_setting,
    get_system_state,
    save_tokens,
    set_pause,
    set_setting,
)

log = structlog.get_logger("api.auth")
router = APIRouter(prefix="/auth", tags=["auth"])

PENDING_KEY = "oauth_pending"
PENDING_TTL = timedelta(minutes=15)


@router.get("/hh/login")
async def hh_login(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> RedirectResponse:
    settings = runtime.settings
    if not settings.hh_client_id or not settings.hh_client_secret:
        raise HTTPException(status_code=400, detail="HH_CLIENT_ID / HH_CLIENT_SECRET не заданы в .env")
    verifier, challenge = generate_pkce()
    state = generate_state()
    await set_setting(
        session,
        PENDING_KEY,
        {"state": state, "verifier": verifier, "created_at": datetime.now(UTC).isoformat()},
    )
    await session.commit()
    url = build_authorize_url(settings, state, challenge)
    log.info("oauth_login_started")
    return RedirectResponse(url, status_code=302)


@router.get("/hh/callback")
async def hh_callback(
    request: Request,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    settings = runtime.settings
    target = settings.frontend_path("/settings")

    pending = await get_setting(session, PENDING_KEY)
    if error:
        await audit(session, "auth", f"Пользователь отклонил доступ: {error}", level="warning")
        await session.commit()
        return RedirectResponse(f"{target}?auth=error&reason={error}", status_code=302)
    if not code or not pending or not state or pending.get("state") != state:
        await audit(session, "auth", "OAuth callback с неверным state или без кода", level="error")
        await session.commit()
        return RedirectResponse(f"{target}?auth=error&reason=bad_state", status_code=302)
    created = datetime.fromisoformat(pending.get("created_at"))
    if datetime.now(UTC) - created > PENDING_TTL:
        return RedirectResponse(f"{target}?auth=error&reason=expired", status_code=302)

    try:
        tokens = await exchange_code(runtime.http, settings, code, pending.get("verifier"))
    except HHOAuthTokenError as exc:
        await audit(session, "auth", f"Обмен кода не удался: {exc.error} {exc.error_description or ''}", level="error")
        await delete_setting(session, PENDING_KEY)
        await session.commit()
        log.error("oauth_exchange_failed", error=exc.error, description=exc.error_description)
        return RedirectResponse(f"{target}?auth=error&reason={exc.error}", status_code=302)
    except HHError as exc:
        await audit(session, "auth", f"Обмен кода не удался: {exc}", level="error")
        await session.commit()
        return RedirectResponse(f"{target}?auth=error&reason=transport", status_code=302)

    await save_tokens(session, tokens)
    await delete_setting(session, PENDING_KEY)
    await session.commit()

    # /me to confirm role and store display info
    me_public: dict[str, Any] | None = None
    try:
        data = await runtime.hh_client().get("/me")
        me = MeInfo.model_validate(data)
        me_public = me.public_dict()
        await set_setting(session, "hh_me", me_public)
        row = await get_hh_auth(session)
        if row is not None and me.id:
            row.hh_user_id = str(me.id)
        if not me.is_applicant:
            await audit(session, "auth", "Вход выполнен, но токен не соискателя", level="warning")
            await session.commit()
            return RedirectResponse(f"{target}?auth=error&reason=not_applicant", status_code=302)
        await audit(session, "auth", f"Вход в hh.ru выполнен: {me.display_name}")
        await session.commit()
    except HHError as exc:
        await audit(session, "auth", f"Токен получен, но /me не отвечает: {exc}", level="warning")
        await session.commit()
        return RedirectResponse(f"{target}?auth=ok&warning=me_failed", status_code=302)

    log.info("oauth_login_ok", user=me_public.get("id") if me_public else None)
    return RedirectResponse(f"{target}?auth=ok", status_code=302)


@router.get("/status")
async def auth_status(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    row = await get_hh_auth(session)
    payload = auth_public_state(row)
    payload["user"] = await get_setting(session, "hh_me")
    payload["client_id_set"] = bool(runtime.settings.hh_client_id and runtime.settings.hh_client_secret)
    payload["redirect_uri"] = runtime.settings.hh_redirect_uri
    payload["user_agent"] = runtime.settings.hh_user_agent
    payload["user_agent_placeholder"] = runtime.settings.user_agent_is_placeholder
    return payload


@router.post("/refresh-me")
async def refresh_me(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    try:
        data = await runtime.hh_client().get("/me")
    except HHError as exc:
        raise HTTPException(status_code=502, detail=exc.summary()) from exc
    me = MeInfo.model_validate(data).public_dict()
    await set_setting(session, "hh_me", me)
    await session.commit()
    return me


@router.post("/logout")
async def logout(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    row = await get_hh_auth(session)
    revoked = False
    if row is not None and row.access_token:
        revoked = await revoke_token(runtime.http, runtime.settings, row.access_token)
    await clear_tokens(session)
    await audit(session, "auth", "Выход из hh.ru", revoked=revoked)
    await session.commit()
    return {"ok": True, "revoked": revoked}


@router.get("/captcha-done")
async def captcha_done(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> RedirectResponse:
    state = await get_system_state(session)
    if state.paused_reason == "captcha":
        await set_pause(session, None, None)
        await audit(session, "hh", "Капча пройдена, пауза снята")
    await session.commit()
    return RedirectResponse(runtime.settings.frontend_path("/settings?captcha=done"), status_code=302)
