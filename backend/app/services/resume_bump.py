"""Reminder «пора поднять резюме» (Telegram + panel feed).

hh closed `POST /resumes/{id}/publish` for the application (the probe gets 403, like every other applicant
method), so the panel cannot bump the resume itself. Instead it pings the user at fixed local times
(`RESUME_BUMP_REMINDER_AT`, `RESUME_BUMP_REMINDER_DAYS`) with a link to the resume page — one tap on
«Поднять в поиске» there. hh counts its 4-hour cooldown from the actual click, not from the schedule, so the slots
should be a bit more than 4 hours apart (the default 08:45 / 13:00 / 17:15 leaves 15 minutes of slack).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import WEEKDAY_NAMES, Settings
from app.db.base import as_utc, utcnow
from app.services.notify import emit_event, enabled_events
from app.services.profile import primary_resume
from app.services.state import get_setting, get_system_state, read_capabilities

log = structlog.get_logger("resume_bump")

RESUME_PAGE = "https://hh.ru/resume/{id}"
RESUMES_LIST = "https://hh.ru/applicant/resumes"
DAY_NAMES = WEEKDAY_NAMES
DAY_SHORT_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
# «в понедельник», «во вторник» … — for «следующее напоминание — во вторник в 08:45»
DAY_WHEN_RU = ("в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье")
_HHMM = re.compile(r"^(\d{1,2}):(\d{2})$")


# ---- schedule -------------------------------------------------------------------


def parse_slots(value: str | None) -> list[tuple[int, int]]:
    """`08:45,13:00,17:15` → sorted unique (hour, minute); malformed parts are dropped."""
    out: set[tuple[int, int]] = set()
    for part in re.split(r"[\s,;]+", (value or "").strip()):
        m = _HHMM.match(part)
        if not m:
            continue
        hour, minute = int(m.group(1)), int(m.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            out.add((hour, minute))
    return sorted(out)


def parse_days(value: str | None) -> list[int]:
    """`mon-fri`, `mon,wed,fri`, `sat-mon` (wraps), `*`/empty → every day. Weekday numbers: mon=0 … sun=6."""
    text = (value or "").strip().lower()
    if not text or text in ("*", "all", "daily", "every day"):
        return list(range(7))
    out: set[int] = set()
    for part in re.split(r"[\s,;]+", text):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a[:3] in DAY_NAMES and b[:3] in DAY_NAMES:
                start, end = DAY_NAMES.index(a[:3]), DAY_NAMES.index(b[:3])
                d = start
                while True:
                    out.add(d)
                    if d == end:
                        break
                    d = (d + 1) % 7
        elif part[:3] in DAY_NAMES:
            out.add(DAY_NAMES.index(part[:3]))
    return sorted(out)


def cron_day_of_week(days: list[int]) -> str | None:
    """APScheduler `day_of_week` expression (None = every day)."""
    if not days or len(days) >= 7:
        return None
    return ",".join(DAY_NAMES[d] for d in days)


def schedule_label(slots: list[tuple[int, int]], days: list[int]) -> str | None:
    """`08:45, 13:00, 17:15 (пн–пт)` for the UI; None when the reminder is off."""
    if not slots:
        return None
    times = ", ".join(f"{h:02d}:{m:02d}" for h, m in slots)
    if not days or len(days) >= 7:
        return f"{times} (ежедневно)"
    # a contiguous run reads as «пн–пт», anything else as a list
    if days == list(range(days[0], days[0] + len(days))) and len(days) > 2:
        return f"{times} ({DAY_SHORT_RU[days[0]]}–{DAY_SHORT_RU[days[-1]]})"
    return f"{times} ({', '.join(DAY_SHORT_RU[d] for d in days)})"


def next_slot(now_local: datetime, slots: list[tuple[int, int]], days: list[int]) -> datetime | None:
    """The first scheduled moment strictly after `now_local` (aware, in the panel's timezone)."""
    if not slots or not days:
        return None
    tz = now_local.tzinfo
    for offset in range(0, 8):
        day = now_local.date() + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        for hour, minute in slots:
            candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
            if candidate > now_local:
                return candidate
    return None


def describe_when(moment: datetime, now_local: datetime) -> str:
    """«сегодня в 13:00», «завтра в 08:45», «в понедельник в 08:45»."""
    hhmm = moment.strftime("%H:%M")
    delta = (moment.date() - now_local.date()).days
    if delta == 0:
        return f"сегодня в {hhmm}"
    if delta == 1:
        return f"завтра в {hhmm}"
    return f"{DAY_WHEN_RU[moment.weekday()]} в {hhmm}"


def reminder_schedule(settings: Settings) -> tuple[list[tuple[int, int]], list[int]]:
    return parse_slots(settings.resume_bump_reminder_at), parse_days(settings.resume_bump_reminder_days)


def reminder_status(settings: Settings) -> dict[str, Any]:
    """For the Telegram block of the Settings page."""
    slots, days = reminder_schedule(settings)
    return {
        "at": [f"{h:02d}:{m:02d}" for h, m in slots],
        "days": settings.resume_bump_reminder_days,
        "label": schedule_label(slots, days),
        "event_enabled": "resume_bump" in enabled_events(settings),
    }


# ---- the reminder ---------------------------------------------------------------


async def resume_link(session: AsyncSession) -> tuple[str, str | None]:
    """(url, resume id): the primary hh resume, else the id the user entered by hand, else the resumes list."""
    resume = await primary_resume(session, "hh")
    resume_id = (resume.hh_resume_id if resume else None) or await get_setting(session, "manual_resume_id")
    resume_id = str(resume_id).strip() if resume_id else None
    return (RESUME_PAGE.format(id=resume_id) if resume_id else RESUMES_LIST), resume_id


async def reminder_payload(
    session: AsyncSession, settings: Settings, *, slot: str | None, now: datetime | None = None, force: bool = False
) -> tuple[dict[str, Any] | None, str]:
    """Payload of the `resume_bump` event, or (None, reason) when the reminder makes no sense right now."""
    now = now or utcnow()
    if not force:
        state = await get_system_state(session)
        if settings.resume_auto_publish and read_capabilities(state).get("resume_publish") == "available":
            return None, "панель поднимает резюме сама (RESUME_AUTO_PUBLISH)"
        resume = await primary_resume(session, "hh")
        nxt = as_utc(resume.next_publish_at) if resume else None
        if nxt is not None and nxt > now:
            allowed_at = nxt.astimezone(ZoneInfo(settings.timezone)).strftime("%d.%m %H:%M")
            return None, f"hh разрешит поднять после {allowed_at}"
    url, resume_id = await resume_link(session)
    slots, days = reminder_schedule(settings)
    now_local = now.astimezone(ZoneInfo(settings.timezone))
    nxt_local = next_slot(now_local, slots, days)
    return {
        "url": url,
        "resume_id": resume_id,
        "slot": slot,
        "next": describe_when(nxt_local, now_local) if nxt_local else None,
        "next_at": nxt_local.isoformat(timespec="minutes") if nxt_local else None,
    }, ""


async def bump_reminder_tick(runtime: Any, *, slot: str | None = None, force: bool = False) -> dict[str, Any]:
    """Scheduler entry point (one cron job per slot) and the «Напомнить сейчас» button (`force=True`)."""
    settings: Settings = runtime.settings
    async with runtime.session_factory() as session:
        payload, reason = await reminder_payload(session, settings, slot=slot, force=force)
        if payload is None:
            return {"skipped": reason}
        rows = await emit_event(session, settings, "resume_bump", payload)
        await session.commit()
    telegram = any(r.channel == "telegram" for r in rows)
    if not telegram and settings.telegram_bot_token:
        log.warning("resume_bump_not_in_telegram_events")
    return {"ok": True, "telegram": telegram, "url": payload["url"], "next": payload["next"]}
