"""Who may call what — the two entrances of the panel (README, «Вход из браузера»).

The panel listens on APP_HOST:APP_PORT for the laptop and the SSH tunnel and, when PANEL_PUBLIC_URL is set, on
127.0.0.1:PANEL_PUBLIC_PORT for the reverse proxy of the public address (app/server.py opens both in one process). The
proxy runs on the same machine: its requests come from 127.0.0.1 exactly like the tunnel's, so the client address tells
nothing. The listener does — `scope["server"]` is the local address of the connection, which no client can choose.

- Local entrance: as before (main.py had it as `local_only`): only the machine's own names in Host (DNS rebinding),
  no state-changing request from another site's page (CSRF); with the public address on, a request relayed by a proxy
  (X-Forwarded-For…) is refused here too — the proxy belongs on the public entrance.
- Public entrance: only the public name in Host; a state-changing request only from the panel's own pages (Origin);
  without a session from the bot's one-time link (services/web_login.py) the API answers 401 and a page leads to
  /login. Open without a session: /login itself and the static files of the interface (there is no data in them).
"""

from __future__ import annotations

import html
from urllib.parse import quote, urlsplit

from fastapi import Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.types import Scope

from app.config import Settings
from app.db.models import WebSession
from app.services.web_login import LOGIN_PATH, cookie_name, find_session, well_formed

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# FastAPI's own pages: not needed from outside, not served there
API_DOCS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})
# what a reverse proxy adds to a request; the browser behind the SSH tunnel sends none of them
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "forwarded", "x-real-ip")
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
LOGIN_REQUIRED = "Нужен вход: отправьте боту команду /open и откройте ссылку из ответа"
# the sign-in form carries one 43-character token: a bigger body is refused before it is read
LOGIN_FORM_MAX_BYTES = 4096
PROXIED_TO_TUNNEL = "Запрос через прокси на внутренний вход панели отклонён: прокси должен вести на PANEL_PUBLIC_PORT"


def host_name(value: str) -> str:
    """`localhost:8000` → `localhost`, `[::1]:8000` → `::1` (the Host header without the port)."""
    v = value.strip().lower()
    if v.startswith("["):
        return v[1:].split("]", 1)[0]
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def is_public(scope: Scope, settings: Settings) -> bool:
    """The request came through the public entrance. A connection without a known local address counts as public:
    when in doubt, ask for the sign-in."""
    if not settings.panel_public_url:
        return False
    server = scope.get("server")
    port = server[1] if server and len(server) > 1 else None
    return port is None or port == settings.panel_public_port


def is_static(path: str) -> bool:
    """A file of the built interface: /assets/… and the root files (favicon.ico, index.html). Not the SPA routes."""
    return path.startswith("/assets/") or (path.count("/") == 1 and "." in path[1:])


async def current_session(request: Request, settings: Settings) -> WebSession | None:
    raw = request.cookies.get(cookie_name(settings))
    if not well_formed(raw):
        return None
    async with request.app.state.runtime.session_factory() as db:
        return await find_session(db, raw)


def secured(response: Response, settings: Settings, *, no_store: bool = False) -> Response:
    """Headers of every answer of the public entrance."""
    h = response.headers
    if settings.public_url_secure:
        h["Strict-Transport-Security"] = "max-age=31536000"
    h["Content-Security-Policy"] = CSP
    h["X-Frame-Options"] = "DENY"
    h["X-Content-Type-Options"] = "nosniff"
    # not `no-referrer`: with it the browser sends `Origin: null` even with the panel's own POST (Fetch standard), and
    # the Origin check below would refuse the panel itself; `same-origin` still tells other sites nothing
    h["Referrer-Policy"] = "same-origin"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    if no_store:
        h["Cache-Control"] = "no-store"
    return response


def _same_origin(request: Request, settings: Settings) -> bool:
    """A state-changing request comes from a page of the public address itself. Browsers send Origin with every POST
    (a form or fetch); when it is missing or `null` (a referrer policy may hide it), only `Sec-Fetch-Site:
    same-origin` is trusted — a header no page script can set."""
    origin = (request.headers.get("origin") or "").strip().lower()
    if origin and origin != "null":
        return origin == settings.panel_public_url
    return request.headers.get("sec-fetch-site") == "same-origin"


