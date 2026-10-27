"""Public Telegram channels with vacancies: channels CRUD, sync (job `channel_sync`), the post feed."""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.db.models import TelegramChannel
from app.runtime import Runtime
from app.services import channels as svc
from app.services.channels import ChannelError
from app.services.jobs import JobBusy, job_label
from app.services.state import auth_public_state, get_hh_auth

router = APIRouter(prefix="/channels", tags=["channels"])


class ChannelCreate(BaseModel):
    input: str = Field(..., min_length=2, max_length=200, description="@канал или ссылка t.me")
    mode: str = "vacancies"
    keywords: str | None = None
    notify: bool = True


class ChannelsBulkCreate(BaseModel):
    input: str = Field(..., min_length=2, max_length=20000, description="каналы через пробел или с новой строки")
    mode: str = "vacancies"
    keywords: str | None = None
    notify: bool = True


class ChannelUpdate(BaseModel):
    enabled: bool | None = None
    mode: str | None = None
    keywords: str | None = None
    notify: bool | None = None


class MarkSeenRequest(BaseModel):
    ids: list[int] | None = None
    channel_id: int | None = None


class ToPanelRequest(BaseModel):
    selection_profile_id: int | None = None
    create_packet: bool = True


def _bad(exc: ChannelError, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=str(exc))


async def _run_sync(runtime: Runtime, channel_id: int | None = None) -> dict[str, Any]:
    try:
        await runtime.jobs.start("channel_sync", lambda: svc.sync_channels(runtime, channel_id=channel_id))
    except JobBusy as exc:
        raise HTTPException(status_code=409, detail="Проверка каналов уже выполняется") from exc
    await runtime.jobs.wait("channel_sync")
    return {"ok": True}


# ---- static routes first (before /{channel_id}) ------------------------------------------------


