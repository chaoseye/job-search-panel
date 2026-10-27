"""Telegram bot commands via long polling: /status, /pause, /resume, /digest, /batch; with
the public address on (PANEL_PUBLIC_URL) also /open (a one-time sign-in link) and /logout (every browser out).

Polled once a minute (`getUpdates` with the stored offset) only when a chat id is configured; messages from any
other chat are ignored (and confirmed) so the bot cannot be driven by strangers. Replies go to the configured chat,
always without link previews: Telegram would otherwise open a sign-in link itself to draw one.
"""

from __future__ import annotations

import html
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.services.audit import audit
from app.services.state import (
    auth_public_state,
    get_hh_auth,
    get_setting,
    get_system_state,
    set_pause,
    set_setting,
)
from app.services.telegram import TelegramClient, TelegramError, effective_chat_id

log = structlog.get_logger("bot_commands")
OFFSET_SETTING = "telegram_update_offset"
HELP = (
    "Команды: /status — состояние панели, /pause — пауза фоновых задач, /resume — снять паузу, "
    "/digest — сводка за сегодня, /hot — свежие вакансии с малой конкуренцией, /channels — новые вакансии из "
    "Telegram-каналов, /batch — собрать пакет для Claude Code"
)
HELP_WEB = (
    ", /open — одноразовая ссылка для входа в панель из браузера (5 минут), /logout — выйти из панели во всех браузерах"
)
NO_PREVIEW = {"is_disabled": True}


def help_text(settings: Settings) -> str:
    return HELP + (HELP_WEB if settings.panel_public_url else "")


def _part(message: dict[str, Any] | None, key: str) -> dict[str, Any]:
    value = message.get(key) if message else None
    return dict(value) if isinstance(value, dict) else {}


def _private_owner(message: dict[str, Any] | None) -> bool:
    """The command came from a private chat of its own sender (the configured chat is checked by the poller): a link
    sent to a group would be a link for every member."""
    chat, sender = _part(message, "chat"), _part(message, "from")
    return chat.get("type") == "private" and sender.get("id") is not None and str(sender["id"]) == str(chat.get("id"))


def _cmd(text: str) -> str | None:
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    return text.split()[0].split("@")[0].lower()


async def _status_text(session: AsyncSession, settings: Settings) -> str:
    from sqlalchemy import func, select

    from app.db.models import ClaudeBatch
    from app.services.applier import limits_status
    from app.services.stats import compute_stats

    auth = auth_public_state(await get_hh_auth(session))
    state = await get_system_state(session)
    limits = await limits_status(session, settings)
    stats = await compute_stats(session, settings, period_key="today", use_cache=False)
    m = stats["metrics"]
    pending = int(
        (await session.execute(select(func.count(ClaudeBatch.id)).where(ClaudeBatch.status == "pending"))).scalar() or 0
    )
    lines = [
        "📋 <b>Job Search Panel</b>",
        f"hh.ru: {'вход выполнен' if auth['logged_in'] else 'нет входа'} · режим {state.apply_mode}",
        f"пауза: {html.escape(state.paused_reason) if state.paused_reason else 'нет'}",
        f"сегодня: найдено {m['found']} · подходят {m['suitable']} · отправлено {m['sent']} "
        f"(осталось {limits.remaining_day}) · приглашений {m['invitations']}",
        f"ждут разбора: {m['awaiting_claude']} вакансий в {pending} пакетах",
    ]
    last_sync = as_utc(state.last_sync_at)
    if last_sync:
        local = last_sync.astimezone(ZoneInfo(settings.timezone))  # TIMEZONE, not the computer's zone
        lines.append(f"синхронизация: {local.strftime('%d.%m %H:%M')}")
    return "\n".join(lines)


async def _web_command(session: AsyncSession, settings: Settings, command: str, message: dict[str, Any] | None) -> str:
    """/open and /logout (services/web_login.py)."""
    from app.services import web_login

    if not settings.panel_public_url:
        return "Вход в панель из браузера выключен: в .env панели не задан PANEL_PUBLIC_URL."
    if not _private_owner(message):
        return f"Команда {command} работает только в личном чате с ботом."
    if command == "/logout":
        ended = await web_login.end_all(session)
        await audit(session, "auth", f"Выход из панели во всех браузерах командой бота /logout (сессий: {ended})")
        return (
            f"🚪 Все входы в панель закрыты (сессий: {ended}), неиспользованные ссылки отменены. Войти снова — /open."
        )
    url, expires = await web_login.issue_link(session, settings, chat_id=str(_part(message, "chat").get("id")))
    until = expires.astimezone(ZoneInfo(settings.timezone)).strftime("%H:%M")
    await audit(session, "auth", "Выдана ссылка для входа в панель (/open)", until=until)
    return (
        f'🔑 <b>Вход в панель</b>: <a href="{html.escape(url, quote=True)}">открыть страницу входа</a>\n'
        f"Ссылка срабатывает один раз и действует до {until}. Не пересылайте её; "
        "если вход просили не вы, отправьте /logout."
    )