def _small_body(request: Request) -> bool:
    try:
        length = int(request.headers.get("content-length", ""))
    except ValueError:  # no length (chunked) or garbage: not a form a browser sends
        return False
    return 0 <= length <= LOGIN_FORM_MAX_BYTES


def bounce_page(request: Request) -> HTMLResponse:
    """A page asked without a session goes to /login — by a refresh from this page rather than a redirect: a cookie
    with SameSite=Strict is not sent with a navigation that came from another site (a link in web.telegram.org), and
    it is sent with one started by the panel's own page. /login then lets a browser with a session straight through."""
    target = LOGIN_PATH + "?next=" + quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
    href = html.escape(target, quote=True)
    body = (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="referrer" content="same-origin">'
        f'<meta http-equiv="refresh" content="0;url={href}"><title>Job Search Panel</title></head>'
        f'<body><p><a href="{href}">Войти в панель</a></p></body></html>'
    )
    return HTMLResponse(body)


async def public_gate(request: Request, call_next: RequestResponseEndpoint, settings: Settings) -> Response:
    if host_name(request.headers.get("host", "")) != settings.public_host:
        return secured(JSONResponse(status_code=400, content={"detail": "Неизвестный заголовок Host"}), settings)
    if request.method in UNSAFE_METHODS and not _same_origin(request, settings):
        return secured(JSONResponse(status_code=403, content={"detail": "Запрос с чужого сайта отклонён"}), settings)
    path = request.url.path
    if path in API_DOCS:
        return secured(JSONResponse(status_code=404, content={"detail": "Not Found"}), settings)
    if path == LOGIN_PATH:
        if request.method == "POST" and not _small_body(request):
            return secured(JSONResponse(status_code=413, content={"detail": "Слишком большой запрос"}), settings)
        return secured(await call_next(request), settings, no_store=True)
    if request.method in ("GET", "HEAD") and is_static(path):
        return secured(await call_next(request), settings)
    web_session = await current_session(request, settings)
    if web_session is None:
        if path.startswith("/api/") or request.method not in ("GET", "HEAD"):
            denied: Response = JSONResponse(
                status_code=401, content={"detail": LOGIN_REQUIRED, "kind": "login_required"}
            )
        else:
            denied = bounce_page(request)
        return secured(denied, settings, no_store=True)
    request.state.web_session = web_session
    return secured(await call_next(request), settings, no_store=path.startswith("/api/"))


async def local_gate(request: Request, call_next: RequestResponseEndpoint, settings: Settings) -> Response:
    """The panel serves only its own machine: an unknown Host (DNS rebinding — a web page whose domain resolves to
    127.0.0.1) gets 400, a state-changing request from another site's page (a plain HTML form needs no CORS
    preflight) gets 403. Requests without Origin (CLI, curl, tests) pass."""
    allowed = settings.allowed_host_names
    if allowed is not None:
        if host_name(request.headers.get("host", "")) not in allowed:
            return JSONResponse(
                status_code=400,
                content={"detail": "Неизвестный заголовок Host: панель отвечает только на localhost (ALLOWED_HOSTS)"},
            )
        if settings.panel_public_url and any(h in request.headers for h in PROXY_HEADERS):
            # the reverse proxy of the public address must lead to PANEL_PUBLIC_PORT; here it would skip the sign-in
            return JSONResponse(status_code=403, content={"detail": PROXIED_TO_TUNNEL})
        if request.method in UNSAFE_METHODS:
            origin = request.headers.get("origin")
            cross_site = request.headers.get("sec-fetch-site") == "cross-site"
            if origin is not None:
                origin_host = urlsplit(origin).hostname if origin != "null" else None
                cross_site = cross_site or not origin_host or origin_host.lower() not in allowed
            if cross_site:
                return JSONResponse(status_code=403, content={"detail": "Запрос с чужого сайта отклонён"})
    return await call_next(request)