@router.get("/summary")
async def channels_summary(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    return await svc.summary(session, runtime.settings)


@router.post("/sync")
async def sync_all(runtime: Annotated[Runtime, Depends(get_runtime)]) -> dict[str, Any]:
    """Reads every enabled channel right now (waits for the job) and returns the channel list."""
    await _run_sync(runtime)
    async with runtime.session_factory() as session:
        return {"items": await svc.list_channels(session)}


@router.post("/reflag")
async def reflag(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
    channel_id: int | None = None,
) -> dict[str, int]:
    """Recomputes the vacancy flag of stored posts (after a dictionary change in services/channels.py)."""
    result = await svc.reflag_posts(session, runtime.settings, channel_id=channel_id)
    await session.commit()
    return result


@router.get("/posts")
async def posts(
    session: Annotated[AsyncSession, Depends(get_session)],
    channel_id: int | None = None,
    only_new: bool = False,
    only_vacancies: bool = True,
    include_hidden: bool = False,
    q: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    return await svc.list_posts(
        session,
        channel_id=channel_id,
        only_new=only_new,
        only_vacancies=only_vacancies,
        include_hidden=include_hidden,
        q=q,
        page=page,
        per_page=per_page,
    )


@router.post("/posts/mark-seen")
async def posts_mark_seen(
    body: MarkSeenRequest, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    count = await svc.mark_seen(session, ids=body.ids, channel_id=body.channel_id)
    await session.commit()
    return {"marked": count}


@router.post("/posts/{post_id}/to-panel")
async def post_to_panel(
    post_id: int,
    body: ToPanelRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Adds the hh.ru vacancies linked from the post to the pipeline (same as «Добавить вакансии по ссылкам»)."""
    from app.services.manual_add import add_vacancies

    try:
        post = await svc.get_post(session, post_id)
    except ChannelError as exc:
        raise _bad(exc, 404) from exc
    hh_urls = svc.hh_vacancy_urls([str(u) for u in json.loads(post.links_json or "[]")], post.text)
    if not hh_urls:
        raise HTTPException(status_code=400, detail="В посте нет ссылки на вакансию hh.ru")
    if not auth_public_state(await get_hh_auth(session))["logged_in"]:
        raise HTTPException(status_code=400, detail="Сначала войдите в hh.ru")
    running = runtime.jobs.running_exclusive()
    if running is not None:
        raise HTTPException(
            status_code=409, detail=f"Сейчас выполняется задача «{job_label(running)}» — попробуйте через минуту"
        )
    outcome = await add_vacancies(
        session,
        runtime.settings,
        runtime.hh_client(),
        " ".join(hh_urls),
        profile_id=body.selection_profile_id,
        create_packet=body.create_packet,
    )
    vacancy = await svc.attach_vacancy(session, post)
    if post.status == "new":
        await svc.set_post_status(session, post, "seen")
    await session.commit()
    channel = await session.get(TelegramChannel, post.channel_id)
    return {
        "added": outcome.added,
        "errors": outcome.errors,
        "batch_ids": outcome.batch_ids,
        "post": svc.post_row(post, channel, vacancy=vacancy),
    }


@router.post("/posts/{post_id}/{action}")
async def post_action(
    post_id: int,
    action: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """seen | hide | restore (hidden → seen)."""
    status = {"seen": "seen", "hide": "hidden", "restore": "seen"}.get(action)
    if status is None:
        raise HTTPException(status_code=404, detail="неизвестное действие")
    try:
        post = await svc.get_post(session, post_id)
        await svc.set_post_status(session, post, status)
    except ChannelError as exc:
        raise _bad(exc, 404) from exc
    await session.commit()
    channel = await session.get(TelegramChannel, post.channel_id)
    return svc.post_row(post, channel)


# ---- channels ---------------------------------------------------------------------------------


@router.get("")
async def list_channels(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    return {
        "items": await svc.list_channels(session),
        "interval_min": runtime.settings.channel_sync_min,
        "languages": sorted(svc.allowed_languages(runtime.settings)),
        "running": runtime.jobs.is_running("channel_sync"),
    }


@router.post("")
async def create_channel(
    body: ChannelCreate,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Adds a public channel and reads its last posts right away (they are shown, not «new»)."""
    if runtime.jobs.is_running("channel_sync"):
        raise HTTPException(status_code=409, detail="Проверка каналов уже выполняется — попробуйте через минуту")
    try:
        channel = await svc.create_channel(
            session,
            runtime.settings,
            runtime.http,
            body.input,
            mode=body.mode,
            keywords=body.keywords,
            notify=body.notify,
        )
    except ChannelError as exc:
        raise _bad(exc) from exc
    await session.commit()
    return svc.channel_row(channel)


@router.post("/bulk")
async def create_channels(
    body: ChannelsBulkCreate,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Adds several channels at once; a folder link (t.me/addlist/…) is explained, not expanded."""
    if runtime.jobs.is_running("channel_sync"):
        raise HTTPException(status_code=409, detail="Проверка каналов уже выполняется — попробуйте через минуту")
    result = await svc.add_channels(
        session,
        runtime.settings,
        runtime.http,
        body.input,
        mode=body.mode,
        keywords=body.keywords,
        notify=body.notify,
    )
    await session.commit()
    return result


@router.put("/{channel_id}")
async def update_channel(
    channel_id: int,
    body: ChannelUpdate,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    try:
        channel = await svc.get_channel(session, channel_id)
        await svc.update_channel(session, runtime.settings, channel, **body.model_dump())
    except ChannelError as exc:
        raise _bad(exc, 404 if "не найден" in str(exc) else 400) from exc
    await session.commit()
    rows = {r["id"]: r for r in await svc.list_channels(session)}
    return rows[channel.id]


@router.delete("/{channel_id}")
async def delete_channel(
    channel_id: int,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    if runtime.jobs.is_running("channel_sync"):
        raise HTTPException(status_code=409, detail="Проверка каналов уже выполняется — попробуйте через минуту")
    try:
        channel = await svc.get_channel(session, channel_id)
    except ChannelError as exc:
        raise _bad(exc, 404) from exc
    await svc.delete_channel(session, channel)
    await session.commit()
    return {"ok": True}


@router.post("/{channel_id}/sync")
async def sync_one(channel_id: int, runtime: Annotated[Runtime, Depends(get_runtime)]) -> dict[str, Any]:
    """Reads one channel right now (waits for the job) and returns its row."""
    async with runtime.session_factory() as session:
        try:
            channel = await svc.get_channel(session, channel_id)
        except ChannelError as exc:
            raise _bad(exc, 404) from exc
        if not channel.enabled:
            raise HTTPException(status_code=409, detail="Канал отключён — включите его, чтобы проверять")
    await _run_sync(runtime, channel_id)
    async with runtime.session_factory() as session:
        rows = {r["id"]: r for r in await svc.list_channels(session)}
    return rows[channel_id]
