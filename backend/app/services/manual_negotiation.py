"""Manual journal of employer responses for applications the panel cannot sync (hh closed `GET /negotiations`
for the app, «Ассистент» mode). The user records what happened on hh.ru — viewed, invitation, rejection, a
message — and the panel stores it as an ordinary Negotiation (hh_id `manual-<application id>`) with messages and
events, so the applications page, stage filters, statistics and reply drafts for Claude work unchanged."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import as_utc, utcnow
from app.db.models import (
    Application,
    InterviewPrep,
    Message,
    Negotiation,
    NegotiationEvent,
    ReplyDraft,
    Vacancy,
)
from app.hh.negotiation import STATE_LABELS
from app.services.audit import audit

MANUAL_PREFIX = "manual-"
MANUAL_STATES: tuple[str, ...] = ("response", "invitation", "discard")
Author = Literal["employer", "applicant"]


class ManualError(Exception):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def is_manual(hh_id: str | None) -> bool:
    return bool(hh_id) and str(hh_id).startswith(MANUAL_PREFIX)


def manual_id(app_row: Application) -> str:
    return f"{MANUAL_PREFIX}{app_row.id}"


async def ensure_manual(session: AsyncSession, app_row: Application) -> Negotiation:
    """The manual negotiation of a sent application (created on first use)."""
    if app_row.status != "sent":
        raise ManualError("Ответ работодателя можно записать только для отправленного отклика")
    if app_row.hh_negotiation_id and not is_manual(app_row.hh_negotiation_id):
        raise ManualError("У этого отклика есть данные hh.ru — они обновляются синхронизацией, ручной журнал не нужен")
    hh_id = manual_id(app_row)
    row = (await session.execute(select(Negotiation).where(Negotiation.hh_id == hh_id))).scalar_one_or_none()
    if row is not None:
        return row
    vacancy = await session.get(Vacancy, app_row.vacancy_id)
    sent_at = as_utc(app_row.sent_at) or utcnow()
    row = Negotiation(
        hh_id=hh_id,
        vacancy_hh_id=vacancy.hh_id if vacancy else None,
        vacancy_id=app_row.vacancy_id,
        application_id=app_row.id,
        state_id="response",
        state_name=STATE_LABELS.get("response", "Отклик"),
        messaging_status="manual",
        url=vacancy.alternate_url if vacancy else None,
        hh_created_at=sent_at,
        hh_updated_at=sent_at,
        raw_json=json.dumps({"source": "manual", "application_id": app_row.id}),
        last_synced_at=utcnow(),
    )
    session.add(row)
    await session.flush()
    session.add(NegotiationEvent(negotiation_id=row.id, event_type="created", to_state="response", occurred_at=sent_at))
    app_row.hh_negotiation_id = hh_id
    await session.flush()
    return row


def _touch_reaction(row: Negotiation, moment: datetime) -> None:
    current = as_utc(row.first_reaction_at)
    if current is None or moment < current:
        row.first_reaction_at = moment


def _touch_viewed(row: Negotiation, moment: datetime) -> bool:
    """Marks the negotiation as seen by the employer; returns True when this is the first time."""
    first = not row.viewed_by_opponent
    row.viewed_by_opponent = True
    if row.first_viewed_at is None:
        row.first_viewed_at = moment
    return first


async def record_update(
    session: AsyncSession,
    app_row: Application,
    *,
    state: str | None = None,
    viewed: bool | None = None,
    text: str | None = None,
    author: Author = "employer",
    occurred_at: datetime | None = None,
) -> Negotiation:
    """Writes what the user saw on hh.ru: a stage change and/or a message. Nothing is sent to hh."""
    if state is not None and state not in MANUAL_STATES:
        raise ManualError(f"Недопустимый статус: {state}", 422)
    text = (text or "").strip()
    if state is None and not viewed and not text:
        raise ManualError("Нечего записывать: укажите статус, просмотр или текст сообщения", 422)
    moment = as_utc(occurred_at) or utcnow()
    row = await ensure_manual(session, app_row)
    notes: list[str] = []

    if viewed or (author == "employer" and text) or state in ("invitation", "discard"):
        if _touch_viewed(row, moment):
            session.add(NegotiationEvent(negotiation_id=row.id, event_type="viewed", occurred_at=moment))
            notes.append("просмотрен")

    if state is not None and state != row.state_id:
        session.add(
            NegotiationEvent(
                negotiation_id=row.id,
                event_type="state_changed",
                from_state=row.state_id or "response",
                to_state=state,
                occurred_at=moment,
            )
        )
        row.state_id = state
        row.state_name = STATE_LABELS.get(state, state)
        if state == "invitation":
            row.invited_at = row.invited_at or moment
            _touch_reaction(row, moment)
        elif state == "discard":
            row.discarded_at = row.discarded_at or moment
            _touch_reaction(row, moment)
        notes.append(row.state_name or state)

    if text:
        session.add(
            Message(
                negotiation_id=row.id,
                hh_message_id=f"{MANUAL_PREFIX}{uuid.uuid4().hex[:12]}",
                author_type=author,
                author_name="работодатель" if author == "employer" else "вы",
                text=text,
                hh_created_at=moment,
                source="manual",
            )
        )
        row.messages_count = (row.messages_count or 0) + 1
        if author == "employer":
            row.last_employer_message_at = moment
            _touch_reaction(row, moment)
            session.add(
                NegotiationEvent(
                    negotiation_id=row.id,
                    event_type="message",
                    occurred_at=moment,
                    payload_json=json.dumps({"author": "работодатель", "text": text[:300]}, ensure_ascii=False),
                )
            )
        notes.append("сообщение работодателя" if author == "employer" else "ваш ответ")

    row.hh_updated_at = moment
    row.last_synced_at = utcnow()
    await session.flush()
    await audit(
        session,
        "negotiations",
        "Ручная запись по отклику: " + ", ".join(notes),
        negotiation_id=row.hh_id,
        application_id=app_row.id,
    )
    return row


async def delete_manual_journal(session: AsyncSession, app_row: Application) -> int:
    """Removes the whole manual journal of an application (messages, events, reply drafts) and detaches it, as
    if nothing had been recorded. Returns the number of removed messages."""
    if not is_manual(app_row.hh_negotiation_id):
        raise ManualError("У этого отклика нет ручного журнала", 404)
    row = (
        await session.execute(select(Negotiation).where(Negotiation.hh_id == app_row.hh_negotiation_id))
    ).scalar_one_or_none()
    removed = 0
    if row is not None:
        removed = int(
            (await session.execute(select(func.count(Message.id)).where(Message.negotiation_id == row.id))).scalar()
            or 0
        )
        await session.execute(delete(Message).where(Message.negotiation_id == row.id))
        await session.execute(delete(NegotiationEvent).where(NegotiationEvent.negotiation_id == row.id))
        await session.execute(delete(ReplyDraft).where(ReplyDraft.negotiation_id == row.id))
        # a prep requested before any record created this journal and keeps its id; forgotten, the prep reads the
        # application's current journal (by hh_negotiation_id) instead of whatever row gets this id next
        await session.execute(
            update(InterviewPrep).where(InterviewPrep.negotiation_id == row.id).values(negotiation_id=None)
        )
        await session.delete(row)
    app_row.hh_negotiation_id = None
    await session.flush()
    await audit(session, "negotiations", "Ручной журнал отклика удалён", application_id=app_row.id, messages=removed)
    return removed


async def delete_manual_message(session: AsyncSession, row: Negotiation, message_id: int) -> None:
    msg = await session.get(Message, message_id)
    if msg is None or msg.negotiation_id != row.id:
        raise ManualError("Сообщение не найдено", 404)
    if msg.source != "manual":
        raise ManualError("Удалять можно только записи, добавленные вручную")
    await session.delete(msg)
    row.messages_count = max(0, (row.messages_count or 0) - 1)
    await session.flush()
    await audit(session, "negotiations", "Удалена ручная запись сообщения", negotiation_id=row.hh_id)
