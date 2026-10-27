"""Sign-in from a browser by the bot's one-time link (README, «Вход из браузера»). One panel, two entrances: the
local one (the laptop, the SSH tunnel) as before, the public one (the reverse proxy) only with a session. The link from /open works once and five minutes, never by a GET; /logout in the
bot ends every session; every sign-in is reported to the bot.

httpx's ASGI transport puts the host and port of the URL into `scope["server"]` (no port for a default one — the panel
then asks for the sign-in): https://panel.example:8443 plays the listener of the proxy, http://test:8000 the tunnel.
The last test runs the real two-socket server."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
import uvicorn
from httpx import ASGITransport
from sqlalchemy import func, select

from app.config import Settings
from app.db.base import utcnow
from app.db.models import AuditLog, LoginToken, Notification, WebSession
from app.main import FRONTEND_DIST, app
from app.runtime import Runtime
from app.server import listening_sockets
from app.services import web_login
from app.services.bot_commands import poll_commands
from app.services.telegram import deliver_pending, telegram_status

TOKEN = "123456:TEST-TOKEN-VALUE"
TG = f"https://api.telegram.org/bot{TOKEN}"
PUBLIC = "https://panel.example"
PROXY_SIDE = "https://panel.example:8443"  # the public entrance in these tests (`scope["server"]`)
TUNNEL_SIDE = "http://test:8000"
OWNER = 42
CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
LINK_RE = re.compile(r"https://panel\.example/login\?t=([A-Za-z0-9_-]{43})")
SAME_SITE = {"Origin": PUBLIC}


class Web:
    """The panel with the public address on: `public` and `local` clients, more browsers on demand."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.transport = ASGITransport(app=app)
        self.clients: list[httpx.AsyncClient] = []
        self.public = self.browser()
        self.local = self.browser(TUNNEL_SIDE)

    def browser(self, base_url: str = PROXY_SIDE) -> httpx.AsyncClient:
        """A browser of its own (its own cookies)."""
        client = httpx.AsyncClient(transport=self.transport, base_url=base_url)
        self.clients.append(client)
        return client


@pytest.fixture
async def web(settings: Settings):
    s = settings.model_copy(
        update={
            "panel_public_url": PUBLIC,
            "panel_public_port": 8443,
            "telegram_bot_token": TOKEN,
            "telegram_chat_id": str(OWNER),
            "telegram_events": "digest",  # the sign-in message must not depend on this list
        }
    )
    runtime = await Runtime.create(s)
    app.state.runtime = runtime
    panel = Web(runtime)
    yield panel
    for client in panel.clients:
        await client.aclose()
    await runtime.close()


class FakeTelegram:
    """getUpdates hands out what `say` queued; sendMessage bodies are kept."""

    def __init__(self, respx_mock: respx.MockRouter) -> None:
        self.sent: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self.update_id = 100
        respx_mock.post(f"{TG}/getUpdates").mock(side_effect=self._updates)
        respx_mock.post(f"{TG}/sendMessage").mock(side_effect=self._send)

    def say(self, text_: str, *, chat: int = OWNER, chat_type: str = "private", sender: int = OWNER) -> None:
        self.update_id += 1
        self.pending.append(
            {
                "update_id": self.update_id,
                "message": {
                    "message_id": self.update_id,
                    "chat": {"id": chat, "type": chat_type},
                    "from": {"id": sender, "is_bot": False},
                    "text": text_,
                },
            }
        )

    def _updates(self, _request: httpx.Request) -> httpx.Response:
        result, self.pending = self.pending, []
        return httpx.Response(200, json={"ok": True, "result": result})

    def _send(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})


async def _open(runtime: Runtime, tg: FakeTelegram) -> str:
    """/open from the owner's private chat → the token of the link in the reply."""
    tg.say("/open")
    await poll_commands(runtime)
    match = LINK_RE.search(tg.sent[-1]["text"])
    assert match, tg.sent[-1]["text"]
    return match.group(1)


