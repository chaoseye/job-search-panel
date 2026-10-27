"""Event notifications: a `ui` feed row for every event and a queued `telegram` row when enabled.

Texts are rendered here (HTML for Telegram, `parse_mode=HTML`), merging several payloads of one event type into
one message. Messages never contain tokens, full letters or personal data beyond vacancy/employer names.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import Notification
from app.services.textmatch import strip_emoji
from app.services.tg_filter import FILTERED_EVENTS

EVENT_TYPES = (
    "digest",
    "batch_ready",
    "batch_imported",
    "applied",
    "invitation",
    "discard",
    "message",
    "captcha",
    "auth",
    "limit",
    "job_error",
    "daily",
    "capability",
    "resume_bump",
    "hot",
    "channel_post",
    "letter",
    "site_vacancy",
    "search_new",
    "studio_reply",
    "ws_tender",
    "login",
)
EVENT_LABELS = {
    "digest": "Дайджест новых подходящих",
    "batch_ready": "Ждут разбора",
    "batch_imported": "Разбор импортирован",
    "applied": "Отклик отправлен",
    "invitation": "Приглашение",
    "discard": "Отказ",
    "message": "Новое сообщение",
    "captcha": "Капча",
    "auth": "Токен истёк/отозван",
    "limit": "Лимит hh исчерпан",
    "job_error": "Сбой задачи",
    "daily": "Вечерняя сводка",
    "capability": "Возможности hh.ru изменились",
    "resume_bump": "Пора поднять резюме",
    "hot": "Горячие вакансии",
    "channel_post": "Вакансия из Telegram-канала",
    "letter": "Вакансия с письмом",
    "site_vacancy": "Вакансия на сайте компании",
    "search_new": "Вакансия по поиску-наблюдению",
    "studio_reply": "Ответ студии",
    "ws_tender": "Тендер workspace.ru",
    "login": "Вход в панель по ссылке",
}
# sent right away; the rest wait MERGE_WINDOW_SEC so same-type events are merged into one message
IMMEDIATE_EVENTS = frozenset(
    {
        "captcha",
        "auth",
        "limit",
        "job_error",
        "daily",
        "batch_ready",
        "resume_bump",
        "hot",
        "letter",
        "studio_reply",
        "login",
    }
)
# sent whatever TELEGRAM_EVENTS says: a sign-in through the public address is how the owner notices a stranger's one
ALWAYS_ON_EVENTS = frozenset({"login"})
MERGE_WINDOW_SEC = 300
BATCH_READY_COOLDOWN_SEC = 1800
MAX_ITEMS_PER_MESSAGE = 10
# these types are never squeezed into one message with «… и ещё N»: channel posts go out ten per message,
# a vacancy with its cover letter is always a message of its own
CHUNK_SIZES = {
    "channel_post": MAX_ITEMS_PER_MESSAGE,
    "letter": 1,
    "site_vacancy": MAX_ITEMS_PER_MESSAGE,
    "search_new": MAX_ITEMS_PER_MESSAGE,
    "ws_tender": MAX_ITEMS_PER_MESSAGE,
}
CHUNKED_EVENTS = frozenset(CHUNK_SIZES)
LETTER_TEXT_MAX = 3000  # Telegram message limit is 4096; letters are ≤ 1200 by the guidelines
SNIPPET_CHARS = 200
NEGOTIATION_PAGE = "https://hh.ru/applicant/negotiations/item?id={nid}"


def enabled_events(settings: Settings) -> set[str]:
    raw = settings.telegram_events or ""
    return {part.strip() for part in raw.split(",") if part.strip()} | ALWAYS_ON_EVENTS


def digest_bucket(event_type: str, moment: datetime) -> str:
    stamp = int(moment.timestamp() // MERGE_WINDOW_SEC)
    return f"{event_type}:{stamp}"


async def emit_event(
    session: AsyncSession,
    settings: Settings,
    event_type: str,
    payload: dict[str, Any],
    *,
    telegram_chat_id: str | None = None,
    now: datetime | None = None,
) -> list[Notification]:
    """Records the event in the UI feed and queues a Telegram row when the bot is configured and the event enabled.

    `telegram_chat_id` is the effective chat id (settings table override or .env); pass None when unknown —
    the sender resolves it again at delivery time, so rows are still queued when a token exists.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {event_type}")
    now = now or utcnow()
    body = json.dumps(payload, ensure_ascii=False, default=str)
    rows: list[Notification] = [
        Notification(channel="ui", event_type=event_type, payload_json=body, status="sent", sent_at=now, created_at=now)
    ]
    if settings.telegram_bot_token and event_type in enabled_events(settings):
        status = "queued"
        error: str | None = None
        telegram_body = body
        if event_type in FILTERED_EVENTS:
            # vacancies with the user's stop words stay in the feed, not in the bot (services/tg_filter.py)
            from app.services.tg_filter import stop_words, telegram_payload

            words = await stop_words(session)
            if words:
                filtered, reason = telegram_payload(event_type, payload, words)
                if filtered is None:
                    status, error = "suppressed", reason
                elif filtered is not payload:
                    telegram_body = json.dumps(filtered, ensure_ascii=False, default=str)
        if event_type == "batch_ready":
            since = now - timedelta(seconds=BATCH_READY_COOLDOWN_SEC)
            recent = (
                await session.execute(
                    select(func.count(Notification.id)).where(
                        Notification.channel == "telegram",
                        Notification.event_type == "batch_ready",
                        Notification.status.in_(("queued", "sent")),
                        Notification.created_at >= since,
                    )
                )
            ).scalar()
            if int(recent or 0) > 0:
                status, error = "suppressed", "не чаще раза в 30 минут"
        rows.append(
            Notification(
                channel="telegram",
                event_type=event_type,
                payload_json=telegram_body,
                status=status,
                error=error,
                digest_key=digest_bucket(event_type, now),
                created_at=now,
            )
        )
    for row in rows:
        session.add(row)
    await session.flush()
    return rows


