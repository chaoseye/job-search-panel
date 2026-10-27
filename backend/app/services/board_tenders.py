"""The board «Воронка» for the tenders of workspace.ru: a found tender
→ its answer is ready (Claude writes it from the tender's text) → the user responded on workspace.ru → the organizer
answered or declined (the user records it by hand: the site tells the user, not the panel). Everything comes from
ws_tenders and their answers; the actions are the usual /api/tenders endpoints. The table is small (a few new tenders
a day), so the columns are sorted in Python with the same `is_open` the tenders page uses."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import WsTender, WsTenderReply, WsTenderReplyRequest
from app.services.applier import local_day_bounds
from app.services.stats import resolve_period
from app.services.ws_tenders import ANSWER_STATUSES, RESPONDED_STATUSES, is_open, tender_row, today_iso

COLUMNS: tuple[tuple[str, str], ...] = (
    ("new", "Найдено"),
    ("ready", "Отклик готов"),
    ("responded", "Откликнулся"),
    ("answer", "Ответ"),
)
COLUMN_KEYS: tuple[str, ...] = tuple(key for key, _ in COLUMNS)
# the bands of «Найдено»: open tenders that suit the questionnaire and the watched sections (`relevant`) and the other
# open ones (`other`, behind a tile); a closed tender leaves «Найдено»
BANDS: tuple[str, ...] = ("relevant", "other")
FOUND_STATUSES = ("new", "interesting")
READY_STATUSES = ("drafting", "drafted")
# «suitable» in the funnel: the stored flag drops once a tender closes, a step the user took stays
ACTED_STATUSES = ("interesting", "drafting", "drafted", *RESPONDED_STATUSES)
# cards a column shows on the board before «Ещё N»; «Найдено» shows its summary and the two best ones
PREVIEW = {"new": 2, "ready": 4, "responded": 4, "answer": 4}
FUNNEL_PERIOD = "30d"


def _iso(dt: datetime | None) -> str | None:
    d = as_utc(dt)
    return d.isoformat(timespec="seconds") if d else None


def _ts(dt: datetime | None) -> float:
    d = as_utc(dt)
    return d.timestamp() if d else 0.0


def band_of(row: WsTender, today: str) -> str | None:
    """The band of «Найдено» the tender is in, None when it is not there."""
    if row.status not in FOUND_STATUSES or not is_open(row, today):
        return None
    return "relevant" if row.relevant else "other"


def column_of(row: WsTender, today: str) -> str | None:
    if row.status in FOUND_STATUSES:
        return "new" if is_open(row, today) else None
    if row.status in READY_STATUSES:
        return "ready"
    if row.status == "responded":
        return "responded"
    if row.status in ANSWER_STATUSES:
        return "answer"
    return None  # skipped, hidden


def _ordered(key: str, rows: list[WsTender], today: str) -> list[WsTender]:
    if key == "new":
        # the order of the tenders page by default: the best fit, then the newest
        return sorted(rows, key=lambda r: (r.fit_score or 0, r.published_on or "", r.ws_id), reverse=True)
    if key == "ready":
        # what can still be posted first, a written answer before one Claude is writing, the nearest deadline first
        return sorted(
            rows, key=lambda r: (not is_open(r, today), r.status != "drafted", r.deadline_on or "9999-12-31", -r.id)
        )
    if key == "answer":
        return sorted(rows, key=lambda r: (_ts(r.answered_at or r.responded_at), r.id), reverse=True)
    return sorted(rows, key=lambda r: (_ts(r.responded_at), r.id), reverse=True)


def _members(rows: list[WsTender], key: str, band: str | None, today: str) -> list[WsTender]:
    found = [r for r in rows if column_of(r, today) == key]
    if key == "new":
        found = [r for r in found if band_of(r, today) == band]
    return _ordered(key, found, today)


async def _items(session: AsyncSession, key: str, rows: list[WsTender], today: str) -> list[dict[str, Any]]:
    """The cards: the tenders page's row and when the tender came to its column (found, the answer written or asked
    for, responded, the organizer's answer recorded)."""
    ids = [r.id for r in rows]
    written: dict[int, datetime] = {}
    asked: dict[int, datetime] = {}
    if key == "ready" and ids:
        written = {
            tid: at
            for tid, at in (
                await session.execute(
                    select(WsTenderReply.tender_id, WsTenderReply.created_at).where(
                        WsTenderReply.tender_id.in_(ids), WsTenderReply.is_current.is_(True)
                    )
                )
            ).all()
        }
        asked = {
            tid: at
            for tid, at in (
                await session.execute(
                    select(WsTenderReplyRequest.tender_id, func.max(WsTenderReplyRequest.created_at))
                    .where(WsTenderReplyRequest.tender_id.in_(ids))
                    .group_by(WsTenderReplyRequest.tender_id)
                )
            ).all()
        }
    items = []
    for row in rows:
        if key == "ready":
            at = written.get(row.id) if row.status == "drafted" else asked.get(row.id)
        elif key == "responded":
            at = row.responded_at
        elif key == "answer":
            at = row.answered_at or row.responded_at
        else:
            at = row.first_seen_at
        items.append({"tender": tender_row(row, today), "stage_at": _iso(at or row.first_seen_at)})
    return items


