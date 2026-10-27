"""Employer questions at application (task `answer_questions`).

Some vacancies ask questions in the response form («расскажите о самом сложном проекте», «какие технологии
использовали»). The user pastes them into the vacancy card, the panel queues a task and Claude answers each one
strictly with profile facts — missing facts become «уточнить» markers, multiple-choice questions get the list of
options to tick — and the answers come back next to the vacancy with copy buttons.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc
from app.db.models import QuestionAnswers, Vacancy
from app.services.audit import audit

STATUS_LABELS = {
    "queued": "в очереди на пакет",
    "sent_in_batch": "в пакете для Claude",
    "done": "готовы",
    "cancelled": "не выполнены",
}
MAX_QUESTIONS = 12
_NUMBERED = re.compile(r"^\s*(?:\d+[.)]|[-•*])\s+")  # numbering or a bullet at the start of a line
_NUMBER = re.compile(r"^\s*\d+[.)]\s+")
_BULLET = re.compile(r"^\s*[-•*]\s+")
OPTION_MAX_CHARS = 25  # a numbered line this short under a question is an answer option, not a question


class QuestionsError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _body(line: str) -> str:
    return _NUMBERED.sub("", line).strip()


def _asks(line: str) -> bool:
    """A line that expects answer options after it: «…?» or «Выберите:»."""
    return _body(line).endswith(("?", ":"))


def _is_option(line: str, *, under_question: bool) -> bool:
    """A line of a multiple-choice list: a bullet, or a short numbered item under a question («1) A1», «2) B2»).
    A numbered line ending with «?» or a long one is a question of its own («2. Что использовали?»)."""
    if _BULLET.match(line):
        return True
    if not _NUMBER.match(line):
        return False
    body = _body(line)
    return under_question and not body.endswith("?") and len(body) < OPTION_MAX_CHARS


def parse_questions(text: str) -> list[str]:
    """Pasted form text → separate questions. Blank lines and numbered questions split («1. …?», «2. …?» on
    consecutive lines or with blank lines between); a question's option list (multiple choice: bullets or short
    numbered items) stays with it; a sentence wrapped over lines is joined; numbering and bullets are stripped."""
    blocks: list[list[str]] = [[]]
    for raw in (text or "").replace("\r", "").split("\n"):
        line = raw.strip().strip("`").strip()
        if not line:
            if blocks[-1]:
                blocks.append([])
            continue
        blocks[-1].append(line)
    items: list[list[str]] = []  # the lines of each question
    for block in blocks:
        if not block:
            continue
        if items:
            prev = items[-1]
            prev_takes_options = _asks(prev[0]) or _asks(prev[-1]) or any(_BULLET.match(x) for x in prev[1:])
            if prev_takes_options and all(_is_option(ln, under_question=True) for ln in block):
                prev.extend(block)  # a list of options after a blank line belongs to the previous question
                continue
        current: list[str] = []
        for ln in block:
            under_question = bool(current) and (_asks(current[0]) or _asks(current[-1]))
            if current and _NUMBER.match(ln) and not _is_option(ln, under_question=under_question):
                items.append(current)  # the next numbered question in the same block
                current = []
            current.append(ln)
        items.append(current)
    out: list[str] = []
    for item in items:
        first = _body(item[0])
        rest = [_body(ln) for ln in item[1:] if _body(ln)]
        if not rest:
            out.append(first)
        elif (_asks(item[0]) and not rest[0].endswith("?")) or any(_NUMBERED.match(ln) for ln in item[1:]):
            out.append(first + "\n" + "\n".join(rest))  # a question with its options or a hint below it
        else:
            out.append(" ".join([first, *rest]))  # one sentence wrapped over several lines
    return [q for q in out if len(q) >= 5][:MAX_QUESTIONS]


def answers_row(q: QuestionAnswers) -> dict[str, Any]:
    try:
        result = json.loads(q.result_json) if q.result_json else None
    except ValueError:
        result = None
    try:
        questions = json.loads(q.questions_json) if q.questions_json else []
    except ValueError:
        questions = []
    created, updated = as_utc(q.created_at), as_utc(q.updated_at)
    return {
        "id": q.id,
        "vacancy_id": q.vacancy_id,
        "questions": questions,
        "status": q.status,
        "status_label": STATUS_LABELS.get(q.status, q.status),
        "batch_id": q.batch_id,
        "result": result,
        "error": q.error,
        "created_at": created.isoformat(timespec="seconds") if created else None,
        "updated_at": updated.isoformat(timespec="seconds") if updated else None,
    }


async def list_answers(session: AsyncSession, vacancy_id: int, limit: int = 5) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(QuestionAnswers)
                .where(QuestionAnswers.vacancy_id == vacancy_id)
                .order_by(desc(QuestionAnswers.id))
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [answers_row(q) for q in rows]


async def request_answers(
    session: AsyncSession, settings: Settings, row: Vacancy, text: str
) -> tuple[QuestionAnswers, str | None]:
    """Queues an `answer_questions` task for the vacancy and tries to build a packet right away."""
    from app.exchange.packets import PacketError, build_batches

    questions = parse_questions(text)
    if not questions:
        raise QuestionsError("Не нашёл вопросов: вставьте текст формы отклика, по вопросу на абзац", 422)
    for old in (
        await session.execute(
            select(QuestionAnswers).where(QuestionAnswers.vacancy_id == row.id, QuestionAnswers.status == "queued")
        )
    ).scalars():
        old.status = "cancelled"
        old.error = "заменён новым запросом"
    req = QuestionAnswers(
        vacancy_id=row.id, questions_text=text.strip(), questions_json=json.dumps(questions, ensure_ascii=False)
    )
    session.add(req)
    await session.flush()
    await audit(
        session,
        "batches",
        f"Запрошены ответы на {len(questions)} вопросов работодателя: «{row.name}»",
        vacancy_id=row.id,
    )
    try:
        created = await build_batches(session, settings, vacancy_ids=[], include_revisions=True)
    except PacketError:
        return req, None
    return req, (created[0].batch_id if created else None)