# ---- rendering ----------------------------------------------------------------


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def link(url: str | None, text: str) -> str:
    return f'<a href="{html.escape(str(url), quote=True)}">{esc(text)}</a>' if url else esc(text)


def snippet(text: str | None, limit: int = SNIPPET_CHARS) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _place_text(p: dict[str, Any]) -> str:
    """«Москва, м. Арбатская» / «м. Сокол» / «Москва» from area, metro (a list, the nearest first) and address:
    the nearest station when there is one, otherwise the street — enough to see where the workplace is."""
    metro = p.get("metro")
    station = str(metro[0]) if isinstance(metro, list) and metro else ""
    where = f"м. {station}" if station else str(p.get("address") or "")
    return ", ".join(x for x in (str(p.get("area") or ""), where) if x)


def _salary_text(p: dict[str, Any]) -> str:
    """«от 90 000 ₽», «100 000 – 130 000 ₽» from salary_from / salary_to / currency of a payload."""
    lo, hi = p.get("salary_from"), p.get("salary_to")
    if not lo and not hi:
        return ""
    code = str(p.get("currency") or "RUR").upper()
    cur = {"RUR": "₽", "RUB": "₽", "USD": "$", "EUR": "€"}.get(code, code)

    def fmt(v: Any) -> str:
        return f"{int(v):,}".replace(",", " ")

    if lo and hi:
        return f"{fmt(lo)} – {fmt(hi)} {cur}".strip()
    return f"от {fmt(lo)} {cur}".strip() if lo else f"до {fmt(hi)} {cur}".strip()


def _num(value: Any, default: float = 0) -> int:
    """A count, a score or a threshold from an event payload as int: empty → `default` (the old `int(x or default)`),
    anything unparsable → `default` too — a notification must always render, or it blocks its delivery group."""
    if not value:
        return int(default)
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _money(value: Any) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return ""
    return f"{n:,}".replace(",", " ") + " ₽"


