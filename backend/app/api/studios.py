"""Studios: the list, finding them (job `studios_find`), their contacts (job `studio_contacts`), letters through
Claude, marking a letter sent (services/studios.py)."""

from __future__ import annotations

from typing import Annotated, Any, Literal, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.db.models import Studio
from app.runtime import Runtime
from app.services import studios as svc
from app.services.jobs import JobBusy

router = APIRouter(prefix="/studios", tags=["studios"])

Group = Literal["work", "ready", "drafted", "sent", "replied", "closed", "all"]
Source = Literal["hh_search", "hh_vacancies", "list", "channel"]
Kind = Literal["dev", "design", "mixed", "unknown"]
LetterKind = Literal["application", "offer"]


async def _get(session: AsyncSession, studio_id: int) -> Studio:
    row = await session.get(Studio, studio_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Студия не найдена")
    return row


def _raise(exc: svc.StudioError) -> NoReturn:
    raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


@router.get("")
async def list_studios(
    session: Annotated[AsyncSession, Depends(get_session)],
    runtime: Annotated[Runtime, Depends(get_runtime)],
    group: Group = "work",
    source: Source | None = None,
    kind: Kind | None = None,
    q: str | None = Query(None, max_length=200),
    with_contacts: bool = False,
    sort: Literal["fit", "name", "recent"] = "fit",
    page: int = Query(0, ge=0, le=10_000),
    per_page: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    data = await svc.list_studios(
        session,
        group=group,
        source=source,
        kind=kind,
        q=q,
        with_contacts=with_contacts,
        sort=sort,
        page=page,
        per_page=per_page,
    )
    data["sent_today"] = await svc.sent_today(session, runtime.settings)
    data["daily_limit"] = runtime.settings.studios_daily_limit
    return data


@router.get("/summary")
async def summary(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    """Menu badge: letters ready to send and fresh replies."""
    counts = await svc.status_counts(session)
    return {"drafted": counts["by_status"].get("drafted", 0), "replied": counts["by_status"].get("replied", 0)}


class StudioSettingsUpdate(BaseModel):
    keywords: list[str] | None = Field(None, max_length=30)
    only_with_vacancies: bool | None = None
    signature: str | None = Field(None, max_length=1000)
    offer_notes: str | None = Field(None, max_length=3000)


@router.get("/settings")
async def get_settings(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    return await svc.studio_settings(session)


@router.put("/settings")
async def put_settings(
    body: StudioSettingsUpdate, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    values = body.model_dump(exclude_none=True)
    if "keywords" in values:
        values["keywords"] = [k.strip()[:100] for k in values["keywords"] if k.strip()]
    data = await svc.save_studio_settings(session, values)
    await session.commit()
    return data


FindSource = Literal["hh_search", "hh_vacancies", "channels"]


def _every_source() -> list[FindSource]:
    return ["hh_search", "hh_vacancies", "channels"]


class FindRequest(BaseModel):
    sources: list[FindSource] = Field(default_factory=_every_source, min_length=1, max_length=3)


@router.post("/find")
async def find(body: FindRequest, runtime: Annotated[Runtime, Depends(get_runtime)]) -> dict[str, Any]:
    """Job `studios_find` in the background; its result is in the «Журнал» tab and in /api/jobs."""
    from app.services.studio_sources import JOB, find_studios

    sources = tuple(dict.fromkeys(body.sources))
    try:
        run_id = await runtime.jobs.start(JOB, lambda: find_studios(runtime, sources))
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_run_id": run_id, "sources": list(sources)}


class ContactsRequest(BaseModel):
    ids: list[int] | None = Field(None, max_length=200)


@router.post("/contacts")
async def contacts(body: ContactsRequest, runtime: Annotated[Runtime, Depends(get_runtime)]) -> dict[str, Any]:
    """Job `studio_contacts`: the given studios, else the ones never checked (STUDIOS_CONTACTS_PER_RUN at most)."""
    from app.services.studio_contacts import JOB, check_contacts

    ids = list(dict.fromkeys(body.ids or [])) or None
    try:
        run_id = await runtime.jobs.start(JOB, lambda: check_contacts(runtime, ids))
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"job_run_id": run_id}


class ImportRequest(BaseModel):
    text: str = Field(..., min_length=3, max_length=100_000)
    letter_kind: LetterKind = "application"


@router.post("/import")
async def import_list(body: ImportRequest, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    """The user's own list: a line per studio — «site», «name; site» or «name; site; e-mail»."""
    try:
        result = await svc.import_list(session, body.text, body.letter_kind)
    except svc.StudioError as exc:
        _raise(exc)
    await session.commit()
    return result


class LettersRequest(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=100)
    notes: str | None = Field(None, max_length=2000)


@router.post("/letters")
async def request_letters(
    body: LettersRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """A `studio_letter` task per studio in the next packet for /hh-batch (built right away when possible)."""
    queued, skipped, batch_id = await svc.request_letters(session, runtime.settings, body.ids, body.notes)
    await session.commit()
    return {"queued": queued, "skipped": skipped, "batch_id": batch_id}


@router.get("/{studio_id}")
async def detail(studio_id: int, session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    return await svc.studio_detail(session, await _get(session, studio_id))


class StudioUpdate(BaseModel):
    emails: list[str] | None = Field(None, max_length=5)
    telegram: str | None = Field(None, max_length=100)
    notes: str | None = Field(None, max_length=2000)
    letter_kind: LetterKind | None = None
    site_url: str | None = Field(None, max_length=500)


@router.put("/{studio_id}")
async def update(
    studio_id: int, body: StudioUpdate, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    row = await _get(session, studio_id)
    try:
        await svc.update_studio(
            session,
            row,
            emails=body.emails,
            telegram=body.telegram,
            notes=body.notes,
            letter_kind=body.letter_kind,
            site_url=body.site_url,
        )
    except svc.StudioError as exc:
        _raise(exc)
    await session.commit()
    return await svc.studio_detail(session, row)


class LetterEdit(BaseModel):
    subject: str = Field(..., min_length=1, max_length=200)
    text: str = Field(..., min_length=20, max_length=10_000)
    # the draft edited (business | simple); none — the current one's style
    style: Literal["business", "simple"] | None = None


@router.put("/{studio_id}/letter")
async def edit_letter(
    studio_id: int, body: LetterEdit, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    row = await _get(session, studio_id)
    try:
        await svc.save_letter(session, row, body.subject, body.text, body.style)
    except svc.StudioError as exc:
        _raise(exc)
    await session.commit()
    return await svc.studio_detail(session, row)


class SentRequest(BaseModel):
    to: str = Field("", max_length=200)
    via: Literal["email", "telegram", "form", "other"] = "email"
    # the draft that was sent (it becomes the current one); none — the current one
    letter_id: int | None = Field(None, ge=1)


@router.post("/{studio_id}/sent")
async def sent(
    studio_id: int,
    body: SentRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """The user sent the letter themselves; returns today's count against STUDIOS_DAILY_LIMIT."""
    row = await _get(session, studio_id)
    try:
        counts = await svc.mark_sent(session, runtime.settings, row, to=body.to, via=body.via, letter_id=body.letter_id)
    except svc.StudioError as exc:
        _raise(exc)
    await session.commit()
    data = await svc.studio_detail(session, row)
    data.update(counts)
    return data


@router.post("/{studio_id}/{action}")
async def action(
    studio_id: int,
    action: Literal["skip", "hide", "restore", "replied", "declined"],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    row = await _get(session, studio_id)
    try:
        await svc.set_status(session, row, action)
    except svc.StudioError as exc:
        _raise(exc)
    await session.commit()
    return await svc.studio_detail(session, row)