async def handle_command(
    runtime: Any, session: AsyncSession, command: str, message: dict[str, Any] | None = None
) -> str:
    settings: Settings = runtime.settings
    if command in ("/open", "/logout"):
        return await _web_command(session, settings, command, message)
    if command == "/status":
        return await _status_text(session, settings)
    if command == "/pause":
        await set_pause(session, "user")
        await audit(session, "telegram", "Пауза включена командой бота /pause")
        return "⏸ Фоновые задачи на паузе. /resume — продолжить."
    if command == "/resume":
        await set_pause(session, None)
        await audit(session, "telegram", "Пауза снята командой бота /resume")
        return "▶️ Пауза снята, задачи продолжают работу."
    if command == "/digest":
        from app.services.notify import render_event
        from app.services.stats import daily_summary_payload

        payload = await daily_summary_payload(session, settings)
        return render_event("daily", [payload], timezone=settings.timezone)
    if command == "/hot":
        from app.services.hot_digest import current_hot, hot_payload
        from app.services.notify import render_hot

        report = await current_hot(session, settings)
        return render_hot(hot_payload(report, report["items"]))
    if command == "/channels":
        from app.services.channels import new_posts_text

        return await new_posts_text(session)
    if command == "/batch":
        from app.exchange.packets import PacketError, build_batches

        try:
            created = await build_batches(session, settings)
        except PacketError as exc:
            return f"⚠️ {html.escape(str(exc))}"
        if not created:
            return "Кандидатов для разбора нет — пакет не создан."
        tasks = sum(b.tasks_count for b in created)
        return f"📦 Создано пакетов: {len(created)}, задач: {tasks}. Запустите <code>/hh-batch</code> в Claude Code."
    if command in ("/start", "/help"):
        return help_text(settings)
    return f"Неизвестная команда. {help_text(settings)}"


async def _handle_callback(
    runtime: Any, session: AsyncSession, client: TelegramClient, cq: dict[str, Any], chat_id: str
) -> bool:
    """An inline button of a cover-letter message: the panel action, a toast and the message shrinks to a status."""
    from app.services.tg_letters import handle_callback

    raw_msg = cq.get("message")
    msg: dict[str, Any] = dict(raw_msg) if isinstance(raw_msg, dict) else {}
    raw_chat = msg.get("chat")
    chat: dict[str, Any] = dict(raw_chat) if isinstance(raw_chat, dict) else {}
    if str(chat.get("id")) != str(chat_id) or not str(cq.get("data") or "").startswith("vac:"):
        return False
    try:
        toast, new_text, alert = await handle_callback(runtime, session, str(cq.get("data")))
    except Exception as exc:  # noqa: BLE001 - one bad button must not break polling
        log.warning("bot_callback_failed", data=cq.get("data"), error=str(exc))
        toast, new_text, alert = (f"Ошибка: {html.escape(str(exc))[:150]}", None, True)
    await session.commit()
    try:
        await client.call("answerCallbackQuery", callback_query_id=cq.get("id"), text=toast[:200], show_alert=alert)
    except TelegramError as exc:
        log.warning("bot_callback_answer_failed", error=str(exc))
    if new_text and msg.get("message_id"):
        try:
            await client.call(
                "editMessageText",
                chat_id=chat_id,
                message_id=msg["message_id"],
                text=new_text,
                parse_mode="HTML",
            )
        except TelegramError as exc:
            log.warning("bot_callback_edit_failed", error=str(exc))
    return True


async def poll_commands(runtime: Any) -> dict[str, Any]:
    """Scheduler tick: reads new updates, executes commands from the configured chat, replies."""
    settings: Settings = runtime.settings
    summary: dict[str, Any] = {"updates": 0, "commands": 0, "ignored": 0}
    if not settings.telegram_commands or not settings.telegram_bot_token:
        return summary
    async with runtime.session_factory() as session:
        chat_id = await effective_chat_id(session, settings)
        if not chat_id:
            return summary
        offset = await get_setting(session, OFFSET_SETTING)
        client = TelegramClient(runtime.http, settings.telegram_bot_token)
        try:
            params: dict[str, Any] = {"timeout": 0, "allowed_updates": ["message", "callback_query"]}
            if isinstance(offset, int):
                params["offset"] = offset
            updates = await client.call("getUpdates", **params)
        except TelegramError as exc:
            log.warning("bot_poll_failed", error=str(exc))
            return {**summary, "error": str(exc)}
        updates = [u for u in (updates or []) if isinstance(u, dict)]
        if not updates:
            return summary
        last_id = max(int(u.get("update_id", 0)) for u in updates)
        await set_setting(session, OFFSET_SETTING, last_id + 1)
        for upd in updates:
            summary["updates"] += 1
            cq = upd.get("callback_query") if isinstance(upd.get("callback_query"), dict) else None
            if cq is not None:
                if await _handle_callback(runtime, session, client, cq, chat_id):
                    summary["buttons"] = int(summary.get("buttons", 0)) + 1
                else:
                    summary["ignored"] += 1
                continue
            msg = upd.get("message") if isinstance(upd.get("message"), dict) else None
            chat = msg.get("chat") if msg and isinstance(msg.get("chat"), dict) else None
            if not msg or not chat or str(chat.get("id")) != str(chat_id):
                summary["ignored"] += 1
                continue
            command = _cmd(str(msg.get("text") or ""))
            if command is None:
                summary["ignored"] += 1
                continue
            summary["commands"] += 1
            try:
                reply = await handle_command(runtime, session, command, msg)
            except Exception as exc:  # noqa: BLE001 - one bad command must not break polling
                log.warning("bot_command_failed", command=command, error=str(exc))
                reply = f"⚠️ Не удалось выполнить {html.escape(command)}: {html.escape(str(exc))[:200]}"
            await session.commit()  # a sign-in link exists in the base before it reaches the chat
            try:
                await client.send_message(chat_id, reply, link_preview_options=NO_PREVIEW)
            except TelegramError as exc:
                log.warning("bot_reply_failed", error=str(exc))
        await session.commit()
    summary["at"] = utcnow().isoformat(timespec="seconds")
    return summary
