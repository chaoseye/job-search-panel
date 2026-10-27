"""Sign-in from a browser through the public address (README, «Вход из браузера»): the bot sends a one-time link
to the owner's chat (/open), the link page has a button, the button's POST spends the link and gives the browser a
session cookie.

- The link token: 256 random bits (`secrets.token_urlsafe(32)`), 5 minutes, one use; the base keeps only its SHA-256.
  A newer link or /logout revokes the links not used yet. Spending is one UPDATE … WHERE unused AND not expired, so
  two clicks at once give one session.
- GET of the link never spends it: link previews and link scanners open links before the person does.
- The session: a random cookie value (only its SHA-256 in the base), PANEL_SESSION_HOURS long; «Выйти» ends one,
  /logout in the bot ends all.
- Neither the token nor the cookie is ever logged or written to the journal (structlog masks keys with «token», and
  nothing here passes them anywhere but to the hash).
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import LoginToken, WebSession

LINK_TTL = timedelta(minutes=5)
LOGIN_PATH = "/login"
# `secrets.token_urlsafe(32)`: 43 characters of the URL-safe base64 alphabet — anything else is not ours
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
# links and ended sessions are kept this long for the record, then removed when a new link is issued
KEEP_DAYS = 30


def digest(raw: str) -> str:
    """What the base keeps instead of a token or a cookie: 256 random bits need no salt or slow hash."""
    return hashlib.sha256(raw.encode()).hexdigest()


def cookie_name(settings: Settings) -> str:
    """`__Host-` makes the browser keep the cookie only with Secure, Path=/ and no Domain — for exactly the public host.
    Plain http (a local check, config allows it only for localhost names) cannot carry that prefix."""
    return "__Host-jobpanel" if settings.public_url_secure else "jobpanel_session"


def well_formed(raw: str | None) -> bool:
    return bool(raw) and TOKEN_RE.fullmatch(raw or "") is not None


# ---- links -------------------------------------------------------------------------------


async def issue_link(
    session: AsyncSession, settings: Settings, *, chat_id: str | None, now: datetime | None = None
) -> tuple[str, datetime]:
    """A new one-time link (url, expires_at); the links not used yet stop working."""
    if not settings.panel_public_url:
        raise ValueError("PANEL_PUBLIC_URL не задан")
    now = now or utcnow()
    await session.execute(
        update(LoginToken)
        .where(LoginToken.used_at.is_(None), LoginToken.revoked_at.is_(None))
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    old = now - timedelta(days=KEEP_DAYS)
    await session.execute(delete(LoginToken).where(LoginToken.created_at < old))
    await session.execute(delete(WebSession).where(WebSession.expires_at < old))
    raw = secrets.token_urlsafe(32)
    expires = now + LINK_TTL
    session.add(LoginToken(token_hash=digest(raw), chat_id=chat_id, created_at=now, expires_at=expires))
    await session.flush()
    return f"{settings.panel_public_url}{LOGIN_PATH}?t={raw}", expires


async def link_state(session: AsyncSession, raw: str | None, *, now: datetime | None = None) -> tuple[str, Any]:
    """What a link is now, without spending it: (ok | unknown | used | revoked | expired, expires_at)."""
    if not well_formed(raw):
        return "unknown", None
    now = now or utcnow()
    row = (
        await session.execute(select(LoginToken).where(LoginToken.token_hash == digest(str(raw))))
    ).scalar_one_or_none()
    if row is None:
        return "unknown", None
    expires = as_utc(row.expires_at)
    if row.used_at is not None:
        return "used", expires
    if row.revoked_at is not None:
        return "revoked", expires
    if expires is None or expires <= now:
        return "expired", expires
    return "ok", expires


async def redeem(
    session: AsyncSession, raw: str | None, *, now: datetime | None = None
) -> tuple[LoginToken | None, str]:
    """Spends a link: (the token row, "ok") or (None, why not). One UPDATE with every condition in WHERE: of two
    simultaneous clicks only one finds the link unused (SQLite has one writer)."""
    if not well_formed(raw):
        return None, "unknown"
    now = now or utcnow()
    token_hash = digest(str(raw))
    result = await session.execute(
        update(LoginToken)
        .where(
            LoginToken.token_hash == token_hash,
            LoginToken.used_at.is_(None),
            LoginToken.revoked_at.is_(None),
            LoginToken.expires_at > now,
        )
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    )
    if getattr(result, "rowcount", 0) == 1:
        row = (await session.execute(select(LoginToken).where(LoginToken.token_hash == token_hash))).scalar_one()
        return row, "ok"
    state, _expires = await link_state(session, raw, now=now)
    return None, state if state != "ok" else "unknown"


# ---- sessions ----------------------------------------------------------------------------


async def start_session(
    session: AsyncSession,
    settings: Settings,
    *,
    token: LoginToken | None,
    ip: str | None,
    user_agent: str | None,
    now: datetime | None = None,
) -> tuple[str, WebSession]:
    """A new browser session: (the cookie value, the row)."""
    now = now or utcnow()
    raw = secrets.token_urlsafe(32)
    row = WebSession(
        session_hash=digest(raw),
        login_token_id=token.id if token is not None else None,
        ip=(ip or "")[:64] or None,
        user_agent=(user_agent or "")[:300] or None,
        created_at=now,
        expires_at=now + timedelta(hours=settings.panel_session_hours),
    )
    session.add(row)
    await session.flush()
    return raw, row


async def find_session(session: AsyncSession, raw: str | None, *, now: datetime | None = None) -> WebSession | None:
    """The live session of a cookie value: not ended, not expired."""
    if not well_formed(raw):
        return None
    now = now or utcnow()
    return (
        await session.execute(
            select(WebSession).where(
                WebSession.session_hash == digest(str(raw)),
                WebSession.revoked_at.is_(None),
                WebSession.expires_at > now,
            )
        )
    ).scalar_one_or_none()


async def end_session(session: AsyncSession, session_id: int, *, now: datetime | None = None) -> None:
    await session.execute(
        update(WebSession)
        .where(WebSession.id == session_id, WebSession.revoked_at.is_(None))
        .values(revoked_at=now or utcnow())
        .execution_options(synchronize_session=False)
    )


async def end_all(session: AsyncSession, *, now: datetime | None = None) -> int:
    """/logout in the bot: every live session ends, every link not used yet is revoked. Returns the sessions ended."""
    now = now or utcnow()
    result = await session.execute(
        update(WebSession)
        .where(WebSession.revoked_at.is_(None), WebSession.expires_at > now)
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(LoginToken)
        .where(LoginToken.used_at.is_(None), LoginToken.revoked_at.is_(None))
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )
    return int(getattr(result, "rowcount", 0) or 0)


# ---- attempts ----------------------------------------------------------------------------


class LoginAttempts:
    """Sign-in attempts (POST /login) in a sliding window: per client address and in total. A link is 256 random bits,
    so guessing is hopeless anyway; the limit keeps a flood of forms from loading the panel and the base."""

    def __init__(
        self,
        *,
        per_ip: int = 10,
        total: int = 60,
        window_sec: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_ip, self.total, self.window = per_ip, total, window_sec
        self._clock = clock
        self._by_ip: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()

    def _prune(self, q: deque[float], now: float) -> None:
        while q and q[0] <= now - self.window:
            q.popleft()

    def check(self, ip: str | None) -> float:
        """0 — the attempt is allowed and counted; otherwise seconds until the next one is."""
        now = self._clock()
        key = ip or "?"
        mine = self._by_ip.setdefault(key, deque())
        self._prune(mine, now)
        self._prune(self._all, now)
        for q, limit in ((mine, self.per_ip), (self._all, self.total)):
            if len(q) >= limit:
                return max(1.0, q[0] + self.window - now)
        mine.append(now)
        self._all.append(now)
        if len(self._by_ip) > 1000:  # addresses that only knocked once do not pile up
            for k in [k for k, q in self._by_ip.items() if not q or q[-1] <= now - self.window]:
                del self._by_ip[k]
        return 0.0


# ---- small helpers -----------------------------------------------------------------------


def safe_next(value: str | None) -> str:
    """Where to go after the sign-in: a path of this site only (no `//host`, no scheme, no backslashes)."""
    v = (value or "").strip()
    if (
        not v.startswith("/")
        or v.startswith("//")
        or "\\" in v
        or len(v) > 500
        or v.startswith(LOGIN_PATH)
        or any(ord(c) < 32 for c in v)
    ):
        return "/"
    return v


_BROWSERS = (
    ("Яндекс Браузер", re.compile(r"YaBrowser/(\d+)")),
    ("Edge", re.compile(r"Edg(?:e|A|iOS)?/(\d+)")),
    ("Opera", re.compile(r"(?:OPR|Opera)/(\d+)")),
    ("Firefox", re.compile(r"(?:Firefox|FxiOS)/(\d+)")),
    ("Chrome", re.compile(r"(?:Chrome|CriOS)/(\d+)")),
    ("Safari", re.compile(r"Version/(\d+)[\d.]*(?: Mobile/\S+)? Safari/")),
)
_SYSTEMS = (
    ("iOS", re.compile(r"iPhone|iPad|iPod")),
    ("Android", re.compile(r"Android")),
    ("Windows", re.compile(r"Windows")),
    ("macOS", re.compile(r"Macintosh|Mac OS X")),
    ("Linux", re.compile(r"Linux|X11")),
)


def describe_browser(user_agent: str | None) -> str:
    """«Chrome 141, Windows» from a User-Agent — for the sign-in message; the header is the client's word, not proof."""
    ua = (user_agent or "").strip()
    if not ua:
        return "браузер не назвался"
    browser = next((f"{name} {m.group(1)}" for name, rx in _BROWSERS if (m := rx.search(ua))), None)
    system = next((name for name, rx in _SYSTEMS if rx.search(ua)), None)
    if browser is None and system is None:
        return ua[:60]
    return ", ".join(x for x in (browser or "неизвестный браузер", system) if x)