async def _sign_in(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.post("/login", data={"t": token}, headers={**SAME_SITE, "User-Agent": CHROME})


async def _count(runtime: Runtime, model: Any) -> int:
    async with runtime.session_factory() as db:
        return int((await db.execute(select(func.count()).select_from(model))).scalar() or 0)


async def _audit(runtime: Runtime) -> list[str]:
    async with runtime.session_factory() as db:
        return [r.message for r in (await db.execute(select(AuditLog).order_by(AuditLog.id))).scalars()]


@respx.mock(assert_all_called=False)
async def test_link_works_once_and_a_get_never_spends_it(web, respx_mock: respx.MockRouter) -> None:
    public, runtime = web.public, web.runtime
    tg = FakeTelegram(respx_mock)
    token = await _open(runtime, tg)
    reply = tg.sent[-1]
    # only to the owner's chat and without a preview: Telegram does not open the link itself
    assert reply["chat_id"] == str(OWNER) and reply["link_preview_options"] == {"is_disabled": True}
    assert "disable_web_page_preview" not in reply and "действует до" in reply["text"]
    # the base keeps a hash of the token, never the token
    async with runtime.session_factory() as db:
        row = (await db.execute(select(LoginToken))).scalar_one()
    assert row.token_hash == web_login.digest(token) and row.used_at is None and row.chat_id == str(OWNER)
    base = runtime.settings.db_path
    for file in (base, base.with_name(base.name + "-wal")):
        assert not file.exists() or token.encode() not in file.read_bytes(), file.name

    for _ in range(2):  # a link preview, a link scanner, the person: GET only shows the button
        page = await public.get(f"/login?t={token}")
        assert page.status_code == 200 and "Войти в панель" in page.text and 'method="post"' in page.text
        assert page.headers["cache-control"] == "no-store"
    async with runtime.session_factory() as db:
        assert (await db.execute(select(LoginToken.used_at))).scalar_one() is None

    signed = await _sign_in(public, token)
    assert signed.status_code == 303 and signed.headers["location"] == "/"
    cookie = signed.headers["set-cookie"]
    assert cookie.startswith("__Host-jobpanel=") and "domain=" not in cookie.lower()
    for attribute in ("HttpOnly", "Secure", "SameSite=strict", "Path=/", f"Max-Age={12 * 3600}"):
        assert attribute.lower() in cookie.lower(), attribute
    async with runtime.session_factory() as db:
        assert (await db.execute(select(LoginToken.used_at))).scalar_one() is not None
        session_row = (await db.execute(select(WebSession))).scalar_one()
    assert session_row.ip == "127.0.0.1" and session_row.user_agent == CHROME
    assert session_row.session_hash == web_login.digest(public.cookies["__Host-jobpanel"])

    stranger = web.browser()  # another browser with the same link: it is spent
    again = await _sign_in(stranger, token)
    assert again.status_code == 400 and "уже вошли" in again.text and "set-cookie" not in again.headers
    assert (await stranger.get(f"/login?t={token}")).status_code == 400  # the page says so too
    journal = await _audit(runtime)
    assert "Выдана ссылка для входа в панель (/open)" in journal and "Вход по ссылке" in journal
    assert any(m.startswith("Вход по ссылке отклонён: По этой ссылке уже вошли") for m in journal)


@respx.mock(assert_all_called=False)
async def test_link_lives_five_minutes_and_a_newer_one_replaces_it(web, respx_mock: respx.MockRouter) -> None:
    public, runtime = web.public, web.runtime
    for minutes, expected in ((6, 400), (4, 303)):
        async with runtime.session_factory() as db:
            url, _expires = await web_login.issue_link(
                db, runtime.settings, chat_id="42", now=utcnow() - timedelta(minutes=minutes)
            )
            await db.commit()
        token = url.rsplit("t=", 1)[1]
        response = await _sign_in(public, token)
        assert response.status_code == expected, minutes
        if expected == 400:
            assert "устарела" in response.text
            assert "устарела" in (await public.get(f"/login?t={token}")).text
    tg = FakeTelegram(respx_mock)
    first = await _open(runtime, tg)
    second = await _open(runtime, tg)
    response = await _sign_in(web.browser(), first)
    assert response.status_code == 400 and "более новая ссылка" in response.text
    assert (await _sign_in(web.browser(), second)).status_code == 303
    fresh = web.browser()  # `public` is in by now: a browser without a session sees the refusals
    for garbage in ("", "short", "x" * 43, "../../etc/passwd"):
        assert (await fresh.post("/login", data={"t": garbage}, headers=SAME_SITE)).status_code == 400


@respx.mock(assert_all_called=False)
async def test_a_browser_that_is_in_goes_on_from_a_spent_link(web, respx_mock: respx.MockRouter) -> None:
    """A user pressed «Войти в панель», got in, then pressed again (a second tab with the same link) and
    took «По этой ссылке уже вошли» for a failed sign-in. A browser with a live session now goes on to the panel from a
    spent, outdated or replaced link and from a second press; only a browser without a session sees the refusal."""
    public, runtime = web.public, web.runtime
    tg = FakeTelegram(respx_mock)
    token = await _open(runtime, tg)
    for _tab in range(2):  # two tabs of one browser opened the link: both show the button, nothing is spent
        page = await public.get(f"/login?t={token}")
        assert page.status_code == 200 and 'method="post"' in page.text
    assert (await _sign_in(public, token)).status_code == 303  # the first tab
    second_tab = await _sign_in(public, token)
    assert second_tab.status_code == 303 and second_tab.headers["location"] == "/"
    assert "set-cookie" not in second_tab.headers  # the session of the first press, not a new one
    reopened = await public.get(f"/login?t={token}")  # the link opened again from the chat
    assert reopened.status_code == 303 and reopened.headers["location"] == "/"
    assert (await public.get(f"/login?t={token}&next=/vacancies")).headers["location"] == "/vacancies"
    async with runtime.session_factory() as db:
        expired_url, _ = await web_login.issue_link(
            db, runtime.settings, chat_id="42", now=utcnow() - timedelta(hours=1)
        )
        await db.commit()
    expired = expired_url.rsplit("t=", 1)[1]
    for response in (await public.get(f"/login?t={expired}"), await _sign_in(public, expired)):
        assert response.status_code == 303 and response.headers["location"] == "/"
    assert await _count(runtime, WebSession) == 1
    journal = await _audit(runtime)
    assert not any(m.startswith("Вход по ссылке отклонён") for m in journal)  # nothing failed
    # another browser with the same link: refused as before
    other = web.browser()
    assert (await _sign_in(other, token)).status_code == 400 and (
        await other.get(f"/login?t={token}")
    ).status_code == 400
    # after «Выйти» this browser has no session either: the refusal again
    assert (await public.post("/api/session/logout", headers=SAME_SITE)).status_code == 200
    again = await public.get(f"/login?t={token}")
    assert again.status_code == 400 and "уже вошли" in again.text
    assert (await _sign_in(public, token)).status_code == 400


async def test_two_clicks_at_once_give_one_session(web) -> None:
    runtime = web.runtime
    async with runtime.session_factory() as db:
        url, _ = await web_login.issue_link(db, runtime.settings, chat_id="42")
        await db.commit()
    token = url.rsplit("t=", 1)[1]
    clients = [web.browser() for _ in range(2)]
    results = await asyncio.gather(*(_sign_in(c, token) for c in clients))
    assert sorted(r.status_code for r in results) == [303, 400]
    assert await _count(runtime, WebSession) == 1


@respx.mock(assert_all_called=False)
async def test_open_only_from_the_owners_private_chat(web, respx_mock: respx.MockRouter) -> None:
    runtime = web.runtime
    tg = FakeTelegram(respx_mock)
    tg.say("/open", chat=99, sender=99)  # a stranger: ignored, nothing is sent anywhere
    tg.say("/open", sender=7)  # the owner's chat, someone else's message (Telegram never does this; refused anyway)
    summary = await poll_commands(runtime)
    assert summary["ignored"] == 1 and all(m["chat_id"] == str(OWNER) for m in tg.sent)
    assert len(tg.sent) == 1 and "только в личном чате" in tg.sent[0]["text"]
    assert not LINK_RE.search(tg.sent[0]["text"]) and await _count(runtime, LoginToken) == 0
    # a group as the configured chat: a link there would be a link for every member
    runtime.settings = runtime.settings.model_copy(update={"telegram_chat_id": "-1001"})
    tg.say("/open", chat=-1001, chat_type="supergroup", sender=OWNER)
    await poll_commands(runtime)
    assert "только в личном чате" in tg.sent[-1]["text"] and await _count(runtime, LoginToken) == 0
    # the public address off: /open says so
    runtime.settings = runtime.settings.model_copy(update={"telegram_chat_id": str(OWNER), "panel_public_url": ""})
    tg.say("/open")
    tg.say("/help")
    await poll_commands(runtime)
    assert "PANEL_PUBLIC_URL" in tg.sent[-2]["text"] and "/open" not in tg.sent[-1]["text"]
    runtime.settings = runtime.settings.model_copy(update={"panel_public_url": PUBLIC})
    tg.say("/help")
    await poll_commands(runtime)
    assert "/open" in tg.sent[-1]["text"] and "/logout" in tg.sent[-1]["text"]
    assert all(m["link_preview_options"] == {"is_disabled": True} for m in tg.sent)


@respx.mock(assert_all_called=False)
async def test_public_entrance_needs_a_session(web, respx_mock: respx.MockRouter) -> None:
    public, runtime = web.public, web.runtime
    for method, path in (
        ("GET", "/api/health"),
        ("GET", "/api/vacancies"),
        ("GET", "/api/session"),
        ("GET", "/api/auth/status"),
        ("POST", "/api/jobs/pause"),
        ("GET", "/api/stats/export?kind=applications"),
    ):
        r = await public.request(method, path, headers=SAME_SITE)
        assert r.status_code == 401 and r.json()["kind"] == "login_required", path
        assert r.headers["strict-transport-security"] == "max-age=31536000" and r.headers["cache-control"] == "no-store"
    page = await public.get("/vacancies?group=new")  # a page of the interface: to /login, by a refresh of this page
    assert page.status_code == 200 and 'http-equiv="refresh"' in page.text
    assert "/login?next=/vacancies%3Fgroup%3Dnew" in page.text
    login = await public.get("/login")
    assert login.status_code == 200 and "/open" in login.text
    for path in ("/docs", "/openapi.json", "/redoc"):
        assert (await public.get(path)).status_code == 404, path
    assert (await public.get("/favicon.ico")).status_code in (200, 404)  # a file of the interface: no sign-in asked
    assert (await public.get("/api/health", headers={"Host": "other.example"})).status_code == 400

    tg = FakeTelegram(respx_mock)
    assert (await _sign_in(public, await _open(runtime, tg))).status_code == 303
    health = await public.get("/api/health")
    assert health.status_code == 200 and health.json()["ok"] is True
    info = (await public.get("/api/session")).json()
    assert info["via"] == "link" and info["expires_at"] > info["created_at"]
    for path in ("/docs", "/openapi.json"):
        assert (await public.get(path)).status_code == 404, path
    # a state-changing request only from the panel's own page
    assert (await public.post("/api/jobs/pause", json={"reason": "user"}, headers=SAME_SITE)).status_code == 200
    assert (await public.post("/api/jobs/resume", headers={"Sec-Fetch-Site": "same-origin"})).status_code == 200
    # `Origin: null` comes from a page with a strict referrer policy: only the browser's own Sec-Fetch-Site vouches then
    hidden = {"Origin": "null", "Sec-Fetch-Site": "same-origin"}
    assert (await public.post("/api/jobs/resume", headers=hidden)).status_code == 200
    for headers in (
        {"Origin": "https://evil.example"},
        {"Origin": "https://evil.example", "Sec-Fetch-Site": "same-origin"},
        {"Origin": "null"},
        {"Origin": "null", "Sec-Fetch-Site": "cross-site"},
        {},
        {"Sec-Fetch-Site": "cross-site"},
    ):
        assert (await public.post("/api/jobs/pause", json={"reason": "x"}, headers=headers)).status_code == 403, headers
    # with a session /login leads on; the place to go is a path of this site only
    to = await public.get("/login?next=/vacancies%3Fgroup%3Dnew")
    assert to.status_code == 303 and to.headers["location"] == "/vacancies?group=new"
    assert (await public.get("/login?next=//evil.example")).headers["location"] == "/"
    if (FRONTEND_DIST / "index.html").exists():
        shell = await public.get("/vacancies")
        assert shell.status_code == 200 and shell.content == (FRONTEND_DIST / "index.html").read_bytes()
    answer = await public.get("/api/health")
    csp = answer.headers["content-security-policy"]
    assert (
        "frame-ancestors 'none'" in csp and "script-src 'self'" in csp and answer.headers["x-frame-options"] == "DENY"
    )
    # same-origin, not no-referrer: with no-referrer the browser sends `Origin: null` with the panel's own POSTs
    assert answer.headers["referrer-policy"] == "same-origin" and answer.headers["x-content-type-options"] == "nosniff"
    assert 'content="same-origin"' in (await web.browser().get("/login")).text


@respx.mock(assert_all_called=False)
async def test_logout_here_and_everywhere(web, respx_mock: respx.MockRouter) -> None:
    public, runtime = web.public, web.runtime
    tg = FakeTelegram(respx_mock)
    other = web.browser()
    assert (await _sign_in(public, await _open(runtime, tg))).status_code == 303
    assert (await _sign_in(other, await _open(runtime, tg))).status_code == 303
    assert (await public.get("/api/health")).status_code == 200 and (await other.get("/api/health")).status_code == 200

    out = await public.post("/api/session/logout", headers=SAME_SITE)  # «Выйти» in one browser
    assert out.status_code == 200 and "max-age=0" in out.headers["set-cookie"].lower()
    assert "__host-jobpanel" in out.headers["set-cookie"].lower() and "secure" in out.headers["set-cookie"].lower()
    assert (await public.get("/api/health")).status_code == 401 and (await other.get("/api/health")).status_code == 200

    pending = await _open(runtime, tg)  # a link not used yet
    tg.say("/logout")
    await poll_commands(runtime)
    assert "Все входы в панель закрыты (сессий: 1)" in tg.sent[-1]["text"]
    assert tg.sent[-1]["link_preview_options"] == {"is_disabled": True}
    assert (await other.get("/api/health")).status_code == 401
    assert "отменила" in (await _sign_in(other, pending)).text
    journal = await _audit(runtime)
    assert "Выход из панели в браузере" in journal
    assert "Выход из панели во всех браузерах командой бота /logout (сессий: 1)" in journal


async def test_tunnel_works_as_before(web) -> None:
    local, runtime = web.local, web.runtime
    health = await local.get("/api/health")
    assert health.status_code == 200 and "strict-transport-security" not in health.headers
    assert "content-security-policy" not in health.headers
    assert (await local.post("/api/jobs/pause", json={"reason": "user"})).status_code == 200  # no session, no Origin
    assert (await local.post("/api/jobs/resume", headers={"Origin": "http://localhost:8000"})).status_code == 200
    evil = {"Origin": "https://evil.example"}
    assert (await local.post("/api/jobs/pause", json={"reason": "x"}, headers=evil)).status_code == 403
    assert (await local.get("/api/session")).json() == {"via": "local"}
    assert (await local.get("/login")).status_code == 404 and (
        await local.post("/login", data={"t": "x"})
    ).status_code == 404
    assert (await local.post("/api/session/logout")).status_code == 404
    # the public name or a proxied request on the tunnel's entrance: a misconfigured proxy, not the tunnel
    assert (await local.get("/api/health", headers={"Host": "panel.example"})).status_code == 400
    for header in ("X-Forwarded-For", "Forwarded", "X-Real-IP"):
        assert (await local.get("/api/health", headers={header: "203.0.113.5"})).status_code == 403, header
    # without the public address the laptop is untouched: proxy headers pass as they always did
    runtime.settings = runtime.settings.model_copy(update={"panel_public_url": ""})
    assert (await local.get("/api/health", headers={"X-Forwarded-For": "203.0.113.5"})).status_code == 200
    assert (await local.get("/login")).status_code == 404


@respx.mock(assert_all_called=False)
async def test_every_sign_in_is_reported_to_the_bot(web, respx_mock: respx.MockRouter) -> None:
    public, runtime = web.public, web.runtime
    tg = FakeTelegram(respx_mock)
    assert (await _sign_in(public, await _open(runtime, tg))).status_code == 303
    async with runtime.session_factory() as db:
        rows = (await db.execute(select(Notification).where(Notification.event_type == "login"))).scalars().all()
        assert {(r.channel, r.status) for r in rows} == {("ui", "sent"), ("telegram", "queued")}
        status = await telegram_status(db, runtime.settings)
        assert next(e for e in status["events"] if e["id"] == "login")["enabled"] is True
        summary = await deliver_pending(db, runtime.settings, runtime.http)
        await db.commit()
    assert summary["sent"] == 1
    notice = tg.sent[-1]["text"]
    assert notice.startswith("🔐 <b>Вход в панель по ссылке</b>")
    assert "Chrome 141, Windows" in notice and "IP 127.0.0.1" in notice and "/logout" in notice


async def test_sign_in_attempts_are_limited(web) -> None:
    public, runtime = web.public, web.runtime
    for _ in range(10):
        assert (await public.post("/login", data={"t": "x" * 43}, headers=SAME_SITE)).status_code == 400
    limited = await public.post("/login", data={"t": "x" * 43}, headers=SAME_SITE)
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0
    big = await public.post(
        "/login",
        content=b"t=" + b"x" * 5000,
        headers={**SAME_SITE, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert big.status_code == 413
    assert (await public.post("/login", data={"t": "x"}, headers={"Origin": "https://evil.example"})).status_code == 403
    assert sum(m.startswith("Вход по ссылке отклонён") for m in await _audit(runtime)) == 10


@respx.mock(assert_all_called=False)
async def test_token_and_cookie_never_reach_logs_journal_or_feed(
    web, respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    public, runtime = web.public, web.runtime
    caplog.set_level("DEBUG")
    tg = FakeTelegram(respx_mock)
    token = await _open(runtime, tg)
    await public.get(f"/login?t={token}")
    assert (await _sign_in(public, token)).status_code == 303
    cookie = public.cookies["__Host-jobpanel"]
    await public.get("/api/health")
    await public.post("/api/session/logout", headers=SAME_SITE)
    async with runtime.session_factory() as db:
        journal = " ".join(f"{r.message} {r.context_json}" for r in (await db.execute(select(AuditLog))).scalars())
        feed = " ".join(f"{r.payload_json} {r.text}" for r in (await db.execute(select(Notification))).scalars())
    for secret in (token, cookie, TOKEN):
        assert secret not in caplog.text and secret not in journal and secret not in feed


async def test_two_listeners_on_real_sockets(settings: Settings) -> None:
    """The real thing: app.server binds the tunnel's port and the proxy's port, uvicorn serves both from one loop, and
    the entrance is told by the socket — the same 127.0.0.1 client gets the tunnel's rules on one port and the sign-in
    on the other. The proxy's X-Forwarded-For is the client address of a sign-in (uvicorn trusts 127.0.0.1)."""
    s = settings.model_copy(
        update={"panel_public_url": PUBLIC, "app_host": "127.0.0.1", "app_port": 0, "panel_public_port": 0}
    )
    sockets = listening_sockets(s)
    main_port, public_port = (sock.getsockname()[1] for sock in sockets)
    assert all(sock.getsockname()[0] == "127.0.0.1" for sock in sockets)
    runtime = await Runtime.create(s.model_copy(update={"app_port": main_port, "panel_public_port": public_port}))
    app.state.runtime = runtime
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_config=None, access_log=False))
    serving = asyncio.create_task(server.serve(sockets=sockets))
    try:
        for _ in range(250):
            if server.started:
                break
            await asyncio.sleep(0.02)
        tunnel, proxy = f"http://127.0.0.1:{main_port}", f"http://127.0.0.1:{public_port}"
        via_proxy = {"Host": "panel.example", "X-Forwarded-For": "203.0.113.5", "X-Forwarded-Proto": "https"}
        async with httpx.AsyncClient() as http:
            assert (await http.get(f"{tunnel}/api/health")).status_code == 200
            assert (await http.get(f"{proxy}/api/health", headers=via_proxy)).status_code == 401
            assert (await http.get(f"{tunnel}/api/health", headers={"Host": "panel.example"})).status_code == 400
            assert (await http.get(f"{tunnel}/api/health", headers={"X-Forwarded-For": "1.2.3.4"})).status_code == 403
            async with runtime.session_factory() as db:
                url, _ = await web_login.issue_link(db, runtime.settings, chat_id="42")
                await db.commit()
            signed = await http.post(
                f"{proxy}/login", data={"t": url.rsplit("t=", 1)[1]}, headers={**via_proxy, **SAME_SITE}
            )
            assert signed.status_code == 303
            pair = signed.headers["set-cookie"].split(";", 1)[0]
            health = await http.get(f"{proxy}/api/health", headers={**via_proxy, "Cookie": pair})
            assert health.status_code == 200 and health.json()["ok"] is True
        async with runtime.session_factory() as db:
            assert (await db.execute(select(WebSession.ip))).scalar_one() == "203.0.113.5"
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 15)
        await runtime.close()
