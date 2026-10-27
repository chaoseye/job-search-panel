"""The sign-in page of the public address and the browser session (services/web_login.py, app/web_access.py).

`/login` is a server page, not a part of the SPA: it must work before any session exists, carry no script and spend
the link only by the POST of its button (a GET never does — link previews and scanners open links first). A browser
that is in already goes on to the panel from a spent link and from a second press of the button: only a browser without
a session sees «уже вошли», «устарела» and the like.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.config import Settings
from app.db.base import as_utc
from app.runtime import Runtime
from app.services.audit import audit
from app.services.web_login import (
    LINK_TTL,
    LOGIN_PATH,
    cookie_name,
    describe_browser,
    end_session,
    link_state,
    redeem,
    safe_next,
    start_session,
)
from app.web_access import current_session, is_public

log = structlog.get_logger("web_login")
router = APIRouter(include_in_schema=False)
api_router = APIRouter(prefix="/session", tags=["session"])

REASONS = {
    "unknown": "Ссылка не найдена: возможно, её скопировали не целиком.",
    "used": "По этой ссылке уже вошли: каждая ссылка срабатывает один раз.",
    "revoked": "Эту ссылку отменила более новая ссылка или команда /logout.",
    "expired": f"Ссылка устарела: она действует {int(LINK_TTL.total_seconds() // 60)} минут.",
}

STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f6f7fa; color: #1b202b;
  font: 14px/1.5 "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, sans-serif; }
main { width: min(100% - 32px, 420px); background: #fefeff; border: 1px solid #dfe2e7; border-radius: 12px;
  padding: 28px 24px; }
h1 { margin: 0 0 12px; font-size: 18px; line-height: 1.3; }
p { margin: 0 0 12px; color: #4d535e; }
.note { margin: 16px 0 0; font-size: 12px; color: #686d78; }
.notice { margin: 0 0 16px; padding: 10px 12px; border-radius: 8px; background: #e0f7e5; color: #116b37; }
.notice.bad { background: #fdeeec; color: #b32926; }
button { width: 100%; height: 40px; margin-top: 4px; border: 0; border-radius: 8px; background: #1b202b; color: #fff;
  font: inherit; font-weight: 600; cursor: pointer; }
button:hover { background: #2f3542; }
button:focus-visible, a:focus-visible { outline: 2px solid #2d74e8; outline-offset: 2px; }
a { color: #2661c2; }
"""


def _page(title: str, body: str, *, status: int = 200, headers: dict[str, str] | None = None) -> HTMLResponse:
    document = (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="referrer" content="same-origin"><meta name="robots" content="noindex, nofollow">'
        f"<title>{html.escape(title)} · Job Search Panel</title>"
        f'<link rel="icon" href="/favicon.ico"><style>{STYLE}</style></head>'
        f"<body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"
    )
    return HTMLResponse(document, status_code=status, headers=headers)


def _local_time(moment: datetime | None, settings: Settings) -> str:
    dt = as_utc(moment)
    return dt.astimezone(ZoneInfo(settings.timezone)).strftime("%H:%M") if dt else ""


async def _bot_name(session: AsyncSession, settings: Settings) -> str:
    """«@ExamplePanelBot» as a link, from the cached getMe (no request to Telegram from this page)."""
    from app.services.telegram import bot_identity

    bot = await bot_identity(session, settings)
    username = str(bot.get("username") or "") if bot else ""
    if not username:
        return "боту панели"
    return f'<a href="https://t.me/{html.escape(username, quote=True)}" rel="noreferrer">@{html.escape(username)}</a>'


async def _ask_for_link(
    session: AsyncSession, settings: Settings, *, notice: str = "", status: int = 200
) -> HTMLResponse:
    who = await _bot_name(session, settings)
    body = (
        notice + f"<p>Панель открывается по одноразовой ссылке. Отправьте {who} команду <b>/open</b>: "
        f"бот пришлёт ссылку, она действует {int(LINK_TTL.total_seconds() // 60)} минут.</p>"
        + '<p class="note">Ссылка приходит только в ваш личный чат с ботом.</p>'
    )
    return _page("Вход в панель", body, status=status)


def _require_public(request: Request, runtime: Runtime) -> Settings:
    if not is_public(request.scope, runtime.settings):
        raise HTTPException(status_code=404)
    return runtime.settings


