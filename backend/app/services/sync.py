"""Synchronization of negotiations with hh.ru: `GET /negotiations` → `negotiations` mirror, events,
external applications, messages, notifications.

Pass plan (F-8.9.1): the first run walks `status=all`; later runs walk `status=non_archived` ordered by
`updated_at desc` and stop once items are older than `last_sync_at − 1 h`, plus a separate `has_updates=true`
pass. Every item is upserted; the diff with the previous snapshot produces `negotiation_events`
(`created`, `viewed`, `state_changed`, `message`) and Telegram/UI notifications.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import Application, Message, Negotiation, NegotiationEvent, ReplyDraft, Vacancy
from app.db.session import release_writes
from app.hh.client import HHClient
from app.hh.errors import (
    HHAuthError,
    HHCaptchaRequired,
    HHError,
    HHForbidden,
    HHNotFound,
)
from app.hh.negotiation import (
    STATE_LABELS,
    MessageData,
    NegotiationData,
    normalize_chat_messages,
    normalize_negotiation,
    normalize_negotiation_messages,
)
from app.hh.vacancy import normalize_vacancy
from app.runtime import Runtime
from app.services.audit import audit
from app.services.counters import bump_counter
from app.services.dictionaries import currency_rates, dictionaries
from app.services.notify import emit_event
from app.services.scanner import apply_data_to_row, fetch_full
from app.services.state import get_system_state, read_capabilities, set_pause, update_capability

log = structlog.get_logger("sync")

PAGE_SIZE = 50
MAX_PAGES = 100
MAX_MESSAGE_FETCHES_PER_RUN = 60
CUTOFF_OVERLAP = timedelta(hours=1)
MESSAGES_PER_FETCH = 50


@dataclass
class SyncContext:
    settings: Settings
    client: HHClient
    caps: dict[str, str]
    first_run: bool
    started_at: datetime
    rates: dict[str, float] = field(default_factory=dict)
    message_budget: int = MAX_MESSAGE_FETCHES_PER_RUN
    notified_negotiations: set[int] = field(default_factory=set)
    summary: dict[str, Any] = field(
        default_factory=lambda: {
            "pages": 0,
            "items": 0,
            "created": 0,
            "updated": 0,
            "external": 0,
            "vacancies_created": 0,
            "events": 0,
            "messages": 0,
            "notifications": 0,
            "message_fetches": 0,
        }
    )

    @property
    def notify(self) -> bool:
        return not self.first_run


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


# ---- vacancies ----------------------------------------------------------------------


async def ensure_vacancy(session: AsyncSession, ctx: SyncContext, data: NegotiationData) -> Vacancy | None:
    """Returns the local vacancy for the negotiation, creating it from the short representation (+ full fetch)."""
    if not data.vacancy_hh_id:
        return None
    row = (await session.execute(select(Vacancy).where(Vacancy.hh_id == data.vacancy_hh_id))).scalar_one_or_none()
    if row is not None:
        return row
    if not data.vacancy_raw:
        return None
    v = normalize_vacancy(data.vacancy_raw, full=False)
    if not v.hh_id:
        return None
    now = utcnow()
    row = Vacancy(
        hh_id=v.hh_id,
        first_seen_at=now,
        last_seen_at=now,
        pipeline_status="already_applied",
        pipeline_reason="отклик найден при синхронизации с hh.ru",
        source_search_ids=",",
        seen_by_user_at=now,
    )
    session.add(row)
    apply_data_to_row(row, v, ctx.rates)
    row.raw_short_json = _json(data.vacancy_raw)
    await session.flush()
    if ctx.caps.get("vacancy_get") != "unavailable":
        await release_writes(session)
        try:
            merged = await fetch_full(ctx.client, row)
            if merged is not None:
                apply_data_to_row(row, merged, ctx.rates)
        except (HHAuthError, HHCaptchaRequired):
            raise
        except HHError as exc:
            log.info("sync_vacancy_full_failed", hh_id=v.hh_id, error=exc.summary())
    ctx.summary["vacancies_created"] += 1
    return row


# ---- applications -------------------------------------------------------------------


async def link_application(
    session: AsyncSession, ctx: SyncContext, data: NegotiationData, vacancy: Vacancy | None
) -> Application | None:
    """Finds the panel's application for this negotiation or records an external one (F-8.9.2).

    Without a local vacancy (hh returned `vacancy: null` — deleted) the negotiation is mirrored but no
    application row is created."""
    app_row = (
        await session.execute(select(Application).where(Application.hh_negotiation_id == data.hh_id))
    ).scalar_one_or_none()
    if app_row is None and vacancy is not None:
        candidates = (
            (
                await session.execute(
                    select(Application)
                    .where(
                        Application.vacancy_id == vacancy.id,
                        Application.status == "sent",
                        Application.dry_run.is_(False),
                        Application.hh_negotiation_id.is_(None),
                    )
                    .order_by(desc(Application.sent_at), desc(Application.id))
                )
            )
            .scalars()
            .all()
        )
        if candidates:
            app_row = candidates[0]
            app_row.hh_negotiation_id = data.hh_id
    if app_row is None and vacancy is None:
        return None
    if app_row is None and vacancy is not None:
        app_row = Application(
            vacancy_id=vacancy.id,
            resume_hh_id=data.resume_hh_id,
            mode="external",
            status="sent",
            sent_at=data.created_at or ctx.started_at,
            queued_at=None,
            hh_negotiation_id=data.hh_id,
            confirmed_by_user=False,
        )
        session.add(app_row)
        await session.flush()
        ctx.summary["external"] += 1
        await bump_counter(session, ctx.settings, "applies_external", moment=app_row.sent_at)
        name = vacancy.name if vacancy is not None else (data.vacancy_name or data.vacancy_hh_id or "?")
        await audit(
            session,
            "sync",
            f"Найден отклик, сделанный вне панели: «{name}»",
            application_id=app_row.id,
            negotiation_id=data.hh_id,
        )
    if vacancy is not None and vacancy.pipeline_status not in ("sent", "queued", "already_applied", "hidden"):
        vacancy.pipeline_status = "already_applied"
        vacancy.pipeline_reason = "по вакансии есть отклик (синхронизация)"
        vacancy.batch_id = None
    return app_row


# ---- messages -----------------------------------------------------------------------


async def fetch_messages(ctx: SyncContext, row: Negotiation) -> list[MessageData]:
    """Chats API first (when the probe confirmed it and a chat_id exists), else negotiation messages (F-8.9.5)."""
    client = ctx.client
    if ctx.caps.get("chats") == "available" and row.chat_id:
        try:
            payload = await client.get(
                f"/common/chats/{row.chat_id}/messages", params={"limit": MESSAGES_PER_FETCH, "order": "prev"}
            )
            return normalize_chat_messages(payload)
        except (HHForbidden, HHNotFound) as exc:
            if isinstance(exc, (HHAuthError, HHCaptchaRequired)):
                raise
            log.info("chat_messages_unavailable", chat_id=row.chat_id, error=exc.summary())
    payload = await client.get(
        f"/negotiations/{row.hh_id}/messages",
        params={"page": 0, "per_page": MESSAGES_PER_FETCH, "with_text_only": "true"},
    )
    return normalize_negotiation_messages(payload)


async def sync_messages(
    session: AsyncSession, ctx: SyncContext, row: Negotiation, vacancy: Vacancy | None
) -> list[Message]:
    """Stores new messages; returns the new employer messages (for events/notifications)."""
    if ctx.message_budget <= 0:
        return []
    ctx.message_budget -= 1
    ctx.summary["message_fetches"] += 1
    await release_writes(session)
    try:
        items = await fetch_messages(ctx, row)
    except (HHAuthError, HHCaptchaRequired):
        raise
    except HHError as exc:
        log.info("messages_fetch_failed", negotiation=row.hh_id, error=exc.summary())
        return []
    known: set[str] = set(
        (await session.execute(select(Message.hh_message_id).where(Message.negotiation_id == row.id))).scalars().all()
    )
    new_employer: list[Message] = []
    for item in items:
        if item.hh_message_id in known:
            continue
        msg = Message(
            negotiation_id=row.id,
            hh_message_id=item.hh_message_id,
            author_type=item.author_type,
            author_name=item.author_name,
            text=item.text,
            hh_created_at=item.created_at,
            viewed_by_me=item.viewed_by_me,
            viewed_by_opponent=item.viewed_by_opponent,
            source=item.source,
            raw_json=_json(item.raw),
        )
        session.add(msg)
        known.add(item.hh_message_id)
        ctx.summary["messages"] += 1
        if item.author_type == "employer" and item.text:
            new_employer.append(msg)
    row.messages_synced_at = utcnow()
    await session.flush()
    if new_employer:
        latest = max((as_utc(m.hh_created_at) or ctx.started_at) for m in new_employer)
        row.last_employer_message_at = latest
        earliest = min((as_utc(m.hh_created_at) or ctx.started_at) for m in new_employer)
        _touch_reaction(row, earliest)
        for msg in new_employer:
            await _add_event(
                session,
                ctx,
                row,
                "message",
                occurred_at=as_utc(msg.hh_created_at) or ctx.started_at,
                payload={"message_id": msg.hh_message_id, "text": (msg.text or "")[:500]},
            )
    return new_employer


# ---- events / notifications ---------------------------------------------------------


def _touch_reaction(row: Negotiation, moment: datetime | None) -> None:
    if moment is None:
        return
    current = as_utc(row.first_reaction_at)
    if current is None or moment < current:
        row.first_reaction_at = moment


async def _add_event(
    session: AsyncSession,
    ctx: SyncContext,
    row: Negotiation,
    event_type: str,
    *,
    occurred_at: datetime,
    from_state: str | None = None,
    to_state: str | None = None,
    payload: dict[str, Any] | None = None,
) -> NegotiationEvent:
    ev = NegotiationEvent(
        negotiation_id=row.id,
        event_type=event_type,
        from_state=from_state,
        to_state=to_state,
        occurred_at=occurred_at,
        payload_json=_json(payload) if payload else None,
    )
    session.add(ev)
    ctx.summary["events"] += 1
    return ev


def _vacancy_payload(row: Negotiation, vacancy: Vacancy | None, data: NegotiationData) -> dict[str, Any]:
    return {
        "negotiation_id": row.hh_id,
        "name": (vacancy.name if vacancy is not None else data.vacancy_name) or f"вакансия {data.vacancy_hh_id}",
        "employer": (vacancy.employer_name if vacancy is not None else data.employer_name),
        "url": vacancy.alternate_url if vacancy is not None else None,
        "vacancy_id": vacancy.id if vacancy is not None else None,
    }


async def _notify(session: AsyncSession, ctx: SyncContext, event_type: str, payload: dict[str, Any]) -> None:
    if not ctx.notify:
        return
    await emit_event(session, ctx.settings, event_type, payload)
    ctx.summary["notifications"] += 1


# ---- one negotiation ------------------------------------------------------------------


async def process_item(session: AsyncSession, ctx: SyncContext, raw: dict[str, Any]) -> Negotiation | None:
    data = normalize_negotiation(raw)
    if not data.hh_id:
        return None
    row = (await session.execute(select(Negotiation).where(Negotiation.hh_id == data.hh_id))).scalar_one_or_none()
    is_new = row is None
    prev_state = row.state_id if row else None
    prev_viewed = row.viewed_by_opponent if row else False
    prev_unread = row.unread_count if row else 0
    prev_messages = row.messages_count if row else 0
    prev_updated = as_utc(row.hh_updated_at) if row else None
    if row is None:
        row = Negotiation(hh_id=data.hh_id)
        session.add(row)

    vacancy = await ensure_vacancy(session, ctx, data)
    app_row = await link_application(session, ctx, data, vacancy)

    row.vacancy_hh_id = data.vacancy_hh_id
    row.vacancy_id = vacancy.id if vacancy is not None else None
    row.application_id = app_row.id if app_row is not None else None
    row.resume_hh_id = data.resume_hh_id
    row.state_id = data.state_id
    row.state_name = data.state_name
    row.hidden = data.hidden
    row.viewed_by_opponent = data.viewed_by_opponent
    row.has_updates = data.has_updates
    row.messaging_status = data.messaging_status
    row.chat_id = data.chat_id
    row.unread_count = data.unread_count
    row.messages_count = data.messages_count
    row.url = data.url
    row.hh_created_at = data.created_at
    row.hh_updated_at = data.updated_at
    row.raw_json = _json(raw)
    row.last_synced_at = ctx.started_at
    await session.flush()

    changed_at = data.updated_at or ctx.started_at
    if is_new:
        ctx.summary["created"] += 1
        await _add_event(
            session, ctx, row, "created", occurred_at=data.created_at or changed_at, to_state=data.state_id
        )
    elif prev_updated != data.updated_at or prev_state != data.state_id or prev_viewed != data.viewed_by_opponent:
        ctx.summary["updated"] += 1

    # viewed by the employer
    if data.viewed_by_opponent and not prev_viewed:
        row.first_viewed_at = row.first_viewed_at or changed_at
        _touch_reaction(row, changed_at)
        await _add_event(session, ctx, row, "viewed", occurred_at=changed_at)

    # state transitions (a brand-new row already in invitation/discard counts as a transition from response)
    state_changed = (not is_new and prev_state != data.state_id) or (
        is_new and data.state_id in ("invitation", "discard")
    )
    state_payload = _vacancy_payload(row, vacancy, data)
    if state_changed:
        await _add_event(
            session,
            ctx,
            row,
            "state_changed",
            occurred_at=changed_at,
            from_state=prev_state or "response",
            to_state=data.state_id,
        )
        if data.state_id == "invitation":
            row.invited_at = row.invited_at or changed_at
            _touch_reaction(row, changed_at)
        elif data.state_id == "discard":
            row.discarded_at = row.discarded_at or changed_at
            _touch_reaction(row, changed_at)

    # messages: fetch when hh signals updates, counters grew, state changed, or (first run) invitation/discard
    need_messages = (
        data.has_updates
        or data.unread_count > prev_unread
        or data.messages_count > prev_messages
        or state_changed
        or (is_new and data.state_id in ("invitation", "discard"))
    )
    new_employer: list[Message] = []
    if need_messages and data.messaging_status not in ("prohibited",):
        new_employer = await sync_messages(session, ctx, row, vacancy)

    # notifications (never on the first full run)
    if state_changed and data.state_id in ("invitation", "discard"):
        payload = dict(state_payload)
        if data.state_id == "invitation" and new_employer:
            payload["text"] = new_employer[-1].text
        await _notify(session, ctx, data.state_id, payload)
        ctx.notified_negotiations.add(row.id)
    elif new_employer and row.id not in ctx.notified_negotiations:
        for msg in new_employer:
            payload = dict(state_payload)
            payload.update({"author": msg.author_name or "работодатель", "text": msg.text})
            await _notify(session, ctx, "message", payload)
    return row


# ---- listing pages ----------------------------------------------------------------------


async def _walk(
    session: AsyncSession, ctx: SyncContext, params: dict[str, Any], *, cutoff: datetime | None, seen: set[str]
) -> None:
    page = 0
    while page < MAX_PAGES:
        query = {"order_by": "updated_at", "order": "desc", "per_page": PAGE_SIZE, "page": page, **params}
        payload = await ctx.client.get("/negotiations", params=query)
        ctx.summary["pages"] += 1
        items = payload.get("items") if isinstance(payload, dict) else None
        if not items:
            break
        stop = False
        for raw in items:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            hh_id = str(raw["id"])
            updated = as_utc(_parse(raw.get("updated_at")))
            if cutoff is not None and updated is not None and updated < cutoff:
                stop = True
                break
            if hh_id in seen:
                continue
            seen.add(hh_id)
            ctx.summary["items"] += 1
            await process_item(session, ctx, raw)
        await session.commit()
        pages = int(payload.get("pages") or 0) if isinstance(payload, dict) else 0
        if stop or page + 1 >= pages:
            break
        page += 1


def _parse(value: Any) -> datetime | None:
    from app.hh.vacancy import parse_hh_datetime

    return parse_hh_datetime(value)


# ---- job body -----------------------------------------------------------------------------


async def run_sync(runtime: Runtime, *, full: bool = False) -> dict[str, Any]:
    """Job body for `sync`. Returns a summary dict (stored in job_runs.stats_json)."""
    settings = runtime.settings
    client = runtime.hh_client()
    async with runtime.session_factory() as session:
        state = await get_system_state(session)
        if state.paused_reason:
            return {"skipped": f"paused: {state.paused_reason}"}
        caps = read_capabilities(state)
        if caps.get("negotiations_list") == "unavailable":
            return {"skipped": "GET /negotiations недоступен приложению (по данным зонда)"}
        last_sync = None if full else as_utc(state.last_sync_at)
        ctx = SyncContext(settings=settings, client=client, caps=caps, first_run=last_sync is None, started_at=utcnow())
        try:
            ctx.rates = currency_rates(await dictionaries(session, client, settings.dictionaries_ttl_days))
        except (HHAuthError, HHCaptchaRequired):
            raise
        except Exception as exc:  # noqa: BLE001 - rates are optional
            log.info("sync_rates_unavailable", error=str(exc))
            ctx.rates = {}
        seen: set[str] = set()
        try:
            if ctx.first_run:
                await _walk(session, ctx, {"status": "all"}, cutoff=None, seen=seen)
            else:
                cutoff = last_sync - CUTOFF_OVERLAP if last_sync else None
                await _walk(session, ctx, {"status": "non_archived"}, cutoff=cutoff, seen=seen)
                await _walk(session, ctx, {"has_updates": "true"}, cutoff=None, seen=seen)
        except HHCaptchaRequired as exc:
            await set_pause(session, "captcha", runtime.captcha_url)
            await audit(session, "sync", f"Синхронизация остановлена: {exc.summary()}", level="error")
            await session.commit()
            ctx.summary["aborted"] = exc.summary()
            return _finish(ctx, client)
        except HHAuthError as exc:
            await audit(session, "sync", f"Синхронизация остановлена: {exc.summary()}", level="error")
            await session.commit()
            ctx.summary["aborted"] = exc.summary()
            return _finish(ctx, client)
        except HHForbidden as exc:
            await update_capability(session, "negotiations_list", "unavailable")
            await audit(
                session,
                "sync",
                f"GET /negotiations недоступен приложению: {exc.summary()}; синхронизация отключена",
                level="warning",
            )
            await session.commit()
            ctx.summary["aborted"] = exc.summary()
            return _finish(ctx, client)
        except HHError as exc:
            await audit(session, "sync", f"Ошибка синхронизации: {exc.summary()}", level="warning")
            await session.commit()
            ctx.summary["error"] = exc.summary()
            return _finish(ctx, client)

        await update_capability(session, "negotiations_list", "available")
        state = await get_system_state(session)
        state.last_sync_at = ctx.started_at
        await bump_counter(session, settings, "hh_requests", client.requests_made)
        s = ctx.summary
        await audit(
            session,
            "sync",
            f"Синхронизация откликов: получено {s['items']}, новых {s['created']}, внешних {s['external']}, "
            f"событий {s['events']}, сообщений {s['messages']}" + (" (первый полный проход)" if ctx.first_run else ""),
        )
        await session.commit()
    return _finish(ctx, client)


def _finish(ctx: SyncContext, client: HHClient) -> dict[str, Any]:
    ctx.summary["hh_requests"] = client.requests_made
    ctx.summary["first_run"] = ctx.first_run
    return ctx.summary


# ---- read side -----------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    d = as_utc(dt)
    return d.isoformat(timespec="seconds") if d else None


def negotiation_row(row: Negotiation | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "hh_id": row.hh_id,
        "state_id": row.state_id,
        "state_label": STATE_LABELS.get(row.state_id or "", row.state_name or row.state_id or ""),
        "hidden": row.hidden,
        "viewed_by_opponent": row.viewed_by_opponent,
        "has_updates": row.has_updates,
        "messaging_status": row.messaging_status,
        "unread_count": row.unread_count,
        "messages_count": row.messages_count,
        "chat_id": row.chat_id,
        "hh_created_at": _iso(row.hh_created_at),
        "hh_updated_at": _iso(row.hh_updated_at),
        "first_viewed_at": _iso(row.first_viewed_at),
        "first_reaction_at": _iso(row.first_reaction_at),
        "invited_at": _iso(row.invited_at),
        "discarded_at": _iso(row.discarded_at),
        "last_employer_message_at": _iso(row.last_employer_message_at),
        "messages_synced_at": _iso(row.messages_synced_at),
        "last_synced_at": _iso(row.last_synced_at),
        "manual": row.hh_id.startswith("manual-"),
        "page_url": (
            row.url if row.hh_id.startswith("manual-") else f"https://hh.ru/applicant/negotiations/item?id={row.hh_id}"
        ),
    }


def message_row(m: Message) -> dict[str, Any]:
    return {
        "id": m.id,
        "hh_message_id": m.hh_message_id,
        "author_type": m.author_type,
        "author_name": m.author_name,
        "text": m.text,
        "created_at": _iso(m.hh_created_at),
        "viewed_by_me": m.viewed_by_me,
        "viewed_by_opponent": m.viewed_by_opponent,
        "source": m.source,
    }


def event_row(e: NegotiationEvent) -> dict[str, Any]:
    try:
        payload = json.loads(e.payload_json) if e.payload_json else None
    except ValueError:
        payload = None
    labels = {
        "created": "отклик создан",
        "viewed": "просмотрен работодателем",
        "state_changed": "смена статуса",
        "message": "сообщение работодателя",
    }
    return {
        "id": e.id,
        "event_type": e.event_type,
        "label": labels.get(e.event_type, e.event_type),
        "from_state": e.from_state,
        "to_state": e.to_state,
        "to_state_label": STATE_LABELS.get(e.to_state or "", e.to_state or ""),
        "occurred_at": _iso(e.occurred_at),
        "payload": payload,
    }


async def negotiation_detail(session: AsyncSession, row: Negotiation) -> dict[str, Any]:
    messages = (
        (
            await session.execute(
                select(Message).where(Message.negotiation_id == row.id).order_by(Message.hh_created_at, Message.id)
            )
        )
        .scalars()
        .all()
    )
    events = (
        (
            await session.execute(
                select(NegotiationEvent)
                .where(NegotiationEvent.negotiation_id == row.id)
                .order_by(NegotiationEvent.occurred_at, NegotiationEvent.id)
            )
        )
        .scalars()
        .all()
    )
    drafts = (
        (
            await session.execute(
                select(ReplyDraft).where(ReplyDraft.negotiation_id == row.id).order_by(desc(ReplyDraft.id)).limit(5)
            )
        )
        .scalars()
        .all()
    )
    return {
        "negotiation": negotiation_row(row),
        "messages": [message_row(m) for m in messages],
        "events": [event_row(e) for e in events],
        "reply_drafts": [reply_draft_row(d) for d in drafts],
    }


def reply_draft_row(d: ReplyDraft) -> dict[str, Any]:
    try:
        clar = json.loads(d.needs_clarification_json) if d.needs_clarification_json else []
    except ValueError:
        clar = []
    labels = {
        "queued": "в очереди на пакет",
        "sent_in_batch": "в пакете для Claude",
        "done": "готов",
        "cancelled": "не выполнен",
    }
    return {
        "id": d.id,
        "status": d.status,
        "status_label": labels.get(d.status, d.status),
        "user_intent": d.user_intent,
        "batch_id": d.batch_id,
        "text": d.draft_text,
        "intent": d.intent,
        "needs_clarification": clar,
        "error": d.error,
        "created_at": _iso(d.created_at),
        "updated_at": _iso(d.updated_at),
    }


async def request_reply_draft(
    session: AsyncSession, settings: Settings, row: Negotiation, user_intent: str | None
) -> tuple[ReplyDraft, str | None]:
    """Queues a `reply_draft` task and tries to build a packet right away. Returns (draft, packet id or None)."""
    from app.exchange.packets import PacketError, build_batches

    if row.vacancy_id is None:
        raise ValueError("у отклика нет вакансии в базе панели")
    for old in (
        await session.execute(
            select(ReplyDraft).where(ReplyDraft.negotiation_id == row.id, ReplyDraft.status == "queued")
        )
    ).scalars():
        old.status = "cancelled"
        old.error = "заменён новым запросом"
    intent = (user_intent or "").strip() or None
    draft = ReplyDraft(negotiation_id=row.id, vacancy_id=row.vacancy_id, user_intent=intent)
    session.add(draft)
    await session.flush()
    await audit(session, "batches", "Запрошен черновик ответа работодателю через Claude", negotiation_id=row.hh_id)
    try:
        created = await build_batches(session, settings, vacancy_ids=[], include_revisions=True)
    except PacketError:
        return draft, None
    return draft, (created[0].batch_id if created else None)


async def refresh_messages(runtime: Runtime, session: AsyncSession, row: Negotiation) -> dict[str, Any]:
    """Manual «обновить переписку» for one negotiation (outside the job)."""
    state = await get_system_state(session)
    caps = read_capabilities(state)
    ctx = SyncContext(
        settings=runtime.settings, client=runtime.hh_client(), caps=caps, first_run=True, started_at=utcnow()
    )
    vacancy = await session.get(Vacancy, row.vacancy_id) if row.vacancy_id else None
    await sync_messages(session, ctx, row, vacancy)
    await session.flush()
    return await negotiation_detail(session, row)


async def sync_status(session: AsyncSession) -> dict[str, Any]:
    from sqlalchemy import func

    state = await get_system_state(session)
    caps = read_capabilities(state)
    unread = int(
        (await session.execute(select(func.count(Negotiation.id)).where(Negotiation.has_updates.is_(True)))).scalar()
        or 0
    )
    total = int((await session.execute(select(func.count(Negotiation.id)))).scalar() or 0)
    return {
        "last_sync_at": _iso(state.last_sync_at),
        "capability": caps.get("negotiations_list", "unknown"),
        "chats_capability": caps.get("chats", "unknown"),
        "negotiations": total,
        "with_updates": unread,
    }
