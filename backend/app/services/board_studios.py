"""The board «Воронка» for the studios: a found studio → its letter
is ready (Claude writes a business and a simple draft) → the user sent it from their own mailbox or messenger → the
studio answered or declined. Everything comes from the studios and their letters; the actions are the usual
/api/studios endpoints, the panel never sends a letter itself."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import Studio, StudioLetter, StudioLetterRequest
from app.services.studios import sent_today, studio_row

COLUMNS: tuple[tuple[str, str], ...] = (
    ("new", "Найдено"),
    ("ready", "Письмо готово"),
    ("sent", "Отправлено"),
    ("answer", "Ответили"),
)
COLUMN_KEYS: tuple[str, ...] = tuple(key for key, _ in COLUMNS)
# the bands of «Найдено» by the contacts: found on the site or typed in (`contacts`), the site not read yet
# (`unchecked`), nothing found (`nocontacts`, behind a tile: a letter needs an address to go to)
BAND_STATUS: dict[str, str] = {"contacts": "ready", "unchecked": "new", "nocontacts": "no_contacts"}
BANDS: tuple[str, ...] = tuple(BAND_STATUS)
DECIDE_BANDS = ("contacts", "unchecked")  # what «Найдено» counts: a step is possible right away
READY_STATUSES = ("drafting", "drafted")
ANSWER_STATUSES = ("replied", "declined")
CLOSED_STATUSES = ("skipped", "hidden")
LETTER_DONE = ("drafted", "sent", "replied", "declined")
SENT_DONE = ("sent", "replied", "declined")
# cards a column shows on the board before «Ещё N»; «Найдено» shows its summary and the two best ones
PREVIEW = {"new": 2, "ready": 4, "sent": 4, "answer": 4}


def _iso(dt: datetime | None) -> str | None:
    d = as_utc(dt)
    return d.isoformat(timespec="seconds") if d else None


def _has_contacts() -> Any:
    # the condition of the studios page's «with contacts» filter
    return or_(Studio.emails_json.like('%"%@%'), Studio.telegram.isnot(None))


def _condition(key: str, band: str | None) -> Any:
    if key == "new":
        if band is None:
            return Studio.status.in_([BAND_STATUS[b] for b in DECIDE_BANDS])
        return Studio.status == BAND_STATUS[band]
    if key == "ready":
        return Studio.status.in_(READY_STATUSES)
    if key == "sent":
        return Studio.status == "sent"
    return Studio.status.in_(ANSWER_STATUSES)


def _order(key: str) -> list[Any]:
    fit = desc(func.coalesce(Studio.fit_score, -1))
    if key == "new":
        return [fit, desc(Studio.id)]  # the order of the studios page by default
    if key == "ready":
        # a written letter before one Claude is writing
        return [case((Studio.status == "drafted", 0), else_=1), fit, desc(Studio.id)]
    if key == "sent":
        return [desc(Studio.sent_at), desc(Studio.id)]
    return [desc(func.coalesce(Studio.replied_at, Studio.sent_at)), desc(Studio.id)]


async def _items(session: AsyncSession, key: str, rows: list[Studio]) -> list[dict[str, Any]]:
    """The cards: the studios page's row and when the studio came to its column (found, the letter written or asked
    for, sent, answered)."""
    ids = [r.id for r in rows]
    written: dict[int, datetime] = {}
    asked: dict[int, datetime] = {}
    if key == "ready" and ids:
        written = {
            sid: at
            for sid, at in (
                await session.execute(
                    select(StudioLetter.studio_id, func.max(StudioLetter.created_at))
                    .where(StudioLetter.studio_id.in_(ids), StudioLetter.is_current.is_(True))
                    .group_by(StudioLetter.studio_id)
                )
            ).all()
        }
        asked = {
            sid: at
            for sid, at in (
                await session.execute(
                    select(StudioLetterRequest.studio_id, func.max(StudioLetterRequest.created_at))
                    .where(StudioLetterRequest.studio_id.in_(ids))
                    .group_by(StudioLetterRequest.studio_id)
                )
            ).all()
        }
    items = []
    for row in rows:
        if key == "ready":
            at = written.get(row.id) if row.status == "drafted" else asked.get(row.id)
        elif key == "sent":
            at = row.sent_at
        elif key == "answer":
            at = row.replied_at or row.sent_at
        else:
            at = row.first_seen_at
        items.append({"studio": studio_row(row), "stage_at": _iso(at or row.first_seen_at)})
    return items


async def _page(
    session: AsyncSession, key: str, band: str | None, offset: int, limit: int
) -> tuple[list[dict[str, Any]], int]:
    cond = _condition(key, band)
    total = int((await session.execute(select(func.count(Studio.id)).where(cond))).scalar() or 0)
    rows = list(
        (await session.execute(select(Studio).where(cond).order_by(*_order(key)).offset(offset).limit(limit)))
        .scalars()
        .all()
    )
    return await _items(session, key, rows), total


async def funnel(session: AsyncSession) -> list[dict[str, Any]]:
    """The funnel over the board, for all the studios in work (skipped and hidden left out): with contacts, the
    letter written, sent, answered (a refusal is an answer too)."""

    def n(cond: Any) -> Any:
        return func.coalesce(func.sum(case((cond, 1), else_=0)), 0)

    work, contacts, letter, sent, answered = (
        await session.execute(
            select(
                func.count(Studio.id),
                n(_has_contacts()),
                n(Studio.status.in_(LETTER_DONE)),
                n(Studio.status.in_(SENT_DONE)),
                n(Studio.status.in_(ANSWER_STATUSES)),
            ).where(Studio.status.notin_(CLOSED_STATUSES))
        )
    ).one()
    return [
        {"key": "work", "label": "студий в работе", "value": int(work), "hint": "без пропущенных и скрытых"},
        {"key": "contacts", "label": "есть контакты", "value": int(contacts), "hint": "почта или Telegram"},
        {"key": "letter", "label": "письмо готово", "value": int(letter), "hint": None},
        {"key": "sent", "label": "отправлено", "value": int(sent), "hint": None},
        {"key": "answered", "label": "ответили", "value": int(answered), "hint": "ответ или отказ"},
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
    """One column as a list (the open column of the board); «Найдено» takes a band (`contacts` by default)."""
    if key not in COLUMN_KEYS:
        raise ValueError(f"unknown column: {key}")
    if key == "new":
        band = band or "contacts"
        if band not in BANDS:
            raise ValueError(f"unknown band: {band}")
    else:
        band = None
    items, total = await _page(session, key, band, offset, limit)
    return {"source": "studios", "key": key, "band": band, "total": total, "offset": offset, "items": items}


async def board_view(session: AsyncSession, settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    now = as_utc(now) or utcnow()
    by_status = {
        str(status): int(count)
        for status, count in (
            await session.execute(select(Studio.status, func.count(Studio.id)).group_by(Studio.status))
        ).all()
    }
    bands = {band: by_status.get(status, 0) for band, status in BAND_STATUS.items()}
    columns: list[dict[str, Any]] = []
    for key, title in COLUMNS:
        items, count = await _page(session, key, "contacts" if key == "new" else None, 0, PREVIEW[key])
        if key == "new":
            count = sum(bands[b] for b in DECIDE_BANDS)
        columns.append({"key": key, "title": title, "count": count, "items": items})
    return {
        "source": "studios",
        "funnel": await funnel(session),
        "bands": bands,
        "sent_today": await sent_today(session, settings, now),
        "daily_limit": settings.studios_daily_limit,
        "columns": columns,
    }