def _vacancy_line(p: dict[str, Any], *, with_score: bool = False) -> str:
    parts = [link(p.get("url"), p.get("name") or "вакансия")]
    if p.get("employer"):
        parts.append("— " + esc(p["employer"]))
    extras = []
    if with_score and p.get("score") is not None:
        extras.append(f"оценка {esc(p['score'])}")
    if p.get("salary"):
        extras.append(_money(p["salary"]))
    if extras:
        parts.append("· " + " · ".join(extras))
    return " ".join(parts)


def _date_ru(value: Any) -> str:
    """«2026-09-30» → «30.09.2026» (anything else as it is)."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(value or ""))
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else str(value or "")


def _list(lines: list[str]) -> str:
    shown = lines[:MAX_ITEMS_PER_MESSAGE]
    more = len(lines) - len(shown)
    text = "\n".join(f"• {line}" for line in shown)
    if more > 0:
        text += f"\n… и ещё {more}"
    return text


def _fmt_local(value: Any, tz: ZoneInfo) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return str(value)
    dt = as_utc(value) if isinstance(value, datetime) else None
    return dt.astimezone(tz).strftime("%d.%m %H:%M") if dt else str(value or "")


def render_event(
    event_type: str,
    payloads: list[dict[str, Any]],
    *,
    timezone: str = "Europe/Moscow",
    part: tuple[int, int, int] | None = None,
) -> str:
    """One Telegram message (HTML) for one or several payloads of the same event type.

    `part=(index, count, total)` marks one message of a chunked delivery (CHUNKED_EVENTS)."""
    tz = ZoneInfo(timezone or "UTC")
    n = len(payloads)
    p0 = payloads[0] if payloads else {}

    if event_type == "digest":
        suitable = sum(_num(p.get("suitable"), 0) for p in payloads)
        maybe = sum(_num(p.get("maybe"), 0) for p in payloads)
        items: list[dict[str, Any]] = []
        for p in payloads:
            items.extend(p.get("top") or [])
        items.sort(key=lambda i: _num(i.get("score"), 0), reverse=True)
        seen: set[str] = set()
        top: list[dict[str, Any]] = []
        for item in items:
            key = str(item.get("hh_id") or item.get("name"))
            if key in seen:
                continue
            seen.add(key)
            top.append(item)
        head = f"🆕 <b>Новые подходящие вакансии: {suitable + maybe}</b> (подходят {suitable}, возможно {maybe})"
        if not top:
            return head
        return head + "\n" + "\n".join(f"{i + 1}. {_vacancy_line(t, with_score=True)}" for i, t in enumerate(top[:5]))

    if event_type == "batch_ready":
        tasks = sum(_num(p.get("tasks"), 0) for p in payloads)
        ids = [str(p.get("batch_id")) for p in payloads if p.get("batch_id")]
        suffix = f" ({', '.join(ids[:3])}{'…' if len(ids) > 3 else ''})" if ids else ""
        packets = f"{n} пакет{_plural(n, 'е', 'ах', 'ах')}"
        return (
            f"📦 <b>Ждут разбора:</b> {tasks} задач в {packets}{suffix} — "
            f"запустите <code>/hh-batch</code> в Claude Code"
        )

    if event_type == "batch_imported":
        ev = sum(_num(p.get("evaluations"), 0) for p in payloads)
        letters = sum(_num(p.get("letters"), 0) for p in payloads)
        suitable = sum(_num(p.get("suitable"), 0) for p in payloads)
        errors = sum(_num(p.get("errors"), 0) for p in payloads)
        text = f"✅ <b>Разбор импортирован:</b> {ev} оценок, {letters} писем готовы; подходят: {suitable}"
        if errors:
            text += f"; ошибок: {errors}"
        return text

    if event_type == "applied":
        if n == 1:
            mode = esc(p0.get("mode_label") or p0.get("mode") or "")
            return f"📨 <b>Отклик отправлен:</b> {_vacancy_line(p0)}" + (f" ({mode})" if mode else "")
        lines = [
            _vacancy_line(p) + (f" ({esc(p.get('mode_label') or p.get('mode'))})" if p.get("mode") else "")
            for p in payloads
        ]
        return f"📨 <b>Отправлено откликов: {n}</b>\n" + _list(lines)

    if event_type == "invitation":
        lines = []
        for p in payloads:
            line = _vacancy_line(p)
            if p.get("text"):
                line += f"\n  «{esc(snippet(p['text']))}»"
            if p.get("negotiation_id"):
                line += "\n  " + link(NEGOTIATION_PAGE.format(nid=p["negotiation_id"]), "Открыть чат")
            lines.append(line)
        if n == 1:
            return "🎉 <b>Приглашение:</b> " + lines[0]
        return f"🎉 <b>Приглашения: {n}</b>\n" + _list(lines)

    if event_type == "discard":
        lines = [_vacancy_line(p) for p in payloads]
        if n == 1:
            return "❌ <b>Отказ:</b> " + lines[0]
        return f"❌ <b>Отказы: {n}</b>\n" + _list(lines)

    if event_type == "message":
        lines = []
        for p in payloads:
            author = esc(p.get("author") or "работодатель")
            line = f"{_vacancy_line(p)} — {author}"
            if p.get("text"):
                line += f"\n  «{esc(snippet(p['text']))}»"
            if p.get("negotiation_id"):
                line += "\n  " + link(NEGOTIATION_PAGE.format(nid=p["negotiation_id"]), "Открыть чат")
            lines.append(line)
        if n == 1:
            return "💬 <b>Новое сообщение:</b> " + lines[0]
        return f"💬 <b>Новые сообщения: {n}</b>\n" + _list(lines)

    if event_type == "captcha":
        url = p0.get("url")
        text = "⚠️ <b>hh.ru требует капчу.</b> Фоновые задачи на паузе."
        return text + (f"\n{link(url, 'Пройти капчу')}" if url else "")

    if event_type == "auth":
        reason = esc(p0.get("reason") or "")
        return "🔑 <b>Токен hh.ru истёк или отозван.</b> Войдите заново: Настройки → «Войти через hh.ru»." + (
            f"\nПричина: {reason}" if reason else ""
        )

    if event_type == "limit":
        until = _fmt_local(p0.get("until"), tz) if p0.get("until") else ""
        return "⛔ <b>Лимит откликов hh.ru исчерпан.</b>" + (
            f" Отправка приостановлена до {esc(until)}." if until else ""
        )

    if event_type == "capability":
        lines = []
        for p in payloads:
            for ch in p.get("changes") or []:
                lines.append(f"{esc(ch.get('key'))}: {esc(ch.get('old'))} → <b>{esc(ch.get('new'))}</b>")
            if p.get("apply_mode") != p.get("prev_mode"):
                lines.append(f"режим отклика: {esc(p.get('prev_mode'))} → <b>{esc(p.get('apply_mode'))}</b>")
        return "🔓 <b>Возможности hh.ru изменились</b> (еженедельный зонд)\n" + "\n".join(lines)

    if event_type == "resume_bump":
        p = payloads[-1]  # sent right away, so normally a single payload; the newest wins otherwise
        text = "⬆️ <b>Пора поднять резюме в поиске</b>"
        if p.get("slot"):
            text += f" ({esc(p['slot'])})"
        text += "\n" + link(p.get("url") or "https://hh.ru/applicant/resumes", "Открыть резюме на hh.ru")
        text += " → кнопка «Поднять в поиске»."
        if p.get("next"):
            text += f"\nСледующее напоминание — {esc(p['next'])}."
        return text

    if event_type == "hot":
        return render_hot(payloads[-1])

    if event_type == "channel_post":
        lines = []
        for p in payloads:
            line = link(p.get("url"), strip_emoji(p.get("title")) or "пост") + f" — @{esc(p.get('username'))}"
            if p.get("hh_url"):
                line += " · " + link(p["hh_url"], "hh.ru")
            lines.append(line)
        if n == 1 and not part:
            text = f"📣 <b>Вакансия в Telegram-канале</b> {esc(strip_emoji(p0.get('channel')))}\n{lines[0]}"
            if p0.get("snippet"):
                text += f"\n«{esc(snippet(p0['snippet']))}»"
            return text
        if part and part[1] > 1:
            head = f"📣 <b>Новые вакансии в Telegram-каналах: {part[2]}</b> (часть {part[0]} из {part[1]})"
        else:
            head = f"📣 <b>Новые вакансии в Telegram-каналах: {n}</b>"
        return head + "\n" + _list(lines)

    if event_type == "letter":
        return render_letter(payloads[-1])

    if event_type == "site_vacancy":
        lines = []
        for p in payloads:
            where = " · ".join(x for x in ("удалённо" if p.get("remote") else "", str(p.get("location") or "")) if x)
            line = link(p.get("url"), p.get("title") or "вакансия") + f" — {esc(p.get('company') or p.get('site'))}"
            if where:
                line += f" · {esc(where)}"
            lines.append(line)
        if n == 1 and not part:
            return f"🏢 <b>Вакансия на сайте компании</b> {esc(p0.get('company') or p0.get('site'))}\n{lines[0]}"
        if part and part[1] > 1:
            head = f"🏢 <b>Новые вакансии на сайтах компаний: {part[2]}</b> (часть {part[0]} из {part[1]})"
        else:
            head = f"🏢 <b>Новые вакансии на сайтах компаний: {n}</b>"
        return head + "\n" + _list(lines)

    if event_type == "search_new":
        lines = []
        for p in payloads:
            line = link(p.get("url"), p.get("title") or "вакансия")
            if p.get("employer"):
                line += f" — {esc(p['employer'])}"
            extra = " · ".join(x for x in (_salary_text(p), _place_text(p)) if x)
            if extra:
                line += f" · {esc(extra)}"
            lines.append(line)
        if n == 1 and not part:
            return f"👀 <b>Новая вакансия по поиску «{esc(p0.get('search'))}»</b>\n{lines[0]}"
        if part and part[1] > 1:
            head = f"👀 <b>Новые вакансии по поискам-наблюдениям: {part[2]}</b> (часть {part[0]} из {part[1]})"
        else:
            head = f"👀 <b>Новые вакансии по поискам-наблюдениям: {n}</b>"
        return head + "\n" + _list(lines)

    if event_type == "studio_reply":
        lines = [
            f"<b>{esc(p.get('name'))}</b>" + (f" — «{esc(snippet(p.get('subject'), 150))}»" if p.get("subject") else "")
            for p in payloads
        ]
        return "✉️ <b>Студия ответила на письмо</b>\n" + "\n".join(lines) + "\nОтвет — в вашей почте."

    if event_type == "ws_tender":
        lines = []
        for p in payloads:
            extra = " · ".join(
                x
                for x in (
                    str(p.get("budget") or ""),
                    f"заявки до {_date_ru(p.get('deadline'))}" if p.get("deadline") else "",
                    f"заказчик из {p['city']}" if p.get("city") else "",
                    f"совпадение {p['score']}" if p.get("score") is not None else "",
                )
                if x
            )
            lines.append(link(p.get("url"), p.get("title") or "тендер") + (f"\n   {esc(extra)}" if extra else ""))
        if n == 1 and not part:
            return f"📋 <b>Тендер на workspace.ru</b>\n{lines[0]}\nЧерновик отклика — на странице «Тендеры» панели."
        if part and part[1] > 1:
            head = f"📋 <b>Новые тендеры на workspace.ru: {part[2]}</b> (часть {part[0]} из {part[1]})"
        else:
            head = f"📋 <b>Новые тендеры на workspace.ru: {n}</b>"
        return head + "\n" + _list(lines)

    if event_type == "login":
        lines = [
            esc(
                " · ".join(
                    x
                    for x in (_fmt_local(p.get("at"), tz), str(p.get("browser") or ""), f"IP {p.get('ip') or '—'}")
                    if x
                )
            )
            for p in payloads
        ]
        head = "🔐 <b>Вход в панель по ссылке</b>" if n == 1 else f"🔐 <b>Входы в панель по ссылке: {n}</b>"
        return head + "\n" + "\n".join(lines) + "\nНе вы? Отправьте /logout: все входы закроются, ссылки отменятся."

    if event_type == "job_error":
        lines = [
            f"<b>{esc(p.get('job'))}</b>: {_num(p.get('failures'), 3)} ошибки подряд. "
            f"Последняя: {esc(snippet(p.get('error'), 300))}"
            if _num(p.get("failures"), 3) > 1
            # a failure that cannot wait for the third one (a damaged backup copy)
            else f"<b>{esc(p.get('job'))}</b>: {esc(snippet(p.get('error'), 300))}"
            for p in payloads
        ]
        return "🛑 <b>Сбой задачи</b>\n" + "\n".join(lines)

    if event_type == "daily":
        s = p0
        date = esc(s.get("date") or "")
        text = (
            f"📊 <b>Сводка за {date}</b>\n"
            f"найдено {_num(s.get('found'), 0)} · подходящих {_num(s.get('suitable'), 0)} · "
            f"отправлено {_num(s.get('sent'), 0)} · просмотрено {_num(s.get('viewed'), 0)} · "
            f"приглашений {_num(s.get('invitations'), 0)} · ждут разбора {_num(s.get('awaiting'), 0)} · "
            f"без ответа >7 дней {_num(s.get('no_answer'), 0)}"
        )
        if s.get("late"):
            text += f"\n⏰ С опозданием: в {esc(s['late'])} панель не работала."
        journal = s.get("journal") or []
        if journal:
            days = _num(s.get("journal_days"), 7)
            total = _num(s.get("journal_total"), len(journal))
            rows = [
                f"• {esc(j.get('name'))}"
                + (f" — {esc(j.get('employer'))}" if j.get("employer") else "")
                + (f" ({_num(j['days'])} дн.)" if j.get("days") is not None else "")
                for j in journal
            ]
            more = f"\n…и ещё {total - len(journal)}" if total > len(journal) else ""
            text += (
                f"\n\n📝 <b>Без записи в журнале ≥{days} дней: {total}</b>\n"
                + "\n".join(rows)
                + more
                + "\nПосмотрите ответы на hh.ru и отметьте статусы: Отклики → «ответ работодателя»."
            )
        return text

    return esc(EVENT_LABELS.get(event_type, event_type))


def render_hot(p: dict[str, Any]) -> str:
    """«Горячие»: fresh vacancies with few responses; also used by the bot command /hot."""
    items = p.get("items") or []
    total = _num(p.get("total"), len(items))
    if not items:
        return (
            f"🔥 <b>Горячих вакансий сейчас нет</b> (свежие ≤ {_num(p.get('max_age_days'), 3)} дн., "
            f"откликов ≤ {_num(p.get('max_responses'), 50)}, оценка ≥ {_num(p.get('min_score'), 50)})."
        )
    lines = []
    for i, it in enumerate(items[:MAX_ITEMS_PER_MESSAGE], 1):
        parts = [link(it.get("url"), it.get("name") or "вакансия")]
        if it.get("employer"):
            parts.append("— " + esc(it["employer"]))
        extras = []
        if it.get("score") is not None:
            extras.append(f"оценка {esc(it['score'])}")
        elif it.get("prescore") is not None:
            extras.append(f"prescore {esc(it['prescore'])}")
        extras.append(f"{_num(it.get('responses'), 0)} откл.")
        if it.get("age_hours") is not None:
            hours = _num(it["age_hours"])
            extras.append(f"{int(hours)} ч" if hours < 48 else f"{int(hours // 24)} дн.")
        if it.get("salary"):
            extras.append(_money(it["salary"]))
        lines.append(f"{i}. {' '.join(parts)} · {' · '.join(extras)}")
    more = f"\n… и ещё {total - len(lines)}" if total > len(lines) else ""
    late = f"\n⏰ С опозданием: в {esc(p['late'])} панель не работала." if p.get("late") else ""
    return (
        f"🔥 <b>Горячие вакансии: {total}</b> — свежие, откликов ≤ {_num(p.get('max_responses'), 50)}\n"
        + "\n".join(lines)
        + more
        + late
    )


def render_letter(p: dict[str, Any]) -> str:
    """A vacancy that passed the evaluation together with its cover letter — one message per vacancy."""
    score = p.get("score")
    status = str(p.get("status") or "")
    verdict = {"suitable": "Подходит", "maybe": "Возможно"}.get(status, "Оценено")
    if p.get("revision"):
        head = f"✉️ <b>Письмо переработано</b> ({verdict.lower()}, {esc(score)})"
    else:
        head = f"✉️ <b>{verdict}</b> — оценка {esc(score)}"
    vacancy = link(p.get("url"), p.get("name") or "вакансия") + (
        f" — {esc(p['employer'])}" if p.get("employer") else ""
    )
    lines = [f"{head}: {vacancy}"]
    facts = [x for x in (_money(p["salary"]) if p.get("salary") else "", p.get("work_format"), _place_text(p)) if x]
    if facts:
        lines.append(" · ".join(esc(f) for f in facts))
    if p.get("summary"):
        lines.append(f"<i>{esc(snippet(p['summary'], 300))}</i>")
    text = str(p.get("letter") or "").strip()
    if len(text) > LETTER_TEXT_MAX:
        text = text[: LETTER_TEXT_MAX - 1].rstrip() + "…"
    version = f" (версия {p['version']})" if p.get("version") else ""
    lines.append("")
    lines.append(f"<b>Сопроводительное письмо</b>{version} — нажмите на текст, чтобы скопировать:")
    lines.append(f"<pre>{esc(text)}</pre>")
    if p.get("needs_clarification"):
        lines.append("")
        lines.append("⚠️ В письме есть пометки [[УТОЧНИТЬ]] — допишите их перед отправкой.")
    return "\n".join(lines)


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def ui_summary(event_type: str, payload: dict[str, Any]) -> str:
    """Short plain-text line for the panel feed."""
    label = EVENT_LABELS.get(event_type, event_type)
    name = payload.get("name")
    employer = payload.get("employer")
    if event_type in ("applied", "invitation", "discard", "message") and name:
        return f"{label}: {name}" + (f" — {employer}" if employer else "")
    if event_type == "digest":
        return f"{label}: {_num(payload.get('suitable'), 0) + _num(payload.get('maybe'), 0)}"
    if event_type == "batch_ready":
        return f"{label}: {payload.get('tasks')} задач, пакет {payload.get('batch_id')}"
    if event_type == "batch_imported":
        return f"{label}: оценок {payload.get('evaluations')}, писем {payload.get('letters')}"
    if event_type == "job_error":
        return f"{label}: {payload.get('job')} — {snippet(payload.get('error'), 120)}"
    if event_type == "capability":
        changes = payload.get("changes") or []
        listed = ", ".join(f"{c.get('key')} → {c.get('new')}" for c in changes)
        return f"{label}: " + (listed or f"режим {payload.get('apply_mode')}")
    if event_type == "limit":
        return f"{label}: до {payload.get('until')}"
    if event_type == "resume_bump":
        text = label + (f" ({payload.get('slot')})" if payload.get("slot") else "")
        return text + (f" · следующее {payload['next']}" if payload.get("next") else "")
    if event_type == "hot":
        return f"{label}: {_num(payload.get('total'), 0)}" + (" (с опозданием)" if payload.get("late") else "")
    if event_type == "channel_post":
        return f"{label}: {payload.get('title')} — @{payload.get('username')}"
    if event_type == "letter":
        text = f"{label}: {name}" + (f" — {employer}" if employer else "")
        return text + (f" ({payload.get('score')})" if payload.get("score") is not None else "")
    if event_type == "site_vacancy":
        return f"{label}: {payload.get('title')} — {payload.get('company') or payload.get('site')}"
    if event_type == "search_new":
        return f"{label}: {payload.get('title')}" + (f" — {payload['employer']}" if payload.get("employer") else "")
    if event_type == "daily":
        return f"{label} за {payload.get('date')}" + (" (с опозданием)" if payload.get("late") else "")
    if event_type == "studio_reply":
        return f"{label}: {payload.get('name')}" + (f" — {payload['subject']}" if payload.get("subject") else "")
    if event_type == "ws_tender":
        return f"{label}: {payload.get('title')}" + (f" — {payload['budget']}" if payload.get("budget") else "")
    if event_type == "login":
        return f"{label}: {payload.get('browser') or 'браузер'} · IP {payload.get('ip') or '—'}"
    return label


# ---- listing ----------------------------------------------------------------------


def notification_row(row: Notification) -> dict[str, Any]:
    try:
        payload = json.loads(row.payload_json or "{}")
    except ValueError:
        payload = {}
    created = as_utc(row.created_at)
    sent = as_utc(row.sent_at)
    return {
        "id": row.id,
        "channel": row.channel,
        "event_type": row.event_type,
        "label": EVENT_LABELS.get(row.event_type, row.event_type),
        "summary": ui_summary(row.event_type, payload),
        "payload": payload,
        "status": row.status,
        "attempts": row.attempts,
        "error": row.error,
        "text": row.text,
        "created_at": created.isoformat(timespec="seconds") if created else None,
        "sent_at": sent.isoformat(timespec="seconds") if sent else None,
    }


async def list_notifications(
    session: AsyncSession, *, channel: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    stmt = select(Notification).order_by(desc(Notification.id)).limit(min(limit, 500))
    if channel:
        stmt = stmt.where(Notification.channel == channel)
    rows = (await session.execute(stmt)).scalars().all()
    return [notification_row(r) for r in rows]


# the dashboard warns about Telegram messages that were given up during this many days
DELIVERY_WINDOW_DAYS = 7


async def delivery_summary(session: AsyncSession, *, now: datetime | None = None) -> dict[str, Any]:
    """Telegram messages given up (`failed`: a permanent error or 24 h of transient ones) during the last
    DELIVERY_WINDOW_DAYS, the newest of them, and the queue — the dashboard shows it when something did not reach
    the bot."""
    since = (now or utcnow()) - timedelta(days=DELIVERY_WINDOW_DAYS)
    failed_q = select(Notification).where(
        Notification.channel == "telegram", Notification.status == "failed", Notification.created_at >= since
    )
    failed = (await session.execute(select(func.count()).select_from(failed_q.subquery()))).scalar() or 0
    last = (await session.execute(failed_q.order_by(desc(Notification.id)).limit(1))).scalar_one_or_none()
    queued = (
        await session.execute(
            select(func.count(Notification.id)).where(
                Notification.channel == "telegram", Notification.status == "queued"
            )
        )
    ).scalar() or 0
    return {
        "days": DELIVERY_WINDOW_DAYS,
        "failed": int(failed),
        "queued": int(queued),
        "last_failed": None
        if last is None
        else {
            "event_type": last.event_type,
            "label": EVENT_LABELS.get(last.event_type, last.event_type),
            "error": last.error,
            "created_at": notification_row(last)["created_at"],
        },
    }


async def queue_counts(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(
            select(Notification.status, func.count(Notification.id))
            .where(Notification.channel == "telegram")
            .group_by(Notification.status)
        )
    ).all()
    return {str(s): int(c) for s, c in rows}
