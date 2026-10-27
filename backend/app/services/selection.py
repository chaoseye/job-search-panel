"""Named selection-criteria sets: several questionnaires that differ only in vacancy
selection criteria; the candidate profile and letters stay shared. Each saved search points to one set
(None = base questionnaire); a vacancy remembers the set of the search that found it."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import as_utc
from app.db.models import Resume, SavedSearch, SelectionProfile, Vacancy
from app.services.audit import audit
from app.services.preferences import Preferences, apply_criteria, criteria_of, get_preferences


class SelectionError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        self.status = status
        super().__init__(message)


def _loads(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def list_profiles(session: AsyncSession) -> list[SelectionProfile]:
    return list((await session.execute(select(SelectionProfile).order_by(SelectionProfile.id))).scalars().all())


async def get_profile(session: AsyncSession, pk: int) -> SelectionProfile:
    row = await session.get(SelectionProfile, pk)
    if row is None:
        raise SelectionError("Набор критериев не найден", 404)
    return row


async def create_profile(
    session: AsyncSession, name: str, criteria: dict[str, Any] | None = None, *, copy_from: int | None = None
) -> SelectionProfile:
    name = name.strip()
    if not name:
        raise SelectionError("Укажите название набора критериев")
    if copy_from is not None:
        source = await get_profile(session, copy_from)
        data = _loads(source.criteria_json)
    elif criteria is not None:
        data = criteria
    else:
        data = criteria_of(await get_preferences(session))
    base = await get_preferences(session)
    effective = apply_criteria(base, data)  # validates
    row = SelectionProfile(name=name[:100], criteria_json=json.dumps(criteria_of(effective), ensure_ascii=False))
    session.add(row)
    await session.flush()
    await audit(session, "profile", f"Создан набор критериев «{row.name}»", selection_profile_id=row.id)
    return row


async def update_profile(
    session: AsyncSession,
    row: SelectionProfile,
    *,
    name: str | None = None,
    criteria: dict[str, Any] | None = None,
    resume_id: int | None = None,
    clear_resume: bool = False,
) -> SelectionProfile:
    if name is not None:
        if not name.strip():
            raise SelectionError("Название не может быть пустым")
        row.name = name.strip()[:100]
    if clear_resume:
        row.resume_id = None
    elif resume_id is not None:
        resume = await session.get(Resume, resume_id)
        if resume is None:
            raise SelectionError("Резюме не найдено", 404)
        row.resume_id = resume.id
    if criteria is not None:
        # partial update: unspecified fields keep the set's own values, not the questionnaire's
        base = await get_preferences(session)
        effective = apply_criteria(base, {**_loads(row.criteria_json), **criteria})
        row.criteria_json = json.dumps(criteria_of(effective), ensure_ascii=False)
    await session.flush()
    await audit(session, "profile", f"Изменён набор критериев «{row.name}»", selection_profile_id=row.id)
    return row


async def delete_profile(session: AsyncSession, row: SelectionProfile) -> dict[str, int]:
    """Deletes the set; searches and vacancies bound to it fall back to the base questionnaire."""
    searches = (await session.execute(select(SavedSearch).where(SavedSearch.selection_profile_id == row.id))).scalars()
    n_s = 0
    for s in searches:
        s.selection_profile_id = None
        n_s += 1
    vacancies = (await session.execute(select(Vacancy).where(Vacancy.selection_profile_id == row.id))).scalars()
    n_v = 0
    for v in vacancies:
        v.selection_profile_id = None
        n_v += 1
    await audit(session, "profile", f"Удалён набор критериев «{row.name}»", searches=n_s, vacancies=n_v)
    await session.delete(row)
    await session.flush()
    return {"searches": n_s, "vacancies": n_v}


async def effective_preferences(session: AsyncSession, profile_id: int | None) -> Preferences:
    """Questionnaire with the criteria of the given set applied (None → base questionnaire as is)."""
    base = await get_preferences(session)
    if profile_id is None:
        return base
    row = await session.get(SelectionProfile, profile_id)
    if row is None:
        return base
    return apply_criteria(base, _loads(row.criteria_json))


async def profile_names(session: AsyncSession) -> dict[int, str]:
    rows = (await session.execute(select(SelectionProfile.id, SelectionProfile.name))).all()
    return {int(i): str(n) for i, n in rows}


async def profile_row(session: AsyncSession, row: SelectionProfile) -> dict[str, Any]:
    searches = int(
        (
            await session.execute(select(func.count(SavedSearch.id)).where(SavedSearch.selection_profile_id == row.id))
        ).scalar()
        or 0
    )
    vacancies = int(
        (await session.execute(select(func.count(Vacancy.id)).where(Vacancy.selection_profile_id == row.id))).scalar()
        or 0
    )
    base = await get_preferences(session)
    effective = apply_criteria(base, _loads(row.criteria_json))
    updated = as_utc(row.updated_at)
    resume = await session.get(Resume, row.resume_id) if row.resume_id else None
    return {
        "id": row.id,
        "name": row.name,
        "criteria": criteria_of(effective),
        "searches": searches,
        "vacancies": vacancies,
        "resume_id": resume.id if resume else None,
        "resume_name": (resume.file_name or resume.title) if resume else None,
        "profile_file": set_profile_file(row.id) if resume else None,
        "updated_at": updated.isoformat(timespec="seconds") if updated else None,
    }


def set_profile_file(profile_id: int) -> str:
    return f"profile_set{profile_id}.md"


async def set_resumes(session: AsyncSession) -> dict[int, Resume]:
    """Sets with their own resume: {set id: Resume}."""
    rows = (await session.execute(select(SelectionProfile).where(SelectionProfile.resume_id.isnot(None)))).scalars()
    out: dict[int, Resume] = {}
    for row in rows:
        resume = await session.get(Resume, row.resume_id)
        if resume is not None:
            out[row.id] = resume
    return out
