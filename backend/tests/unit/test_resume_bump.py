"""Reminder «пора поднять резюме»: schedule parsing, next-slot arithmetic, message text."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Settings
from app.services.notify import render_event, ui_summary
from app.services.resume_bump import (
    cron_day_of_week,
    describe_when,
    next_slot,
    parse_days,
    parse_slots,
    reminder_status,
    schedule_label,
)

MSK = ZoneInfo("Europe/Moscow")


def test_parse_slots_and_days() -> None:
    assert parse_slots("08:45,13:00,17:15") == [(8, 45), (13, 0), (17, 15)]
    assert parse_slots(" 17:15; 8:45 , 13:00 , 13:00, 25:00, abc, 9:99") == [(8, 45), (13, 0), (17, 15)]
    assert parse_slots("") == [] and parse_slots(None) == []
    assert parse_days("mon-fri") == [0, 1, 2, 3, 4]
    assert parse_days("Mon, Wed, friday") == [0, 2, 4]
    assert parse_days("sat-mon") == [0, 5, 6]  # a range wraps around the week
    assert parse_days("*") == list(range(7)) and parse_days("") == list(range(7))
    assert parse_days("xyz") == []
    assert cron_day_of_week([0, 1, 2, 3, 4]) == "mon,tue,wed,thu,fri"
    assert cron_day_of_week(list(range(7))) is None and cron_day_of_week([]) is None


def test_schedule_label() -> None:
    assert schedule_label([(8, 45), (13, 0), (17, 15)], [0, 1, 2, 3, 4]) == "08:45, 13:00, 17:15 (пн–пт)"
    assert schedule_label([(11, 0)], list(range(7))) == "11:00 (ежедневно)"
    assert schedule_label([(11, 0)], [0, 2, 4]) == "11:00 (пн, ср, пт)"
    assert schedule_label([(11, 0)], [5, 6]) == "11:00 (сб, вс)"
    assert schedule_label([], [0]) is None


def test_next_slot_and_wording() -> None:
    slots, days = [(8, 45), (13, 0), (17, 15)], [0, 1, 2, 3, 4]
    tue_0845 = datetime(2026, 9, 15, 8, 45, 5, tzinfo=MSK)  # the 08:45 job has just fired
    nxt = next_slot(tue_0845, slots, days)
    assert nxt == datetime(2026, 9, 15, 13, 0, tzinfo=MSK)
    assert describe_when(nxt, tue_0845) == "сегодня в 13:00"
    fri_evening = datetime(2026, 9, 18, 17, 16, tzinfo=MSK)
    nxt = next_slot(fri_evening, slots, days)
    assert nxt == datetime(2026, 9, 21, 8, 45, tzinfo=MSK) and nxt.weekday() == 0
    assert describe_when(nxt, fri_evening) == "в понедельник в 08:45"
    thu_late = datetime(2026, 9, 17, 23, 0, tzinfo=MSK)
    assert describe_when(next_slot(thu_late, slots, days), thu_late) == "завтра в 08:45"
    # a one-day schedule found up to a week ahead; nothing configured → None
    assert next_slot(tue_0845, [(8, 0)], [1]) == datetime(2026, 9, 22, 8, 0, tzinfo=MSK)
    assert next_slot(tue_0845, [], days) is None and next_slot(tue_0845, slots, []) is None


def test_reminder_status_and_render() -> None:
    settings = Settings(_env_file=None, resume_bump_reminder_at="08:45,13:00,17:15")  # type: ignore[call-arg]
    status = reminder_status(settings)
    assert status["at"] == ["08:45", "13:00", "17:15"] and status["label"] == "08:45, 13:00, 17:15 (пн–пт)"
    assert status["event_enabled"] is True  # in the default TELEGRAM_EVENTS
    off = reminder_status(Settings(_env_file=None, telegram_events="daily"))  # type: ignore[call-arg]
    assert off["label"] is None and off["event_enabled"] is False

    payload = {"url": "https://hh.ru/resume/abc123", "slot": "08:45", "next": "сегодня в 13:00"}
    assert render_event("resume_bump", [payload]) == (
        "⬆️ <b>Пора поднять резюме в поиске</b> (08:45)\n"
        '<a href="https://hh.ru/resume/abc123">Открыть резюме на hh.ru</a> → кнопка «Поднять в поиске».\n'
        "Следующее напоминание — сегодня в 13:00."
    )
    manual = render_event("resume_bump", [{"url": "https://hh.ru/applicant/resumes", "slot": None, "next": None}])
    assert manual == (
        "⬆️ <b>Пора поднять резюме в поиске</b>\n"
        '<a href="https://hh.ru/applicant/resumes">Открыть резюме на hh.ru</a> → кнопка «Поднять в поиске».'
    )
    assert ui_summary("resume_bump", payload) == "Пора поднять резюме (08:45) · следующее сегодня в 13:00"
