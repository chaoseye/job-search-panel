"""Event feed and Telegram settings/actions."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.runtime import Runtime
from app.services.notify import delivery_summary, list_notifications
from app.services.telegram import TelegramError, detect_chat_id, flush_now, send_test, telegram_status

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
async def feed(
    session: Annotated[AsyncSession, Depends(get_session)],
    channel: str | None = Query(default="ui", description="ui | telegram | (empty = all)"),
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    return await list_notifications(session, channel=channel or None, limit=limit)


@router.get("/summary")
async def summary(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    """Telegram messages not delivered during the last week (the dashboard warning)."""
    return await delivery_summary(session)


@router.get("/telegram")
async def telegram(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    status = await telegram_status(session, runtime.settings, runtime.http)
    await session.commit()  # the first call may cache the bot identity
    return status


class LettersRequest(BaseModel):
    enabled: bool
    min_score: int = Field(50, ge=0, le=100)


@router.put("/telegram/letters")
async def letters(
    body: LettersRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """The switch of «vacancy with its cover letter → the bot» and its score threshold."""
    from app.services.tg_letters import letters_config, save_letters_config

    await save_letters_config(session, enabled=body.enabled, min_score=body.min_score)
    await session.commit()
    return await letters_config(session, runtime.settings)


class StopWordsRequest(BaseModel):
    words: list[str] = Field(default_factory=list, max_length=100)


@router.put("/telegram/stop-words")
async def put_stop_words(
    body: StopWordsRequest, session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    """Vacancies whose title has one of these words do not go to the bot (they stay in the panel and the feed)."""
    from app.services.tg_filter import save_stop_words

    words = await save_stop_words(session, body.words)
    await session.commit()
    return {"stop_words": words}


@router.post("/telegram/detect-chat")
async def detect(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    try:
        result = await detect_chat_id(session, runtime.settings, runtime.http)
    except TelegramError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return result


@router.post("/telegram/test")
async def test_message(
    runtime: Annotated[Runtime, Depends(get_runtime)], session: Annotated[AsyncSession, Depends(get_session)]
) -> dict[str, Any]:
    try:
        result = await send_test(session, runtime.settings, runtime.http)
    except TelegramError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return result


@router.post("/telegram/flush")
async def flush(runtime: Annotated[Runtime, Depends(get_runtime)]) -> dict[str, Any]:
    return await flush_now(runtime)


@router.post("/resume-bump")
async def resume_bump_now(runtime: Annotated[Runtime, Depends(get_runtime)]) -> dict[str, Any]:
    """«Напомнить сейчас»: the same reminder the schedule sends, queued for immediate delivery."""
    from app.services.resume_bump import bump_reminder_tick

    result = await bump_reminder_tick(runtime, force=True)
    if result.get("ok") and result.get("telegram"):
        await flush_now(runtime)
    return result
