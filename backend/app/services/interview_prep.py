"""Interview preparation through the packet exchange (task `interview_prep`).

When an invitation is recorded — by hand, from an hh e-mail or by the sync — the panel queues a task for Claude:
the vacancy, the letter that was sent, the journal thread and the user's notes go into the next packet; Claude
returns likely questions with answers grounded in the profile, questions to ask the employer, risks to address
and what to refresh before the call. The result is shown in the application panel («Подготовка к собеседованию»).
Nothing is sent anywhere — it is a study sheet for the user.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc
from app.db.models import Application, InterviewPrep
from app.services.audit import audit
from app.services.manual_negotiation import ensure_manual

STATUS_LABELS = {
    "queued": "в очереди на пакет",
    "sent_in_batch": "в пакете для Claude",
    "done": "готова",
    "cancelled": "не выполнена",
}


class PrepError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def prep_row(p: InterviewPrep) -> dict[str, Any]:
    try:
        result = json.loads(p.result_json) if p.result_json else None
    except ValueError:
        result = None
    created, updated = as_utc(p.created_at), as_utc(p.updated_at)
    return {
        "id": p.id,
        "application_id": p.application_id,
        "status": p.status,
        "status_label": STATUS_LABELS.get(p.status, p.status),
        "auto": p.auto,
        "user_notes": p.user_notes,
        "batch_id": p.batch_id,
        "result": result,
        "error": p.error,
        "created_at": created.isoformat(timespec="seconds") if created else None,
        "updated_at": updated.isoformat(timespec="seconds") if updated else None,
    }


async def list_preps(session: AsyncSession, application_id: int, limit: int = 5) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(InterviewPrep)
                .where(InterviewPrep.application_id == application_id)
                .order_by(desc(InterviewPrep.id))
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [prep_row(p) for p in rows]


async def request_interview_prep(
    session: AsyncSession, settings: Settings, app_row: Application, *, notes: str | None, auto: bool = False
) -> tuple[InterviewPrep | None, str | None]:
    """Queues an `interview_prep` task and tries to build a packet right away. Returns (prep, packet id).

    `auto=True` (an invitation just recorded) creates nothing when a prep for the application already exists."""
    from app.exchange.packets import PacketError, build_batches

    if app_row.status != "sent":
        raise PrepError("Подготовка к собеседованию — только для отправленного отклика", 409)
    existing = (
        (
            await session.execute(
                select(InterviewPrep).where(
                    InterviewPrep.application_id == app_row.id, InterviewPrep.status != "cancelled"
                )
            )
        )
        .scalars()
        .all()
    )
    if auto and existing:
        return None, None
    for old in existing:
        if old.status == "queued":
            old.status = "cancelled"
            old.error = "заменена новым запросом"
    negotiation = await ensure_manual(session, app_row) if app_row.hh_negotiation_id is None else None
    prep = InterviewPrep(
        application_id=app_row.id,
        vacancy_id=app_row.vacancy_id,
        negotiation_id=negotiation.id if negotiation is not None else None,
        user_notes=(notes or "").strip() or None,
        auto=auto,
    )
    session.add(prep)
    await session.flush()
    await audit(
        session,
        "batches",
        "Запрошена подготовка к собеседованию через Claude" + (" (автоматически по приглашению)" if auto else ""),
        application_id=app_row.id,
    )
    try:
        created = await build_batches(session, settings, vacancy_ids=[], include_revisions=True)
    except PacketError:
        return prep, None
    return prep, (created[0].batch_id if created else None)