@router.get(LOGIN_PATH)
async def login_page(
    request: Request,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
    t: str | None = None,
    next: str | None = None,
    out: str | None = None,
    expired: str | None = None,
) -> Response:
    settings = _require_public(request, runtime)
    if t:
        state, expires = await link_state(session, t)
        if state != "ok":
            # a link already pressed in another tab of this browser, or opened again later: the browser is in
            if await current_session(request, settings) is not None:
                return RedirectResponse(safe_next(next), status_code=303)
            notice = f'<p class="notice bad">{html.escape(REASONS[state])}</p>'
            return await _ask_for_link(session, settings, notice=notice, status=400)
        token = html.escape(t, quote=True)
        body = (
            "<p>Вы входите в Job Search Panel на этом устройстве.</p>"
            f'<form method="post" action="{LOGIN_PATH}"><input type="hidden" name="t" value="{token}">'
            '<button type="submit">Войти в панель</button></form>'
            f'<p class="note">Ссылка срабатывает один раз и действует до {_local_time(expires, settings)}. '
            "Если вы её не запрашивали, закройте страницу и отправьте боту /logout.</p>"
        )
        return _page("Вход в панель", body)
    if await current_session(request, settings) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    notice = ""
    if out:
        notice = '<p class="notice">Вы вышли из панели на этом устройстве.</p>'
    elif expired:
        notice = '<p class="notice bad">Сессия закончилась: войдите снова.</p>'
    return await _ask_for_link(session, settings, notice=notice)


@router.post(LOGIN_PATH)
async def login_submit(
    request: Request,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
    t: Annotated[str, Form()] = "",
) -> Response:
    settings = _require_public(request, runtime)
    ip = request.client.host if request.client else None
    wait = runtime.login_attempts.check(ip)
    if wait:
        minutes = max(1, round(wait / 60))
        body = f"<p>Слишком много попыток входа. Подождите {minutes} мин. и попробуйте снова.</p>"
        return _page("Вход в панель", body, status=429, headers={"Retry-After": str(int(wait))})
    token, state = await redeem(session, t)
    if token is None:
        # the button pressed again, or in a second tab with the same link, after this browser got in: no error and no
        # second session — into the panel («уже вошли» reads like a failed sign-in)
        if await current_session(request, settings) is not None:
            await session.rollback()
            return RedirectResponse("/", status_code=303)
        await audit(session, "auth", f"Вход по ссылке отклонён: {REASONS[state]}", level="warning", ip=ip)
        await session.commit()
        notice = f'<p class="notice bad">{html.escape(REASONS[state])}</p>'
        return await _ask_for_link(session, settings, notice=notice, status=400)
    user_agent = request.headers.get("user-agent")
    cookie, row = await start_session(session, settings, token=token, ip=ip, user_agent=user_agent)
    browser = describe_browser(user_agent)
    await audit(session, "auth", "Вход по ссылке", ip=ip, browser=browser, web_session=row.id)
    from app.services.notify import emit_event

    created = as_utc(row.created_at)
    await emit_event(
        session,
        settings,
        "login",
        {"at": created.isoformat() if created else None, "ip": ip, "browser": browser},
    )
    await session.commit()
    _deliver_soon(request)
    log.info("web_login", web_session=row.id, browser=browser)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        cookie_name(settings),
        cookie,
        max_age=int(timedelta(hours=settings.panel_session_hours).total_seconds()),
        path="/",
        secure=settings.public_url_secure,
        httponly=True,
        samesite="strict",
    )
    return response


def _deliver_soon(request: Request) -> None:
    """The sign-in message goes now, not at the next minute tick: the delivery job runs at once (one delivery path,
    so no message is sent twice)."""
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        return
    try:
        scheduler.modify_job("notify_tick", next_run_time=datetime.now(UTC))
    except Exception as exc:  # noqa: BLE001 - the regular tick sends it a minute later anyway
        log.warning("login_notice_trigger_failed", error=str(exc))


@api_router.get("")
async def session_info(request: Request, runtime: Annotated[Runtime, Depends(get_runtime)]) -> dict[str, Any]:
    """How this browser reached the panel: `local` (the laptop, the tunnel) or `link` (the public address, with the
    session's times) — the interface shows «Выйти» only for the latter."""
    web_session = getattr(request.state, "web_session", None)
    if not is_public(request.scope, runtime.settings) or web_session is None:
        return {"via": "local"}
    created, expires = as_utc(web_session.created_at), as_utc(web_session.expires_at)
    return {
        "via": "link",
        "created_at": created.isoformat() if created else None,
        "expires_at": expires.isoformat() if expires else None,
    }


@api_router.post("/logout")
async def session_logout(
    request: Request,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    settings = _require_public(request, runtime)
    web_session = getattr(request.state, "web_session", None)
    if web_session is not None:
        await end_session(session, web_session.id)
        await audit(session, "auth", "Выход из панели в браузере", web_session=web_session.id)
        await session.commit()
    response = JSONResponse({"ok": True})
    response.delete_cookie(
        cookie_name(settings), path="/", secure=settings.public_url_secure, httponly=True, samesite="strict"
    )
    return response
