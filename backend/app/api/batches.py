"""Exchange with Claude Code: packets, import, guidelines."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.db.models import ClaudeBatch, LetterRevisionRequest, Vacancy
from app.exchange.guidelines_md import GUIDELINES_MD, ensure_guidelines, guidelines_path
from app.exchange.importer import import_pending_results, import_result_text
from app.exchange.packets import (
    PacketError,
    batch_row,
    build_batches,
    cancel_batch,
    cancel_pending_batches,
    done_dir,
    pending_dir,
)
from app.runtime import Runtime
from app.services.profile import active_profile

router = APIRouter(prefix="/batches", tags=["batches"])


def _json_names(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.glob("*.json")) if folder.exists() else []


async def _summary(session: AsyncSession, runtime: Runtime) -> dict[str, Any]:
    settings = runtime.settings
    status_rows = await session.execute(
        select(Vacancy.pipeline_status, func.count(Vacancy.id)).group_by(Vacancy.pipeline_status)
    )
    counts: dict[str, int] = {str(status): int(n) for status, n in status_rows.all()}
    pending_batches = int(
        (await session.execute(select(func.count(ClaudeBatch.id)).where(ClaudeBatch.status == "pending"))).scalar() or 0
    )
    revisions = int(
        (
            await session.execute(
                select(func.count(LetterRevisionRequest.id)).where(LetterRevisionRequest.status == "queued")
            )
        ).scalar()
        or 0
    )
    profile = await active_profile(session)
    return {
        "waiting_for_packet": int(counts.get("prescored", 0)) + revisions,
        "prescored": int(counts.get("prescored", 0)),
        "revision_requests": revisions,
        "awaiting_claude": int(counts.get("awaiting_claude", 0)),
        "pending_batches": pending_batches,
        "pending_files": _json_names(pending_dir(settings)),
        "done_files": _json_names(done_dir(settings)),
        "exchange_dir": str(settings.exchange_dir),
        "profile_version": profile.version if profile else None,
        "guidelines_exists": guidelines_path(settings).exists(),
        "batch_size": settings.batch_size,
        "auto_create": settings.batch_auto_create,
        "command": "/hh-batch",
    }


@router.get("/summary")
async def summary(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    return await _summary(session, runtime)


@router.get("")
async def list_batches(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = 50,
) -> dict[str, Any]:
    stmt = select(ClaudeBatch).order_by(desc(ClaudeBatch.id)).limit(min(limit, 500))
    rows = (await session.execute(stmt)).scalars().all()
    return {"items": [batch_row(b) for b in rows], "summary": await _summary(session, runtime)}


class CreateRequest(BaseModel):
    vacancy_ids: list[int] | None = None
    include_revisions: bool = True


@router.post("/create")
async def create(
    body: CreateRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    try:
        created = await build_batches(
            session, runtime.settings, vacancy_ids=body.vacancy_ids, include_revisions=body.include_revisions
        )
    except PacketError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return {"created": [batch_row(b) for b in created], "summary": await _summary(session, runtime)}


@router.post("/import")
async def import_now(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    outcomes = await import_pending_results(session, runtime.settings, require_stable=False)
    await session.commit()
    return {"outcomes": [o.to_dict() for o in outcomes], "summary": await _summary(session, runtime)}


class ImportTextRequest(BaseModel):
    text: str


@router.post("/import-text")
async def import_text(
    body: ImportTextRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    outcome = await import_result_text(session, runtime.settings, body.text)
    await session.commit()
    if outcome.status == "rejected" and outcome.batch_id is None:
        raise HTTPException(status_code=400, detail="; ".join(outcome.errors) or "не удалось импортировать")
    return {"outcome": outcome.to_dict(), "summary": await _summary(session, runtime)}


@router.get("/guidelines", response_class=PlainTextResponse)
async def get_guidelines(runtime: Annotated[Runtime, Depends(get_runtime)]) -> str:
    path = guidelines_path(runtime.settings)
    if not path.exists():
        ensure_guidelines(runtime.settings)
    return path.read_text(encoding="utf-8") if path.exists() else GUIDELINES_MD


@router.get("/{batch_pk}")
async def get_batch(batch_pk: int, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    row = await session.get(ClaudeBatch, batch_pk)
    if row is None:
        raise HTTPException(status_code=404, detail="Пакет не найден")
    data = batch_row(row)
    path = Path(row.packet_path)
    if path.exists():
        try:
            packet = json.loads(path.read_text(encoding="utf-8"))
            data["tasks"] = [
                {
                    "type": t.get("type"),
                    "vacancy_id": (t.get("vacancy") or {}).get("id"),
                    "name": (t.get("vacancy") or {}).get("name"),
                }
                for t in packet.get("tasks", [])
            ]
        except (OSError, ValueError):
            data["tasks"] = []
    return data


@router.post("/cancel-pending")
async def cancel_pending(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Cancels every pending packet: vacancies go back to `prescored`, packet files are removed."""
    if runtime.jobs.is_running("reprocess"):
        raise HTTPException(status_code=409, detail="Идёт переоценка — дождитесь её окончания")
    before = int(
        (
            await session.execute(select(func.count(Vacancy.id)).where(Vacancy.pipeline_status == "awaiting_claude"))
        ).scalar()
        or 0
    )
    cancelled = await cancel_pending_batches(session, runtime.settings)
    await session.commit()
    summary = await _summary(session, runtime)
    return {"ok": True, "cancelled": cancelled, "returned": before - summary["awaiting_claude"], "summary": summary}


@router.delete("/{batch_pk}")
async def cancel(
    batch_pk: int,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    row = await session.get(ClaudeBatch, batch_pk)
    if row is None:
        raise HTTPException(status_code=404, detail="Пакет не найден")
    try:
        returned = await cancel_batch(session, runtime.settings, row)
    except PacketError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return {"ok": True, "returned": returned, "summary": await _summary(session, runtime)}
