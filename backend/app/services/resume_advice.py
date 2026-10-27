"""Resume advice through the packet exchange (task `resume_advice`).

The analytics know what the good vacancies of a segment ask for and what Claude kept listing as gaps; this task
turns that into concrete edits of the resume text: which sections to rewrite and how, which skills to add (only if
the candidate really has them — Claude asks instead of inventing), what to soften or drop. The demand snapshot is
frozen at request time (`demand_json`), so the packet says exactly what the user saw when asking.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc
from app.db.models import ResumeAdvice
from app.services import analytics as an
from app.services.audit import audit

STATUS_LABELS = {
    "queued": "в очереди на пакет",
    "sent_in_batch": "в пакете для Claude",
    "done": "готовы",
    "cancelled": "не выполнены",
}
SAMPLE_VACANCIES = 6


def _loads(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def advice_row(a: ResumeAdvice) -> dict[str, Any]:
    created, updated = as_utc(a.created_at), as_utc(a.updated_at)
    return {
        "id": a.id,
        "segment_by": a.segment_by,
        "segment_key": a.segment_key,
        "segment_label": a.segment_label,
        "user_notes": a.user_notes,
        "demand": _loads(a.demand_json),
        "status": a.status,
        "status_label": STATUS_LABELS.get(a.status, a.status),
        "batch_id": a.batch_id,
        "result": _loads(a.result_json),
        "error": a.error,
        "created_at": created.isoformat(timespec="seconds") if created else None,
        "updated_at": updated.isoformat(timespec="seconds") if updated else None,
    }


async def list_advices(session: AsyncSession, limit: int = 10) -> list[dict[str, Any]]:
    rows = (await session.execute(select(ResumeAdvice).order_by(desc(ResumeAdvice.id)).limit(limit))).scalars().all()
    return [advice_row(a) for a in rows]


def demand_snapshot(base: an.Base, vs: list[an.V]) -> dict[str, Any]:
    """What the segment asks for, as the packet will carry it: demanded-but-missing skills, Claude's gaps, red
    flags, the top skills, and a few of the best-scored vacancies as examples."""
    skills = an.skills_report(vs, base)
    good = sorted(
        (v for v in vs if v.id in base.evals and base.evals[v.id].score >= an.GOOD_SCORE),
        key=lambda v: -base.evals[v.id].score,
    )
    return {
        "found": len(vs),
        "evaluated": skills["evaluated"],
        "good": len(good),
        "top_skills": [
            {"name": s["name"], "count": s["count"], "in_resume": s["in_resume"]} for s in skills["skills"][:20]
        ],
        "missing": [{"name": s["name"], "count": s["count"]} for s in skills["missing"]],
        "gap_skills": skills["gap_skills"][:12],
        "gaps": skills["gaps"][:10],
        "red_flags": skills["red_flags"][:6],
        "sample_vacancies": [
            {
                "name": v.name,
                "employer": v.employer_name,
                "url": v.url,
                "score": base.evals[v.id].score,
                "key_skills": v.key_skills[:12],
                "summary": base.evals[v.id].summary,
            }
            for v in good[:SAMPLE_VACANCIES]
        ],
    }


async def request_resume_advice(
    session: AsyncSession,
    settings: Settings,
    *,
    by: str | None,
    key: str | None,
    notes: str | None,
) -> tuple[ResumeAdvice, str | None]:
    """Freezes the demand of the segment, queues a `resume_advice` task, tries to build a packet right away."""
    from app.exchange.packets import PacketError, build_batches

    base = await an.load_base(session, settings, scope="passed")
    vs = an.select_segment(base, by, key)
    label = "все вакансии"
    if by and key:
        labels = {k: lbl for v in vs for k, lbl in an.segment_keys(v, by, base)}
        label = labels.get(key, key)
    for old in (await session.execute(select(ResumeAdvice).where(ResumeAdvice.status == "queued"))).scalars():
        old.status = "cancelled"
        old.error = "заменён новым запросом"
    row = ResumeAdvice(
        segment_by=by if by and key else None,
        segment_key=key if by and key else None,
        segment_label=label[:200],
        user_notes=(notes or "").strip() or None,
        demand_json=json.dumps(demand_snapshot(base, vs), ensure_ascii=False),
    )
    session.add(row)
    await session.flush()
    await audit(session, "batches", f"Запрошены правки резюме через Claude: {label}")
    try:
        created = await build_batches(session, settings, vacancy_ids=[], include_revisions=True)
    except PacketError:
        return row, None
    return row, (created[0].batch_id if created else None)
