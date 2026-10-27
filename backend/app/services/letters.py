"""Cover letters: versions, edits, template fallback, pre-send checks."""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import as_utc
from app.db.models import CoverLetter, LetterRevisionRequest, Resume, Vacancy
from app.exchange.schemas import CLARIFY_MARKER_RE, LINK_RE, Letter
from app.hh.resume import NormalizedResume
from app.services.audit import audit
from app.services.preferences import get_preferences
from app.services.state import get_setting, set_setting

HH_MAX_LETTER_CHARS = 10_000
MARKER_FULL_RE = re.compile(r"\[\[\s*УТОЧНИТЬ[^\]]*\]\]", re.IGNORECASE)
TEMPLATE_KEY = "letter_template"
DEFAULT_TEMPLATE = (
    "{greeting}\n\n"
    "Меня заинтересовала вакансия «{vacancy_name}» в компании {employer_name}. "
    "Я {my_title}, и мой опыт напрямую связан с задачами, описанными в вакансии.\n\n"
    "Буду рад обсудить детали и ответить на вопросы.\n\n"
    "С уважением,\n{my_name}"
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def letter_warnings(text: str) -> list[str]:
    warnings: list[str] = []
    if LINK_RE.search(text):
        warnings.append("в письме есть ссылка или контакт — hh может отсеять как спам")
    if CLARIFY_MARKER_RE.search(text):
        warnings.append("в письме остались маркеры [[УТОЧНИТЬ]]")
    if len(text) > HH_MAX_LETTER_CHARS:
        warnings.append(f"длина {len(text)} > {HH_MAX_LETTER_CHARS} символов (лимит hh)")
    return warnings


def check_before_send(text: str | None, *, required: bool) -> list[str]:
    """Blocking problems for sending."""
    problems: list[str] = []
    if not text or not text.strip():
        if required:
            problems.append("сопроводительное письмо обязательно для этой вакансии")
        return problems
    if len(text) > HH_MAX_LETTER_CHARS:
        problems.append(f"письмо длиннее {HH_MAX_LETTER_CHARS} символов")
    if CLARIFY_MARKER_RE.search(text):
        problems.append("в письме есть маркеры [[УТОЧНИТЬ]] — заполните их")
    if LINK_RE.search(text):
        problems.append("в письме есть ссылка или контакт — уберите")
    return problems


def letter_status(letter: CoverLetter | None) -> str:
    """none | ready | edited | template | needs_clarification"""
    if letter is None:
        return "none"
    if _loads(letter.needs_clarification_json, []) or CLARIFY_MARKER_RE.search(letter.text):
        return "needs_clarification"
    return {"claude": "ready", "edited": "edited", "template": "template"}.get(letter.origin, "ready")


def letter_row(letter: CoverLetter) -> dict[str, Any]:
    created = as_utc(letter.created_at)
    return {
        "id": letter.id,
        "version": letter.version,
        "text": letter.text,
        "language": letter.language,
        "char_count": letter.char_count,
        "origin": letter.origin,
        "batch_id": letter.batch_id,
        "mentions": _loads(letter.mentions_json, []),
        "needs_clarification": _loads(letter.needs_clarification_json, []),
        "warnings": _loads(letter.warnings_json, []),
        "is_current": letter.is_current,
        "status": letter_status(letter),
        "created_at": created.isoformat(timespec="seconds") if created else None,
        "send_problems": check_before_send(letter.text, required=False),
    }


async def current_letter(session: AsyncSession, vacancy_id: int) -> CoverLetter | None:
    return (
        await session.execute(
            select(CoverLetter)
            .where(CoverLetter.vacancy_id == vacancy_id, CoverLetter.is_current.is_(True))
            .order_by(desc(CoverLetter.version))
            .limit(1)
        )
    ).scalar_one_or_none()


async def letter_versions(session: AsyncSession, vacancy_id: int) -> list[CoverLetter]:
    rows = await session.execute(
        select(CoverLetter).where(CoverLetter.vacancy_id == vacancy_id).order_by(desc(CoverLetter.version))
    )
    return list(rows.scalars().all())


async def current_letters_map(session: AsyncSession, vacancy_ids: list[int]) -> dict[int, CoverLetter]:
    if not vacancy_ids:
        return {}
    rows = await session.execute(
        select(CoverLetter).where(CoverLetter.vacancy_id.in_(vacancy_ids), CoverLetter.is_current.is_(True))
    )
    return {r.vacancy_id: r for r in rows.scalars().all()}


async def add_letter(
    session: AsyncSession,
    vacancy: Vacancy,
    *,
    text: str,
    origin: str,
    language: str | None = None,
    batch_id: str | None = None,
    mentions: list[str] | None = None,
    needs_clarification: list[str] | None = None,
    extra_warnings: list[str] | None = None,
    check_style: bool = False,
) -> CoverLetter:
    text = text.replace("\r\n", "\n").strip()
    if not text:
        raise ValueError("пустое письмо")
    max_version = (
        await session.execute(select(func.max(CoverLetter.version)).where(CoverLetter.vacancy_id == vacancy.id))
    ).scalar() or 0
    for prev in (
        await session.execute(
            select(CoverLetter).where(CoverLetter.vacancy_id == vacancy.id, CoverLetter.is_current.is_(True))
        )
    ).scalars():
        prev.is_current = False
    markers = [m.group(0) for m in MARKER_FULL_RE.finditer(text)]
    clarifications = list(needs_clarification or [])
    for marker in markers:
        if marker not in clarifications:
            clarifications.append(marker)
    warnings = letter_warnings(text) + list(extra_warnings or [])
    if check_style and origin in ("claude", "edited"):
        from app.services import humanizer

        warnings += await humanizer.letter_warnings(text)
    row = CoverLetter(
        vacancy_id=vacancy.id,
        version=int(max_version) + 1,
        text=text,
        language=language,
        char_count=len(text),
        origin=origin,
        batch_id=batch_id,
        mentions_json=_json(mentions or []),
        needs_clarification_json=_json(clarifications),
        warnings_json=_json(warnings),
        is_current=True,
    )
    session.add(row)
    await session.flush()
    return row


async def restore_version(session: AsyncSession, vacancy: Vacancy, letter_id: int) -> CoverLetter:
    target = await session.get(CoverLetter, letter_id)
    if target is None or target.vacancy_id != vacancy.id:
        raise ValueError("версия письма не найдена")
    for row in (await session.execute(select(CoverLetter).where(CoverLetter.vacancy_id == vacancy.id))).scalars():
        row.is_current = row.id == target.id
    await session.flush()
    message = f"Возвращена версия письма {target.version} для «{vacancy.name}»"
    await audit(session, "letters", message, vacancy_id=vacancy.id)
    return target


async def save_edit(session: AsyncSession, vacancy: Vacancy, text: str, *, check_style: bool = False) -> CoverLetter:
    current = await current_letter(session, vacancy.id)
    if current is not None and current.text.strip() == text.replace("\r\n", "\n").strip():
        return current
    row = await add_letter(
        session,
        vacancy,
        text=text,
        origin="edited",
        language=current.language if current else None,
        check_style=check_style,
    )
    message = f"Письмо отредактировано (версия {row.version}) для «{vacancy.name}»"
    await audit(session, "letters", message, vacancy_id=vacancy.id)
    return row


async def request_revision(session: AsyncSession, vacancy: Vacancy, comment: str) -> LetterRevisionRequest:
    comment = comment.strip()
    if not comment:
        raise ValueError("укажите, что изменить в письме")
    current = await current_letter(session, vacancy.id)
    if current is None:
        raise ValueError("сначала нужно письмо — запросите разбор в Claude Code")
    existing = (
        await session.execute(
            select(LetterRevisionRequest).where(
                LetterRevisionRequest.vacancy_id == vacancy.id, LetterRevisionRequest.status == "queued"
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.user_comment = comment
        existing.previous_letter_id = current.id
        await session.flush()
        return existing
    row = LetterRevisionRequest(vacancy_id=vacancy.id, previous_letter_id=current.id, user_comment=comment)
    session.add(row)
    await session.flush()
    message = f"Запрошена переработка письма для «{vacancy.name}»: {comment}"
    await audit(session, "letters", message, vacancy_id=vacancy.id)
    return row


async def pending_revision(session: AsyncSession, vacancy_id: int) -> LetterRevisionRequest | None:
    return (
        await session.execute(
            select(LetterRevisionRequest)
            .where(
                LetterRevisionRequest.vacancy_id == vacancy_id,
                LetterRevisionRequest.status.in_(["queued", "sent_in_batch"]),
            )
            .order_by(desc(LetterRevisionRequest.id))
            .limit(1)
        )
    ).scalar_one_or_none()


# ---- template fallback ------------------------------------------------------


async def get_template(session: AsyncSession) -> str:
    value = await get_setting(session, TEMPLATE_KEY)
    return str(value) if isinstance(value, str) and value.strip() else DEFAULT_TEMPLATE


async def set_template(session: AsyncSession, text: str) -> None:
    await set_setting(session, TEMPLATE_KEY, text.strip() or DEFAULT_TEMPLATE)


async def render_template(session: AsyncSession, vacancy: Vacancy) -> str:
    prefs = await get_preferences(session)
    resume = (
        await session.execute(select(Resume).where(Resume.source == "hh", Resume.is_primary.is_(True)).limit(1))
    ).scalar_one_or_none()
    title = prefs.target_titles[0] if prefs.target_titles else ""
    name = ""
    if resume is not None and resume.structured_json:
        try:
            norm = NormalizedResume.model_validate_json(resume.structured_json)
            title = title or (norm.title or "")
            name = norm.full_name or ""
        except ValueError:
            pass
    template = await get_template(session)
    values = {
        "greeting": prefs.letter_style.greeting or "Здравствуйте!",
        "vacancy_name": vacancy.name,
        "employer_name": vacancy.employer_name or "вашей компании",
        "my_title": title or "специалист с релевантным опытом",
        "my_name": name or "",
    }
    try:
        return template.format(**values).strip()
    except (KeyError, IndexError, ValueError):
        return DEFAULT_TEMPLATE.format(**values).strip()


async def apply_template(session: AsyncSession, vacancy: Vacancy) -> CoverLetter:
    text = await render_template(session, vacancy)
    row = await add_letter(session, vacancy, text=text, origin="template")
    message = f"Письмо из шаблона (версия {row.version}) для «{vacancy.name}»"
    await audit(session, "letters", message, vacancy_id=vacancy.id)
    return row


def letter_from_result(letter: Letter, *, min_chars: int, max_chars: int) -> tuple[list[str], list[str]]:
    """Warnings and clarifications for a letter produced by Claude."""
    warnings: list[str] = []
    n = len(letter.text)
    if n < int(min_chars * 0.8):
        warnings.append(f"письмо короче ожидаемого ({n} < {min_chars})")
    elif n > int(max_chars * 1.2):
        warnings.append(f"письмо длиннее ожидаемого ({n} > {max_chars})")
    cliches = letter.cliches()
    if cliches:
        warnings.append("клише: " + ", ".join(cliches))
    clarifications = list(letter.needs_clarification)
    for marker in letter.markers():
        if marker not in clarifications:
            clarifications.append(marker)
    return warnings, clarifications
