from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.config import Settings
from app.services.applier import (
    in_window,
    local_day_bounds,
    negotiation_id_from_location,
    next_day_resume_at,
    parse_window,
)


def test_parse_window() -> None:
    start, end = parse_window("09:00-21:00")
    assert (start.hour, end.hour) == (9, 21)
    start, end = parse_window("garbage")
    assert (start.hour, end.hour, end.minute) == (0, 23, 59)


def test_in_window_regular_and_overnight() -> None:
    tz = ZoneInfo("Europe/Moscow")
    assert in_window(datetime(2026, 9, 11, 12, 0, tzinfo=tz), "09:00-21:00")
    assert not in_window(datetime(2026, 9, 11, 22, 0, tzinfo=tz), "09:00-21:00")
    assert in_window(datetime(2026, 9, 11, 9, 0, tzinfo=tz), "09:00-21:00")
    assert in_window(datetime(2026, 9, 11, 23, 30, tzinfo=tz), "22:00-06:00")
    assert in_window(datetime(2026, 9, 11, 5, 0, tzinfo=tz), "22:00-06:00")
    assert not in_window(datetime(2026, 9, 11, 12, 0, tzinfo=tz), "22:00-06:00")


def test_day_bounds_and_resume(settings: Settings) -> None:
    settings = settings.model_copy(update={"timezone": "Europe/Moscow"})
    now = datetime(2026, 9, 11, 20, 30, tzinfo=UTC)  # 23:30 MSK
    start, end = local_day_bounds(settings, now)
    assert start == datetime(2026, 9, 10, 21, 0, tzinfo=UTC)  # 00:00 MSK on 11.09
    assert end == datetime(2026, 9, 11, 21, 0, tzinfo=UTC)
    resume = next_day_resume_at(settings, now)
    assert resume == datetime(2026, 9, 11, 21, 5, tzinfo=UTC)  # 00:05 MSK on 12.09


def test_negotiation_id_from_location() -> None:
    assert negotiation_id_from_location("/negotiations/123") == "123"
    assert negotiation_id_from_location("https://api.hh.ru/negotiations/abc?x=1") == "abc"
    assert negotiation_id_from_location("https://employer.example/respond") is None
    assert negotiation_id_from_location(None) is None
