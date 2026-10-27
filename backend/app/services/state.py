"""Settings/system-state/auth persistence and the DB-backed token provider."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import HHAuth, Setting, SystemState
from app.hh.auth import client_credentials_token, refresh_tokens
from app.hh.errors import HHError, HHOAuthTokenError
from app.hh.models import TokenResponse
from app.services.audit import audit

log = structlog.get_logger("state")

APP_TOKEN_MIN_INTERVAL = timedelta(minutes=5)


# ---- generic settings -------------------------------------------------------


async def get_setting(session: AsyncSession, key: str, default: Any = None) -> Any:
    row = await session.get(Setting, key)
    if row is None:
        return default
    try:
        return json.loads(row.value_json)
    except ValueError:
        return default


async def set_setting(session: AsyncSession, key: str, value: Any) -> None:
    row = await session.get(Setting, key)
    payload = json.dumps(value, ensure_ascii=False, default=str)
    if row is None:
        session.add(Setting(key=key, value_json=payload))
    else:
        row.value_json = payload


async def delete_setting(session: AsyncSession, key: str) -> None:
    row = await session.get(Setting, key)
    if row is not None:
        await session.delete(row)


# ---- system state -----------------------------------------------------------


async def get_system_state(session: AsyncSession) -> SystemState:
    row = await session.get(SystemState, 1)
    if row is None:
        row = SystemState(id=1)
        session.add(row)
        await session.flush()
    return row


def read_capabilities(state: SystemState) -> dict[str, str]:
    if not state.capabilities_json:
        return {}
    try:
        data = json.loads(state.capabilities_json)
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


async def update_capability(session: AsyncSession, key: str, value: str) -> None:
    """Record what a real call just proved (e.g. resume_get available) without re-running the probe."""
    state = await get_system_state(session)
    caps = read_capabilities(state)
    if caps.get(key) == value:
        return
    caps[key] = value
    state.capabilities_json = json.dumps(caps, ensure_ascii=False)
    await session.flush()


def effective_apply_mode(settings: Settings, state: SystemState, suggested: str | None) -> str:
    if state.apply_mode_override in ("api", "assist"):
        return state.apply_mode_override
    if settings.apply_mode in ("api", "assist"):
        return settings.apply_mode
    return suggested or "assist"


# ---- hh auth ----------------------------------------------------------------


async def get_hh_auth(session: AsyncSession) -> HHAuth | None:
    return await session.get(HHAuth, 1)


async def save_tokens(session: AsyncSession, tokens: TokenResponse, hh_user_id: str | None = None) -> HHAuth:
    row = await get_hh_auth(session)
    if row is None:
        row = HHAuth(id=1)
        session.add(row)
    now = utcnow()
    row.access_token = tokens.access_token
    if tokens.refresh_token:
        row.refresh_token = tokens.refresh_token
    row.expires_at = now + timedelta(seconds=max(60, tokens.expires_in))
    row.obtained_at = now
    row.state = "ok"
    if hh_user_id:
        row.hh_user_id = hh_user_id
    await session.flush()
    return row


async def clear_tokens(session: AsyncSession) -> None:
    row = await get_hh_auth(session)
    if row is not None:
        row.access_token = None
        row.refresh_token = None
        row.expires_at = None
        row.state = "none"
    await delete_setting(session, "hh_me")


def auth_public_state(row: HHAuth | None) -> dict[str, Any]:
    if row is None or not row.access_token or row.state in ("none",):
        return {"logged_in": False, "state": "none", "expires_at": None, "hh_user_id": None}
    expires = as_utc(row.expires_at)
    return {
        "logged_in": row.state in ("ok", "expired"),
        "state": row.state,
        "expires_at": expires.isoformat() if expires else None,
        "hh_user_id": row.hh_user_id,
    }


class DbTokenStore:
    """TokenProvider backed by the hh_auth table. Refreshes only after expiry (hh rule)."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], settings: Settings, http: httpx.AsyncClient
    ) -> None:
        self._sf = session_factory
        self._settings = settings
        self._http = http
        self._lock = asyncio.Lock()
        self._app_lock = asyncio.Lock()
        self._app_token: str | None = None
        self._app_token_attempt_at: datetime | None = None

    async def get_access_token(self) -> str | None:
        async with self._sf() as session:
            row = await get_hh_auth(session)
            if row is None or not row.access_token or row.state == "revoked":
                return None
            expires = as_utc(row.expires_at)
            token = row.access_token
        if expires is not None and expires <= utcnow():
            return await self.refresh()
        return token

    async def refresh(self) -> str | None:
        async with self._lock:
            async with self._sf() as session:
                row = await get_hh_auth(session)
                if row is None or not row.refresh_token or row.state == "revoked":
                    return None
                expires = as_utc(row.expires_at)
                if expires is not None and expires > utcnow() + timedelta(minutes=1) and row.state == "ok":
                    # somebody refreshed while we waited for the lock
                    return row.access_token
                try:
                    tokens = await refresh_tokens(self._http, self._settings, row.refresh_token)
                except HHOAuthTokenError as exc:
                    if exc.not_expired_yet:
                        log.info("refresh_not_needed_yet")
                        return row.access_token
                    row.state = "expired"
                    await audit(session, "auth", f"Не удалось обновить токен: {exc.error}", level="error")
                    from app.services.notify import emit_event

                    await emit_event(session, self._settings, "auth", {"reason": exc.error})
                    await session.commit()
                    log.warning("refresh_failed", error=exc.error, description=exc.error_description)
                    return None
                except HHError as exc:
                    log.warning("refresh_transport_error", error=str(exc))
                    return None
                await save_tokens(session, tokens)
                await audit(session, "auth", "Токен hh.ru обновлён")
                await session.commit()
                log.info("token_refreshed")
                return tokens.access_token

    async def get_app_token(self, *, force: bool = False) -> str | None:
        """Application token (client_credentials) for public methods.

        Kept only in memory: the token is permanent on hh.ru's side, and writing it to SQLite from here could block
        behind a job session that holds the write lock (the same trap as the captcha hook). Re-requested on `force`
        (rejected token) but not more often than once per 5 minutes (hh rule); None when it cannot be obtained.
        """
        async with self._app_lock:
            if self._app_token and not force:
                return self._app_token
            now = utcnow()
            if self._app_token_attempt_at is not None and now - self._app_token_attempt_at < APP_TOKEN_MIN_INTERVAL:
                return self._app_token
            self._app_token_attempt_at = now
            if not (self._settings.hh_client_id and self._settings.hh_client_secret):
                return None
            try:
                tokens = await client_credentials_token(self._http, self._settings)
            except Exception as exc:  # noqa: BLE001 - never break a public call because of the app token
                log.warning("app_token_failed", error=str(exc)[:200])
                return self._app_token
            self._app_token = tokens.access_token
            log.info("app_token_obtained")
            return self._app_token

    async def on_auth_failure(self, value: str) -> None:
        revoked = value in ("token_revoked", "bad_authorization", "application_not_found", "client_id_deleted")
        async with self._sf() as session:
            row = await get_hh_auth(session)
            if row is None:
                return
            if revoked:
                row.state = "revoked"
                await audit(session, "auth", f"Требуется повторный вход в hh.ru: {value}", level="error")
                from app.services.notify import emit_event

                await emit_event(session, self._settings, "auth", {"reason": value})
            else:
                await audit(session, "auth", f"Ошибка авторизации hh.ru: {value}", level="warning")
            await session.commit()


# ---- captcha / pause --------------------------------------------------------


async def set_pause(session: AsyncSession, reason: str | None, captcha_url: str | None = None) -> None:
    state = await get_system_state(session)
    state.paused_reason = reason
    state.captcha_url = captcha_url
    await session.flush()


def captcha_url_with_backurl(captcha_url: str, backurl: str) -> str:
    sep = "&" if "?" in captcha_url else "?"
    from urllib.parse import quote

    return f"{captcha_url}{sep}backurl={quote(backurl, safe='')}"


__all__ = [
    "get_setting",
    "set_setting",
    "delete_setting",
    "get_system_state",
    "read_capabilities",
    "update_capability",
    "effective_apply_mode",
    "get_hh_auth",
    "save_tokens",
    "clear_tokens",
    "auth_public_state",
    "DbTokenStore",
    "set_pause",
    "captcha_url_with_backurl",
]