async def _all(session: AsyncSession) -> list[WsTender]:
    return list((await session.execute(select(WsTender))).scalars().all())


async def funnel(
    session: AsyncSession, settings: Settings, rows: list[WsTender], now: datetime | None = None
) -> list[dict[str, Any]]:
    """The 30-day funnel over the board, as the vacancies' one counts: tenders found in the period (and the suitable
    ones of them), answers written in the period, responses marked in the period, the organizers' answers recorded in
    the period (a refusal is an answer too)."""
    period = resolve_period(settings, FUNNEL_PERIOD, now=now)
    found = [r for r in rows if period.contains(r.first_seen_at)]
    written = set(
        (
            await session.execute(
                select(WsTenderReply.tender_id)
                .where(WsTenderReply.created_at >= period.start, WsTenderReply.created_at < period.end)
                .distinct()
            )
        ).scalars()
    )
    return [
        {"key": "found", "label": "найдено за 30 дней", "value": len(found), "hint": "тендеры workspace.ru"},
        {
            "key": "suitable",
            "label": "подходят",
            "value": sum(1 for r in found if r.relevant or r.status in ACTED_STATUSES),
            "hint": "подходят анкете и разделам или уже в работе",
        },
        {"key": "ready", "label": "отклик готов", "value": len(written), "hint": "отклики, написанные за 30 дней"},
        {
            "key": "responded",
            "label": "откликнулся",
            "value": sum(1 for r in rows if r.status in RESPONDED_STATUSES and period.contains(r.responded_at)),
            "hint": "отклики, отмеченные за 30 дней",
        },
        {
            "key": "answered",
            "label": "ответили",
            "value": sum(1 for r in rows if r.status in ANSWER_STATUSES and period.contains(r.answered_at)),
            "hint": "ответы и отказы заказчиков, отмеченные за 30 дней",
        },
    ]


async def column_items(
    session: AsyncSession,
    settings: Settings,
    key: str,
    *,
    band: str | None = None,
    offset: int = 0,
    limit: int = 60,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One column as a list (the open column of the board); «Найдено» takes a band (`relevant` by default)."""
    if key not in COLUMN_KEYS:
        raise ValueError(f"unknown column: {key}")
    if key == "new":
        band = band or "relevant"
        if band not in BANDS:
            raise ValueError(f"unknown band: {band}")
    else:
        band = None
    today = today_iso(settings, as_utc(now) or utcnow())
    members = _members(await _all(session), key, band, today)
    return {
        "source": "tenders",
        "key": key,
        "band": band,
        "total": len(members),
        "offset": offset,
        "items": await _items(session, key, members[offset : offset + limit], today),
    }


async def board_view(session: AsyncSession, settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    now = as_utc(now) or utcnow()
    today = today_iso(settings, now)
    day_start, _ = local_day_bounds(settings, now)
    rows = await _all(session)
    bands = {band: sum(1 for r in rows if band_of(r, today) == band) for band in BANDS}
    columns: list[dict[str, Any]] = []
    for key, title in COLUMNS:
        members = _members(rows, key, "relevant" if key == "new" else None, today)
        columns.append(
            {
                "key": key,
                "title": title,
                "count": len(members),
                "items": await _items(session, key, members[: PREVIEW[key]], today),
            }
        )
    return {
        "source": "tenders",
        "funnel": await funnel(session, settings, rows, now),
        "bands": bands,
        "fresh_today": sum(
            1 for r in rows if band_of(r, today) == "relevant" and (as_utc(r.first_seen_at) or now) >= day_start
        ),
        "columns": columns,
    }
