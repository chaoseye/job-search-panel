"""Telegram transport: sendMessage with HTML, retries on network errors and 429, ≤ 20 messages/min.

The bot token lives only in `.env`; it never appears in logs, errors, API responses or messages (every error string
is passed through `_safe`, which blanks the token).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import Notification
from app.net import TRANSPORT_ERRORS
from app.services.audit import audit
from app.services.notify import CHUNK_SIZES, IMMEDIATE_EVENTS, MERGE_WINDOW_SEC, render_event
from app.services.state import get_setting, set_setting

log = structlog.get_logger("telegram")

API_BASE = "https://api.telegram.org"
MAX_PER_MINUTE = 20
# a message is retried until it is this old: transient failures (network, 5xx, 429) never give up earlier — a laptop
# asleep or offline for a few hours still gets its letters and digests (5 attempts ≈ 9 minutes lost 9 messages 21.09)
STALE_AFTER = timedelta(hours=24)
RETRY_BACKOFF_SEC = (30, 60, 120, 300, 600)  # the last delay repeats
CHAT_ID_SETTING = "telegram_chat_id"
BOT_SETTING = "telegram_bot"  # cached getMe result: {"username", "name", "token_fp"}


class TelegramError(Exception):
    def __init__(
        self, message: str, *, status: int | None = None, retry_after: float | None = None, transient: bool = False
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.transient = transient


class TelegramClient:
    def __init__(self, http: httpx.AsyncClient, token: str) -> None:
        self._http = http
        self._token = token

    def _safe(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    async def call(self, method: str, **params: Any) -> Any:
        url = f"{API_BASE}/bot{self._token}/{method}"
        try:
            resp = await self._http.post(url, json=params, timeout=httpx.Timeout(20.0))
        except TRANSPORT_ERRORS as exc:
            raise TelegramError(self._safe(f"сеть: {type(exc).__name__}"), transient=True) from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code == 200 and isinstance(body, dict) and body.get("ok"):
            return body.get("result")
        description = str(body.get("description") if isinstance(body, dict) else "") or f"HTTP {resp.status_code}"
        retry_after = None
        if isinstance(body, dict) and isinstance(body.get("parameters"), dict):
            try:
                retry_after = float(body["parameters"].get("retry_after"))
            except (TypeError, ValueError):
                retry_after = None
        transient = resp.status_code == 429 or resp.status_code >= 500
        raise TelegramError(
            self._safe(f"Telegram {resp.status_code}: {description}"),
            status=resp.status_code,
            retry_after=retry_after,
            transient=transient,
        )

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        link_preview_options: dict[str, Any] | None = None,
    ) -> Any:
        """`link_preview_options` (Bot API 7.0) replaces the older `disable_web_page_preview`: replies to commands use
        it, so a sign-in link is never opened by Telegram for a preview."""
        params: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if link_preview_options is not None:
            params["link_preview_options"] = link_preview_options
        else:
            params["disable_web_page_preview"] = True
        if reply_markup:
            params["reply_markup"] = reply_markup
        return await self.call("sendMessage", **params)

    async def get_updates(self) -> list[dict[str, Any]]:
        result = await self.call("getUpdates", timeout=0, allowed_updates=["message", "my_chat_member"])
        return [u for u in (result or []) if isinstance(u, dict)]

    async def get_me(self) -> dict[str, Any]:
        result = await self.call("getMe")
        return result if isinstance(result, dict) else {}


# ---- configuration ------------------------------------------------------------------


async def effective_chat_id(session: AsyncSession, settings: Settings) -> str | None:
    override = await get_setting(session, CHAT_ID_SETTING)
    value = str(override).strip() if override else (settings.telegram_chat_id or "").strip()
    return value or None


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


async def remember_bot(session: AsyncSession, settings: Settings, me: dict[str, Any]) -> dict[str, Any]:
    """Caches the bot identity (username/name) for the UI; keyed by a token fingerprint so a new token re-fetches."""
    info = {
        "username": str(me.get("username") or "") or None,
        "name": str(me.get("first_name") or "") or None,
        "token_fp": _token_fingerprint(settings.telegram_bot_token),
    }
    await set_setting(session, BOT_SETTING, info)
    return info


async def bot_identity(
    session: AsyncSession, settings: Settings, http: httpx.AsyncClient | None = None
) -> dict[str, Any] | None:
    """Cached `getMe` for the current token; fetched once when `http` is given and nothing is cached yet."""
    if not settings.telegram_bot_token:
        return None
    cached = await get_setting(session, BOT_SETTING)
    fp = _token_fingerprint(settings.telegram_bot_token)
    if isinstance(cached, dict) and cached.get("token_fp") == fp:
        return cached
    if http is None:
        return None
    try:
        me = await TelegramClient(http, settings.telegram_bot_token).get_me()
    except TelegramError as exc:
        log.info("bot_identity_unavailable", error=str(exc))
        return None
    return await remember_bot(session, settings, me)


async def telegram_status(
    session: AsyncSession, settings: Settings, http: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    from app.services.notify import EVENT_LABELS, EVENT_TYPES, enabled_events, queue_counts
    from app.services.resume_bump import reminder_status
    from app.services.tg_filter import stop_words
    from app.services.tg_letters import letters_config

    chat_id = await effective_chat_id(session, settings)
    bot = await bot_identity(session, settings, http)
    enabled = enabled_events(settings)
    last_sent = (
        await session.execute(
            select(func.max(Notification.sent_at)).where(
                Notification.channel == "telegram", Notification.status == "sent"
            )
        )
    ).scalar()
    last_sent_dt = as_utc(last_sent)
    return {
        "token_set": bool(settings.telegram_bot_token),
        "chat_id": chat_id,
        "chat_id_source": "settings" if await get_setting(session, CHAT_ID_SETTING) else ("env" if chat_id else None),
        "configured": bool(settings.telegram_bot_token and chat_id),
        "bot_username": bot.get("username") if bot else None,
        "bot_name": bot.get("name") if bot else None,
        "bot_link": f"https://t.me/{bot['username']}" if bot and bot.get("username") else None,
        "daily_summary_at": settings.telegram_daily_summary_at,
        "resume_bump": reminder_status(settings),
        "hot_digest_at": settings.hot_digest_at.strip() or None,
        "letters": await letters_config(session, settings),
        "stop_words": await stop_words(session),
        "events": [{"id": e, "label": EVENT_LABELS[e], "enabled": e in enabled} for e in EVENT_TYPES],
        "queue": await queue_counts(session),
        "last_sent_at": last_sent_dt.isoformat(timespec="seconds") if last_sent_dt else None,
    }


async def detect_chat_id(session: AsyncSession, settings: Settings, http: httpx.AsyncClient) -> dict[str, Any]:
    """`getUpdates` helper: the user writes /start to the bot, the panel picks the chat id."""
    if not settings.telegram_bot_token:
        raise TelegramError("TELEGRAM_BOT_TOKEN не задан в .env")
    client = TelegramClient(http, settings.telegram_bot_token)
    updates = await client.get_updates()
    for upd in reversed(updates):
        msg = upd.get("message") if isinstance(upd.get("message"), dict) else None
        chat = msg.get("chat") if msg and isinstance(msg.get("chat"), dict) else None
        if chat is None and isinstance(upd.get("my_chat_member"), dict):
            chat = upd["my_chat_member"].get("chat") if isinstance(upd["my_chat_member"].get("chat"), dict) else None
        if chat and chat.get("id") is not None:
            chat_id = str(chat["id"])
            await set_setting(session, CHAT_ID_SETTING, chat_id)
            await audit(session, "telegram", f"chat_id определён по сообщению боту: {chat_id}")
            name = " ".join(str(chat.get(k) or "") for k in ("first_name", "last_name", "title", "username")).strip()
            return {"chat_id": chat_id, "chat_title": name or None}
    raise TelegramError("Бот не получал сообщений. Напишите боту /start в Telegram и повторите.")


async def send_test(session: AsyncSession, settings: Settings, http: httpx.AsyncClient) -> dict[str, Any]:
    if not settings.telegram_bot_token:
        raise TelegramError("TELEGRAM_BOT_TOKEN не задан в .env")
    chat_id = await effective_chat_id(session, settings)
    if not chat_id:
        raise TelegramError("chat_id не задан: нажмите «Определить chat_id» или укажите TELEGRAM_CHAT_ID в .env")
    client = TelegramClient(http, settings.telegram_bot_token)
    me = await client.get_me()
    await remember_bot(session, settings, me)
    await client.send_message(chat_id, "✅ <b>Job Search Panel</b>: тестовое сообщение. Уведомления настроены.")
    await audit(session, "telegram", "Тестовое сообщение отправлено")
    return {"ok": True, "bot": me.get("username"), "chat_id": chat_id}


# ---- delivery ------------------------------------------------------------------------


async def _sent_last_minute(session: AsyncSession, now: datetime) -> int:
    """Telegram messages (not notification rows) sent in the last minute. The rows merged into one message share its
    `sent_at` and `text`: ten channel posts in one message used to spend ten of the 20-per-minute budget."""
    since = now - timedelta(seconds=60)
    messages = (
        select(Notification.sent_at)
        .where(Notification.channel == "telegram", Notification.status == "sent", Notification.sent_at >= since)
        .group_by(Notification.sent_at, Notification.event_type, Notification.text)
        .subquery()
    )
    return int((await session.execute(select(func.count()).select_from(messages))).scalar() or 0)


async def deliver_pending(
    session: AsyncSession,
    settings: Settings,
    http: httpx.AsyncClient,
    *,
    now: datetime | None = None,
    max_per_minute: int = MAX_PER_MINUTE,
    force: bool = False,
) -> dict[str, Any]:
    """Sends due `telegram` rows grouped by event type (one message per type). Returns a small summary.

    `force=True` (manual flush) ignores the merge window; retries and the per-minute cap still apply."""
    now = now or utcnow()
    summary: dict[str, Any] = {"sent": 0, "failed": 0, "deferred": 0, "messages": 0}
    if not settings.telegram_bot_token:
        return summary
    rows = (
        (
            await session.execute(
                select(Notification)
                .where(Notification.channel == "telegram", Notification.status == "queued")
                .order_by(Notification.created_at, Notification.id)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return summary
    chat_id = await effective_chat_id(session, settings)
    budget = max(0, max_per_minute - await _sent_last_minute(session, now))
    client = TelegramClient(http, settings.telegram_bot_token)

    groups: dict[str, list[Notification]] = {}
    for row in rows:
        created = as_utc(row.created_at) or now
        if created < now - STALE_AFTER:
            row.status, row.error = "failed", "устарело (не отправлено за 24 ч)"
            summary["failed"] += 1
            continue
        next_at = as_utc(row.next_attempt_at)
        if next_at is not None and next_at > now:
            summary["deferred"] += 1
            continue
        groups.setdefault(row.event_type, []).append(row)

    for event_type, group in groups.items():
        oldest = min(as_utc(r.created_at) or now for r in group)
        if not force and event_type not in IMMEDIATE_EVENTS and oldest > now - timedelta(seconds=MERGE_WINDOW_SEC):
            summary["deferred"] += len(group)
            continue  # wait for the merge window to close
        if not chat_id:
            for r in group:
                r.attempts += 1
                r.next_attempt_at = now + timedelta(seconds=RETRY_BACKOFF_SEC[-1])
                r.error = "chat_id не задан"
            summary["deferred"] += len(group)
            continue
        # one message per group; chunked types go out as several messages so no link is dropped
        size = CHUNK_SIZES.get(event_type)
        chunks = [group[i : i + size] for i in range(0, len(group), size)] if size else [group]
        for index, chunk in enumerate(chunks, 1):
            if budget <= 0:
                summary["deferred"] += len(chunk)  # the rest waits for the next minute
                continue
            payloads = []
            for r in chunk:
                try:
                    payloads.append(json.loads(r.payload_json or "{}"))
                except ValueError:
                    payloads.append({})
            part = (index, len(chunks), len(group)) if len(chunks) > 1 else None
            text = render_event(event_type, payloads, timezone=settings.timezone, part=part)
            reply_markup: dict[str, Any] | None = None
            if event_type == "letter" and payloads and payloads[0].get("vacancy_id"):
                from app.services.tg_letters import keyboard

                reply_markup = keyboard(int(payloads[0]["vacancy_id"]), payloads[0].get("apply_url"))
            try:
                result = await client.send_message(chat_id, text, reply_markup=reply_markup)
            except TelegramError as exc:
                _apply_failure(chunk, exc, now)
                failed = [r for r in chunk if r.status == "failed"]
                summary["failed"] += len(failed)
                summary["deferred"] += len(chunk) - len(failed)
                if failed:
                    await audit(
                        session, "telegram", f"Не удалось отправить уведомление ({event_type}): {exc}", level="warning"
                    )
                log.warning("telegram_send_failed", event_type=event_type, error=str(exc), transient=exc.transient)
                continue
            budget -= 1
            summary["messages"] += 1
            for r in chunk:
                r.status = "sent"
                r.sent_at = now
                r.text = text
                r.error = None
            summary["sent"] += len(chunk)
            if reply_markup is not None and isinstance(result, dict) and result.get("message_id"):
                from app.services.tg_letters import remember

                await remember(
                    session,
                    vacancy_id=int(payloads[0]["vacancy_id"]),
                    chat_id=chat_id,
                    message_id=int(result["message_id"]),
                    notification_id=chunk[0].id,
                )
    await session.flush()
    return summary


def _apply_failure(group: list[Notification], exc: TelegramError, now: datetime) -> None:
    """A permanent error (400 chat not found, 403 bot blocked) fails the rows at once; a transient one schedules the
    next attempt — the rows fail only when they are older than STALE_AFTER (checked in deliver_pending)."""
    for r in group:
        r.attempts += 1
        r.error = str(exc)
        if not exc.transient:
            r.status = "failed"
            continue
        if exc.retry_after:
            delay = exc.retry_after
        else:
            delay = RETRY_BACKOFF_SEC[min(r.attempts - 1, len(RETRY_BACKOFF_SEC) - 1)]
        r.next_attempt_at = now + timedelta(seconds=delay)


async def notify_tick(runtime: Any) -> dict[str, Any]:
    """Scheduler entry point: one delivery pass in its own session, then the bot messages of vacancies handled in
    the panel are deleted."""
    from app.services.tg_letters import flush_resolutions

    async with runtime.session_factory() as session:
        summary = await deliver_pending(session, runtime.settings, runtime.http)
        try:
            summary["resolved"] = await flush_resolutions(session, runtime.settings, runtime.http)
        except Exception as exc:  # noqa: BLE001 - never block delivery
            log.warning("letter_resolution_failed", error=str(exc))
        await session.commit()
    if summary["messages"] or summary["failed"]:
        log.info("telegram_delivered", **summary)
    return summary


async def flush_now(runtime: Any) -> dict[str, Any]:
    """Manual flush from the UI: sends everything queued without waiting for the merge window."""
    async with runtime.session_factory() as session:
        summary = await deliver_pending(session, runtime.settings, runtime.http, force=True)
        await session.commit()
    return summary
