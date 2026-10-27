"""Saved searches: parameters model, hh query building, CRUD, preview."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import as_utc
from app.db.models import SavedSearch, Vacancy
from app.hh.client import HHClient
from app.hh.vacancy import VacancyData, normalize_vacancy
from app.services.audit import audit

MAX_DEPTH = 2000
PAGE_SIZE = 100
LIST_PARAMS = (
    "search_field",
    "experience",
    "employment_form",
    "work_format",
    "work_schedule_by_days",
    "working_hours",
    "label",
    "education",
)


class Ref(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str = ""

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return str(v).strip()


class SearchParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str = ""
    search_field: list[str] = Field(default_factory=list)
    experience: list[str] = Field(default_factory=list)
    employment_form: list[str] = Field(default_factory=list)
    work_format: list[str] = Field(default_factory=list)
    work_schedule_by_days: list[str] = Field(default_factory=list)
    salary: int | None = Field(None, ge=0)
    currency: str = "RUR"
    only_with_salary: bool = False
    label: list[str] = Field(default_factory=list)
    professional_role: list[Ref] = Field(default_factory=list)
    industry: list[Ref] = Field(default_factory=list)
    areas: list[Ref] = Field(default_factory=list)
    # metro stations (ids from /metro/{city}) and the length of the working day (HOURS_8 … from /dictionaries):
    # API-level filters (hh `metro`, `working_hours`), not shown in the search form yet
    metro: list[Ref] = Field(default_factory=list)
    working_hours: list[str] = Field(default_factory=list)
    excluded_text: str = ""
    education: list[str] = Field(default_factory=list)
    period_days: int | None = Field(None, ge=1, le=30)

    @field_validator("text", "excluded_text")
    @classmethod
    def _strip(cls, v: str) -> str:
        return (v or "").strip()

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        return (v or "RUR").strip().upper()[:3] or "RUR"

    @field_validator(*LIST_PARAMS)
    @classmethod
    def _lists(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for x in v:
            s = str(x).strip()
            if s and s not in out:
                out.append(s)
        return out

    def summary(self) -> str:
        parts: list[str] = []
        if self.text:
            parts.append(f"«{self.text}»")
        if self.areas:
            parts.append(", ".join(a.name or a.id for a in self.areas))
        if self.metro:
            parts.append("м. " + ", ".join(m.name or m.id for m in self.metro))
        if self.work_format:
            parts.append("/".join(self.work_format))
        if self.working_hours:
            parts.append("/".join(self.working_hours))
        if self.salary:
            parts.append(f"от {self.salary} {self.currency}")
        return " · ".join(parts) or "без фильтров"


def build_query(
    params: SearchParams,
    *,
    page: int,
    per_page: int = PAGE_SIZE,
    date_from: datetime | None = None,
    period: int | None = None,
) -> dict[str, Any]:
    q: dict[str, Any] = {
        "page": page,
        "per_page": per_page,
        "order_by": "publication_time",
        "responses_count_enabled": "true",
    }
    if params.text:
        q["text"] = params.text
    for key in LIST_PARAMS:
        values = getattr(params, key)
        if values:
            q[key] = list(values)
    if params.areas:
        q["area"] = [a.id for a in params.areas]
    if params.metro:
        q["metro"] = [m.id for m in params.metro]
    if params.professional_role:
        q["professional_role"] = [r.id for r in params.professional_role]
    if params.industry:
        q["industry"] = [r.id for r in params.industry]
    if params.salary:
        q["salary"] = params.salary
        q["currency"] = params.currency
    if params.only_with_salary:
        q["only_with_salary"] = "true"
    if params.excluded_text:
        q["excluded_text"] = params.excluded_text
    if date_from is not None:
        q["date_from"] = (as_utc(date_from) or date_from).strftime("%Y-%m-%dT%H:%M:%S%z")
    elif period:
        q["period"] = min(int(period), 30)
    return q


# ---- CRUD -------------------------------------------------------------------


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., min_length=1, max_length=200)
    source: Literal["search", "similar_to_resume"] = "search"
    params: SearchParams = Field(default_factory=SearchParams)
    interval_min: int = Field(30, ge=10, le=1440)
    enabled: bool = True
    # named selection-criteria set (None = base questionnaire)
    selection_profile_id: int | None = None
    # tracking only: new vacancies go to the bot as links (event search_new), nothing is evaluated
    watch_only: bool = False


def search_row(s: SavedSearch, profile_names: dict[int, str] | None = None) -> dict[str, Any]:
    params = SearchParams.model_validate_json(s.params_json or "{}")
    names = profile_names or {}

    def iso(dt: datetime | None) -> str | None:
        d = as_utc(dt)
        return d.isoformat(timespec="seconds") if d else None

    return {
        "id": s.id,
        "name": s.name,
        "source": s.source,
        "params": params.model_dump(),
        "summary": params.summary(),
        "interval_min": s.interval_min,
        "enabled": s.enabled,
        "watch_only": bool(s.watch_only),
        "selection_profile_id": s.selection_profile_id,
        "selection_profile_name": names.get(s.selection_profile_id) if s.selection_profile_id else None,
        "last_run_at": iso(s.last_run_at),
        "last_seen_published_at": iso(s.last_seen_published_at),
        "last_found": s.last_found,
        "last_new": s.last_new,
        "last_requests": s.last_requests,
        "last_error": s.last_error,
        "last_warning": s.last_warning,
        "created_at": iso(s.created_at),
    }


async def list_searches(session: AsyncSession) -> list[SavedSearch]:
    return list((await session.execute(select(SavedSearch).order_by(SavedSearch.id))).scalars().all())


async def create_search(session: AsyncSession, data: SearchInput) -> SavedSearch:
    row = SavedSearch(
        name=data.name.strip(),
        source=data.source,
        params_json=data.params.model_dump_json(),
        interval_min=data.interval_min,
        enabled=data.enabled,
        watch_only=data.watch_only,
        selection_profile_id=data.selection_profile_id,
    )
    session.add(row)
    await session.flush()
    await audit(session, "searches", f"Создан поиск «{row.name}»", search_id=row.id)
    return row


async def update_search(session: AsyncSession, row: SavedSearch, data: SearchInput) -> SavedSearch:
    old_params = row.params_json
    old_profile = row.selection_profile_id
    row.name = data.name.strip()
    row.source = data.source
    row.params_json = data.params.model_dump_json()
    row.interval_min = data.interval_min
    row.enabled = data.enabled
    row.watch_only = data.watch_only
    row.selection_profile_id = data.selection_profile_id
    if old_params != row.params_json:
        # parameters changed: next run starts from a fresh window
        row.last_seen_published_at = None
    moved = 0
    if old_profile != row.selection_profile_id:
        moved = await rebind_vacancies(session, row.id, old_profile, row.selection_profile_id)
    await session.flush()
    note = f" (вакансий переведено на новый набор критериев: {moved})" if moved else ""
    await audit(session, "searches", f"Изменён поиск «{row.name}»{note}", search_id=row.id)
    return row


async def rebind_vacancies(
    session: AsyncSession, search_id: int, old_profile: int | None, new_profile: int | None
) -> int:
    """The vacancies this search found follow its criteria set: when the search is re-bound, those still on the
    old set move to the new one (a vacancy found by several searches keeps the set it already follows if it
    differs). They are re-evaluated by «Переоценить всё», not here."""
    stmt = (
        update(Vacancy)
        .where(Vacancy.source_search_ids.like(f"%,{search_id},%"))
        .values(selection_profile_id=new_profile)
    )
    stmt = stmt.where(
        Vacancy.selection_profile_id.is_(None) if old_profile is None else Vacancy.selection_profile_id == old_profile
    )
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)


async def delete_search(session: AsyncSession, row: SavedSearch) -> None:
    await audit(session, "searches", f"Удалён поиск «{row.name}»", search_id=row.id)
    await session.delete(row)
    await session.flush()


def _names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(x.get("name")) for x in value if isinstance(x, dict) and x.get("name")]


def _place(item: dict[str, Any]) -> dict[str, Any]:
    """Address and metro of a search item — what the preview shows besides the short representation."""
    address = item.get("address")
    if not isinstance(address, dict):
        address = {}
    street = " ".join(str(address.get(k) or "") for k in ("street", "building")).strip()
    stations = [
        str(m.get("station_name"))
        for m in (address.get("metro_stations") or [])
        if isinstance(m, dict) and m.get("station_name")
    ]
    return {"address": street or None, "metro": stations}


async def preview(
    client: HHClient, params: SearchParams, *, path: str = "/vacancies", page: int = 0, per_page: int = 10
) -> dict[str, Any]:
    q = build_query(params, page=page, per_page=per_page, period=params.period_days or 7)
    data = await client.public(path, params=q)
    items: list[tuple[VacancyData, dict[str, Any]]] = []
    if isinstance(data, dict):
        for item in data.get("items") or []:
            if isinstance(item, dict) and item.get("id"):
                items.append((normalize_vacancy(item, full=False), item))
    return {
        "found": (data.get("found") if isinstance(data, dict) else None),
        "pages": (data.get("pages") if isinstance(data, dict) else None),
        "page": page,
        "items": [
            {
                "hh_id": v.hh_id,
                "name": v.name,
                "employer_name": v.employer_name,
                "area_name": v.area_name,
                "salary_from": v.salary_from,
                "salary_to": v.salary_to,
                "salary_currency": v.salary_currency,
                "published_at": v.published_at.isoformat() if v.published_at else None,
                "alternate_url": v.alternate_url,
                "responses_count": v.responses_count,
                "working_hours": _names(raw.get("working_hours")),
                "work_schedule_by_days": _names(raw.get("work_schedule_by_days")),
                **_place(raw),
            }
            for v, raw in items
        ],
    }
