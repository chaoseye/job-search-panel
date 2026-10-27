"""Cover letters in the bot, both ways.

The event `letter` sends every evaluated vacancy with its letter as one Telegram message with inline buttons
(«Да, отправлено», «Пропустить», «Отложить на 3 дня» and a link to the hh.ru response page). The panel
remembers the message per vacancy (`vacancy_tg_messages`). A button press is handled by the bot poller exactly
like the panel action (assist_open + assist_confirm, skip, snooze) and the message shrinks to a status line. When
the same vacancy is handled in the panel (assist «Да, отправлено», an API send, skip, snooze, hide) the message
is deleted from the chat on the next delivery tick. The feature has a switch in Settings (setting
`telegram_letters`: enabled, min_score).
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.db.models import Vacancy, VacancyTgMessage
from app.services.notify import enabled_events, esc

log = structlog.get_logger("tg_letters")

SETTING = "telegram_letters"
CALLBACK_PREFIX = "vac"
SNOOZE_DAYS = 3
ACTIONS = ("sent", "skip", "snooze")
STUBS = {
    "sent": "✅ Отклик отправлен",
    "skipped": "⏭ Пропущена",
    "snoozed": f"🕒 Отложена на {SNOOZE_DAYS} дн.",
    "hidden": "🙈 Скрыта",
    "queued": "📤 В очереди на отправку",
}


# ---- the switch ------------------------------------------------------------------------------


async def letters_config(session: AsyncSession, settings: Settings) -> dict[str, Any]:
    """{"enabled", "min_score"}: the Settings-page switch; defaults come from .env."""
    from app.services.state import get_setting

    stored = await get_setting(session, SETTING)
    enabled = "letter" in enabled_events(settings)
    min_score = int(settings.telegram_letters_min_score)
    if isinstance(stored, dict):
        if stored.get("enabled") is not None:
            enabled = bool(stored["enabled"])
        if stored.get("min_score") is not None:
            min_score = int(stored["min_score"])
    return {
        "enabled": enabled,
        "min_score": max(0, min(100, min_score)),
        "event_in_env": "letter" in enabled_events(settings),
    }


async def save_letters_config(session: AsyncSession, *, enabled: bool, min_score: int) -> dict[str, Any]:
    from app.services.audit import audit
    from app.services.state import set_setting

    value = {"enabled": bool(enabled), "min_score": max(0, min(100, int(min_score)))}
    await set_setting(session, SETTING, value)
    await audit(
        session,
        "telegram",
        f"Письма в бота {'включены' if enabled else 'выключены'}, порог оценки {value['min_score']}",
    )
    return value


def settings_for_letters(settings: Settings) -> Settings:
    """A settings copy where the event is enabled regardless of TELEGRAM_EVENTS (the switch is authoritative)."""
    if "letter" in enabled_events(settings):
        return settings
    return settings.model_copy(update={"telegram_events": (settings.telegram_events or "") + ",letter"})


# ---- the message -----------------------------------------------------------------------------


def keyboard(vacancy_id: int, apply_url: str | None) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = [
        [
            {"text": "✅ Да, отправлено", "callback_data": f"{CALLBACK_PREFIX}:{vacancy_id}:sent"},
            {"text": "⏭ Пропустить", "callback_data": f"{CALLBACK_PREFIX}:{vacancy_id}:skip"},
            {"text": f"🕒 Отложить {SNOOZE_DAYS} дн.", "callback_data": f"{CALLBACK_PREFIX}:{vacancy_id}:snooze"},
        ]
    ]
    if apply_url:
        rows.append([{"text": "Откликнуться на hh.ru", "url": apply_url}])
    return {"inline_keyboard": rows}


def parse_callback(data: str | None) -> tuple[int, str] | None:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX or not parts[1].isdigit() or parts[2] not in ACTIONS:
        return None
    return int(parts[1]), parts[2]


async def remember(
    session: AsyncSession, *, vacancy_id: int, chat_id: str, message_id: int, notification_id: int | None
) -> VacancyTgMessage:
    row = VacancyTgMessage(
        vacancy_id=vacancy_id, chat_id=str(chat_id), message_id=int(message_id), notification_id=notification_id
    )
    session.add(row)
    await session.flush()
    return row


async def active_messages(session: AsyncSession, vacancy_id: int) -> list[VacancyTgMessage]:
    return list(
        (
            await session.execute(
                select(VacancyTgMessage).where(
                    VacancyTgMessage.vacancy_id == vacancy_id, VacancyTgMessage.status == "active"
                )
            )
        )
        .scalars()
        .all()
    )


async def mark_resolved(session: AsyncSession, vacancy_id: int, resolution: str) -> int:
    """Called by the panel actions (pure DB): the bot messages of the vacancy are deleted on the next tick."""
    rows = await active_messages(session, vacancy_id)
    for row in rows:
        row.status = "resolve"
        row.resolution = resolution
    if rows:
        await session.flush()
    return len(rows)


def _stub(vacancy: Vacancy | None, resolution: str, *, source: str) -> str:
    head = STUBS.get(resolution, resolution)
    name = esc(vacancy.name) if vacancy is not None else "вакансия"
    employer = f" — {esc(vacancy.employer_name)}" if vacancy is not None and vacancy.employer_name else ""
    return f"{head}: {name}{employer} ({source})"


async def flush_resolutions(session: AsyncSession, settings: Settings, http: httpx.AsyncClient) -> int:
    """Deletes (or, when too old to delete, rewrites) the messages of vacancies handled in the panel."""
    from app.services.telegram import TelegramClient, TelegramError

    if not settings.telegram_bot_token:
        return 0
    rows = list(
        (await session.execute(select(VacancyTgMessage).where(VacancyTgMessage.status == "resolve"))).scalars().all()
    )
    if not rows:
        return 0
    client = TelegramClient(http, settings.telegram_bot_token)
    done = 0
    for row in rows:
        try:
            await client.call("deleteMessage", chat_id=row.chat_id, message_id=row.message_id)
        except TelegramError as exc:
            if exc.transient:
                continue  # network / 429: try again next minute
            vacancy = await session.get(Vacancy, row.vacancy_id)
            try:
                await client.call(
                    "editMessageText",
                    chat_id=row.chat_id,
                    message_id=row.message_id,
                    text=_stub(vacancy, row.resolution or "sent", source="из панели"),
                    parse_mode="HTML",
                )
            except TelegramError as exc2:
                row.error = str(exc2)
        row.status = "resolved"
        row.resolved_at = utcnow()
        done += 1
    await session.flush()
    return done


# ---- buttons ---------------------------------------------------------------------------------


async def handle_callback(runtime: Any, session: AsyncSession, data: str | None) -> tuple[str, str | None, bool]:
    """A button press: (toast, new message text or None to leave the message as is, show as alert)."""
    from app.services import vacancies as vac_svc
    from app.services.applier import ApplyError, assist_confirm, assist_open

    parsed = parse_callback(data)
    if parsed is None:
        return ("Неизвестная кнопка", None, True)
    vacancy_id, action = parsed
    vacancy = await session.get(Vacancy, vacancy_id)
    if vacancy is None:
        return ("Вакансия уже удалена из панели", "Вакансия удалена из панели", True)
    settings: Settings = runtime.settings
    if action == "sent":
        if vacancy.pipeline_status == "sent":
            return ("Отклик уже отмечен отправленным", _stub(vacancy, "sent", source="из бота"), False)
        try:
            app_row = await assist_open(session, settings, vacancy)
            await assist_confirm(session, app_row, sent=True, settings=settings)
        except ApplyError as exc:
            return (f"Не получилось: {exc}"[:200], None, True)
        resolution, toast = "sent", "Отклик отмечен отправленным"
    elif action == "skip":
        await vac_svc.skip(session, vacancy, "из Telegram")
        resolution, toast = "skipped", "Вакансия пропущена"
    else:
        await vac_svc.snooze(session, vacancy, SNOOZE_DAYS)
        resolution, toast = "snoozed", f"Отложена на {SNOOZE_DAYS} дн."
    # the panel-side hooks queued the message for deletion; the bot edits it instead
    for row in await active_messages(session, vacancy_id):
        row.status = "resolved"
        row.resolution = resolution
        row.resolved_at = utcnow()
    for row in (
        await session.execute(
            select(VacancyTgMessage).where(
                VacancyTgMessage.vacancy_id == vacancy_id, VacancyTgMessage.status == "resolve"
            )
        )
    ).scalars():
        row.status = "resolved"
        row.resolved_at = utcnow()
    await session.flush()
    return (toast, _stub(vacancy, resolution, source="из бота"), False)
