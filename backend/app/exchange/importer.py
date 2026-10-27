"""Importing /hh-batch results from data/exchange/done."""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.db.models import (
    ClaudeBatch,
    CoverLetter,
    InterviewPrep,
    LetterRevisionRequest,
    QuestionAnswers,
    ReplyDraft,
    ResumeAdvice,
    StudioLetterRequest,
    Vacancy,
    VacancyEvaluation,
    WsTenderReplyRequest,
)
from app.db.session import release_writes
from app.exchange.packets import done_dir, imported_dir, pending_dir, rejected_dir
from app.exchange.schemas import LINK_RE, SUPPORTED_PROTOCOLS, Packet, ResultFile, ResultItem
from app.services import humanizer
from app.services.audit import audit
from app.services.letters import add_letter, letter_from_result
from app.services.profile import active_profile
from app.services.relocate import local_path
from app.services.state import get_hh_auth
from app.services.tagging import clean_tags

log = structlog.get_logger("exchange.importer")

STABLE_SECONDS = 2.0


@dataclass
class ImportOutcome:
    file: str
    batch_id: str | None = None
    status: str = "rejected"  # imported | partial | rejected | skipped
    evaluations: int = 0
    letters: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    stale_profile: bool = False
    suitable: int = 0
    maybe: int = 0
    replies: int = 0
    preps: int = 0
    advices: int = 0
    pitches: int = 0
    answers: int = 0
    top: list[dict[str, Any]] = field(default_factory=list)
    # vacancies with a fresh cover letter for the `letter` Telegram event (payloads, see _letter_payload)
    with_letters: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "batch_id": self.batch_id,
            "status": self.status,
            "evaluations": self.evaluations,
            "letters": self.letters,
            "errors": self.errors,
            "warnings": self.warnings,
            "missing": self.missing,
            "stale_profile": self.stale_profile,
            "suitable": self.suitable,
            "maybe": self.maybe,
            "replies": self.replies,
            "preps": self.preps,
            "advices": self.advices,
            "pitches": self.pitches,
            "answers": self.answers,
        }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _move(src: Path, target_dir: Path, *, rename: str | None = None) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / (rename or src.name)
    if dest.exists():
        dest = target_dir / f"{src.stem}_{int(time.time())}{src.suffix}"
    shutil.move(str(src), str(dest))
    return dest


def _reject(settings: Settings, path: Path, reason: str, outcome: ImportOutcome) -> ImportOutcome:
    outcome.status = "rejected"
    outcome.errors.append(reason)
    try:
        dest = _move(path, rejected_dir(settings))
        dest.with_suffix(dest.suffix + ".error.txt").write_text(reason, encoding="utf-8")
    except OSError as exc:  # pragma: no cover - filesystem trouble
        log.warning("reject_move_failed", path=str(path), error=str(exc))
    log.warning("result_rejected", file=path.name, reason=reason[:300])
    return outcome


def is_stable(path: Path, seconds: float = STABLE_SECONDS) -> bool:
    try:
        return time.time() - path.stat().st_mtime >= seconds
    except OSError:
        return False


def _verdict_status(score: int, settings: Settings) -> str:
    if score >= settings.score_threshold_suitable:
        return "suitable"
    if score >= settings.score_threshold_maybe:
        return "maybe"
    return "not_suitable"


# a vacancy Claude fails on this many times in a row (error, no evaluation, no result) is parked in needs_manual
MAX_CLAUDE_ATTEMPTS = 3
BATCH_ID_RE = re.compile(r"^\d{8}_\d{4}_[0-9a-f]{4}$")  # packets.new_batch_id
_SECRET_PATTERNS = (
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),  # Telegram bot token
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:HH_CLIENT_SECRET|HH_CLIENT_ID|TELEGRAM_BOT_TOKEN|IMAP_PASSWORD)\s*="),
)


async def _known_secrets(session: AsyncSession, settings: Settings) -> list[str]:
    """Values that must never appear in a letter or a draft: the .env secrets and the user's hh tokens."""
    values = [settings.hh_client_secret, settings.telegram_bot_token, settings.imap_password]
    auth = await get_hh_auth(session)
    if auth is not None:
        values += [auth.access_token or "", auth.refresh_token or ""]
    return [v for v in values if v and len(v) >= 8]


def leaked_secret(text: str, secrets: list[str]) -> str | None:
    """Why the text looks like it carries a secret (a prompt injection in a vacancy may ask Claude to paste .env into
    the letter, which then goes to the employer and to the bot); None when it is clean."""
    if any(v in text for v in secrets):
        return "в тексте секрет из настроек панели"
    if any(rx.search(text) for rx in _SECRET_PATTERNS):
        return "в тексте фрагмент, похожий на токен или ключ"
    return None


def _requeue(vacancy: Vacancy, reason: str) -> None:
    """A result without an evaluation: back to `prescored` for the next packet, or — after MAX_CLAUDE_ATTEMPTS
    failures in a row — parked in needs_manual, so a vacancy Claude keeps failing on is not re-packed forever."""
    vacancy.batch_id = None
    vacancy.claude_attempts = (vacancy.claude_attempts or 0) + 1
    if vacancy.claude_attempts >= MAX_CLAUDE_ATTEMPTS:
        vacancy.pipeline_status = "needs_manual"
        vacancy.pipeline_reason = (
            f"Claude не разобрал вакансию {vacancy.claude_attempts} раза подряд ({reason[:200]}); "
            "«В пакет для Claude» отправит её снова"
        )
    else:
        vacancy.pipeline_status = "prescored"
        vacancy.pipeline_reason = reason


async def _apply_item(
    session: AsyncSession,
    settings: Settings,
    batch: ClaudeBatch,
    task_type: str,
    item: ResultItem,
    letter_defaults: dict[str, Any],
    outcome: ImportOutcome,
) -> None:
    if task_type == "resume_advice":
        await _finish_advice(session, batch.batch_id, item, outcome)
        return
    if task_type == "studio_letter":
        await _finish_studio(session, settings, batch.batch_id, item, outcome)
        return
    if task_type == "tender_reply":
        await _finish_tender(session, settings, batch.batch_id, item, outcome)
        return
    vacancy = (await session.execute(select(Vacancy).where(Vacancy.hh_id == item.vacancy_id))).scalar_one_or_none()
    if vacancy is None:
        outcome.errors.append(f"{item.vacancy_id}: вакансия не найдена в базе")
        return

    if item.error:
        outcome.errors.append(f"{item.vacancy_id}: {item.error}")
        if task_type == "evaluate_and_letter" and vacancy.pipeline_status == "awaiting_claude":
            _requeue(vacancy, f"ошибка разбора: {item.error[:200]}")
        if task_type == "letter_revision":
            await _finish_revision(session, vacancy, batch.batch_id, done=False)
        if task_type == "reply_draft":
            await _finish_reply(session, vacancy, batch.batch_id, item, error=item.error)
        if task_type == "interview_prep":
            await _finish_prep(session, vacancy, batch.batch_id, item, error=item.error)
        if task_type == "answer_questions":
            await _finish_answers(session, vacancy, batch.batch_id, item, error=item.error)
        return

    if task_type == "answer_questions":
        if item.answers is None:
            outcome.errors.append(f"{item.vacancy_id}: нет answers")
            await _finish_answers(session, vacancy, batch.batch_id, item, error="результат без answers")
        else:
            await _finish_answers(session, vacancy, batch.batch_id, item)
            outcome.answers += 1
        return

    if task_type == "interview_prep":
        if item.prep is None:
            outcome.errors.append(f"{item.vacancy_id}: нет prep")
            await _finish_prep(session, vacancy, batch.batch_id, item, error="результат без prep")
        else:
            await _finish_prep(session, vacancy, batch.batch_id, item)
            outcome.preps += 1
        return

    if task_type == "reply_draft":
        if item.reply is None:
            outcome.errors.append(f"{item.vacancy_id}: нет reply")
            await _finish_reply(session, vacancy, batch.batch_id, item, error="результат без reply")
        else:
            await _finish_reply(session, vacancy, batch.batch_id, item)
            outcome.replies += 1
        return

    if task_type == "evaluate_and_letter":
        if item.evaluation is None:
            outcome.errors.append(f"{item.vacancy_id}: нет evaluation")
            if vacancy.pipeline_status == "awaiting_claude":
                _requeue(vacancy, "результат без оценки")
            return
        ev = item.evaluation
        existing = (
            await session.execute(
                select(VacancyEvaluation).where(
                    VacancyEvaluation.vacancy_id == vacancy.id, VacancyEvaluation.batch_id == batch.batch_id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            for prev in (
                await session.execute(
                    select(VacancyEvaluation).where(
                        VacancyEvaluation.vacancy_id == vacancy.id, VacancyEvaluation.is_current.is_(True)
                    )
                )
            ).scalars():
                prev.is_current = False
            existing = VacancyEvaluation(vacancy_id=vacancy.id, batch_id=batch.batch_id)
            session.add(existing)
        existing.profile_version = batch.profile_version
        existing.protocol_version = batch.protocol_version
        existing.score = ev.score
        existing.verdict = ev.verdict
        existing.summary = ev.summary
        existing.matched_json = _json(ev.matched)
        existing.gaps_json = _json(ev.gaps)
        existing.red_flags_json = _json(ev.red_flags)
        existing.salary_fit = ev.salary_fit
        existing.seniority_fit = ev.seniority_fit
        existing.language = ev.language
        existing.tags_json = _json(clean_tags(ev.tags.model_dump())) if ev.tags is not None else None
        existing.is_current = True
        vacancy.score = ev.score
        vacancy.claude_attempts = 0
        if vacancy.pipeline_status != "awaiting_claude":
            # while the packet was with Claude the user hid or skipped the vacancy, applied, or the freshness check
            # closed it: the evaluation is stored, the status and the letter stay as they are (no new «Подходит»,
            # no letter in the bot for a vacancy that is already handled)
            if vacancy.batch_id == batch.batch_id:
                vacancy.batch_id = None
            outcome.evaluations += 1
            outcome.warnings.append(
                f"{item.vacancy_id}: пока шёл разбор, вакансия получила статус «{vacancy.pipeline_status}» — "
                "оценка сохранена, статус и письмо не изменены"
            )
            return
        vacancy.pipeline_status = _verdict_status(ev.score, settings)
        vacancy.pipeline_reason = None if vacancy.pipeline_status != "not_suitable" else f"оценка Claude {ev.score}"
        vacancy.batch_id = None
        outcome.evaluations += 1
        if vacancy.pipeline_status in ("suitable", "maybe"):
            if vacancy.pipeline_status == "suitable":
                outcome.suitable += 1
            else:
                outcome.maybe += 1
            outcome.top.append(
                {
                    "hh_id": vacancy.hh_id,
                    "name": vacancy.name,
                    "employer": vacancy.employer_name,
                    "url": vacancy.alternate_url,
                    "score": ev.score,
                    "salary": vacancy.salary_net_rur,
                }
            )

    if item.letter is not None:
        warnings, clarifications = letter_from_result(
            item.letter,
            min_chars=int(letter_defaults.get("min_chars") or 600),
            max_chars=int(letter_defaults.get("max_chars") or 1200),
        )
        dup = (
            await session.execute(
                select(CoverLetter).where(CoverLetter.vacancy_id == vacancy.id, CoverLetter.batch_id == batch.batch_id)
            )
        ).scalar_one_or_none()
        if dup is None:
            letter_row = await add_letter(
                session,
                vacancy,
                text=item.letter.text,
                origin="claude",
                language=item.letter.language,
                batch_id=batch.batch_id,
                mentions=item.letter.mentions,
                needs_clarification=clarifications,
                extra_warnings=warnings,
                check_style=settings.humanizer_check,
            )
            outcome.letters += 1
            if vacancy.score is not None and vacancy.pipeline_status in ("suitable", "maybe"):
                outcome.with_letters.append(
                    _letter_payload(
                        vacancy,
                        item.letter.text,
                        version=letter_row.version,
                        needs_clarification=bool(clarifications),
                        revision=task_type == "letter_revision",
                        summary=item.evaluation.summary if item.evaluation is not None else None,
                    )
                )
        for w in warnings:
            outcome.warnings.append(f"{item.vacancy_id}: {w}")
        if task_type == "letter_revision":
            await _finish_revision(session, vacancy, batch.batch_id, done=True)
    elif task_type == "letter_revision":
        outcome.errors.append(f"{item.vacancy_id}: переработка без письма")
        await _finish_revision(session, vacancy, batch.batch_id, done=False)


def _stub_id(key: str) -> int:
    """`advice-7` / `studio-7` → 7 (-1 for a malformed key, which matches no row)."""
    try:
        return int(key.split("-", 1)[1])
    except (IndexError, ValueError):
        return -1


def _loads_list(text: str | None) -> list[Any]:
    try:
        value = json.loads(text or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _letter_payload(
    vacancy: Vacancy,
    text: str,
    *,
    version: int | None,
    needs_clarification: bool,
    revision: bool,
    summary: str | None,
) -> dict[str, Any]:
    formats = _loads_list(vacancy.work_format_names_json)
    return {
        "vacancy_id": vacancy.id,
        "hh_id": vacancy.hh_id,
        "name": vacancy.name,
        "employer": vacancy.employer_name,
        "url": vacancy.alternate_url,
        "apply_url": vacancy.apply_alternate_url or vacancy.alternate_url,
        "score": vacancy.score,
        "status": vacancy.pipeline_status,
        "salary": vacancy.salary_net_rur,
        "work_format": ", ".join(str(f) for f in formats) or None,
        "area": vacancy.area_name,
        "address": vacancy.address,
        "metro": _loads_list(vacancy.metro_json),
        "summary": summary,
        "letter": text,
        "version": version,
        "needs_clarification": needs_clarification,
        "revision": revision,
    }


async def _finish_reply(
    session: AsyncSession, vacancy: Vacancy, batch_id: str, item: ResultItem, *, error: str | None = None
) -> None:
    rows = (
        (
            await session.execute(
                select(ReplyDraft).where(ReplyDraft.vacancy_id == vacancy.id, ReplyDraft.batch_id == batch_id)
            )
        )
        .scalars()
        .all()
    )
    for r in rows:
        if error or item.reply is None:
            r.status = "cancelled"
            r.error = error or "нет reply"
            continue
        r.status = "done"
        r.error = None
        r.draft_text = item.reply.text
        r.intent = item.reply.intent
        r.needs_clarification_json = _json(item.reply.needs_clarification)


async def _finish_prep(
    session: AsyncSession, vacancy: Vacancy, batch_id: str, item: ResultItem, *, error: str | None = None
) -> None:
    rows = (
        (
            await session.execute(
                select(InterviewPrep).where(InterviewPrep.vacancy_id == vacancy.id, InterviewPrep.batch_id == batch_id)
            )
        )
        .scalars()
        .all()
    )
    for r in rows:
        if error or item.prep is None:
            r.status = "cancelled"
            r.error = error or "нет prep"
            continue
        r.status = "done"
        r.error = None
        r.result_json = _json(item.prep.model_dump())


async def _finish_answers(
    session: AsyncSession, vacancy: Vacancy, batch_id: str, item: ResultItem, *, error: str | None = None
) -> None:
    rows = (
        (
            await session.execute(
                select(QuestionAnswers).where(
                    QuestionAnswers.vacancy_id == vacancy.id, QuestionAnswers.batch_id == batch_id
                )
            )
        )
        .scalars()
        .all()
    )
    for r in rows:
        if error or item.answers is None:
            r.status = "cancelled"
            r.error = error or "нет answers"
            continue
        r.status = "done"
        r.error = None
        r.result_json = _json(item.answers.model_dump())


async def _finish_advice(session: AsyncSession, batch_id: str, item: ResultItem, outcome: ImportOutcome) -> None:
    """`advice-<id>` results: the advice row of this packet gets the edits (or an error)."""
    try:
        advice_id = int(item.vacancy_id.split("-", 1)[1])
    except (IndexError, ValueError):
        outcome.errors.append(f"{item.vacancy_id}: неверный id правок резюме")
        return
    row = await session.get(ResumeAdvice, advice_id)
    if row is None or row.batch_id != batch_id:
        outcome.errors.append(f"{item.vacancy_id}: запрос правок резюме не найден в пакете")
        return
    if item.error or item.advice is None:
        row.status = "cancelled"
        row.error = item.error or "результат без advice"
        outcome.errors.append(f"{item.vacancy_id}: {row.error}")
        return
    row.status = "done"
    row.error = None
    row.result_json = _json(item.advice.model_dump())
    outcome.advices += 1


async def _finish_studio(
    session: AsyncSession, settings: Settings, batch_id: str, item: ResultItem, outcome: ImportOutcome
) -> None:
    """`studio-<id>` results: a draft per style (`pitches`; `pitch` of a v9 result is one business draft), the first
    one becomes the studio's current version — or the request is closed with the error and the studio returns to
    where it was."""
    from app.services import studios

    try:
        request_id = int(item.vacancy_id.split("-", 1)[1])
    except (IndexError, ValueError):
        outcome.errors.append(f"{item.vacancy_id}: неверный id письма студии")
        return
    req = await session.get(StudioLetterRequest, request_id)
    if req is None or req.batch_id != batch_id:
        outcome.errors.append(f"{item.vacancy_id}: запрос письма студии не найден в пакете")
        return
    pitches = item.studio_pitches()
    if item.error or not pitches:
        error = item.error or "результат без pitches"
        await studios.fail_letter(session, req, error)
        outcome.errors.append(f"{item.vacancy_id}: {error}")
        return
    for n, pitch in enumerate(pitches):
        warnings: list[str] = []
        if len(pitch.text) > 2500:
            warnings.append(f"письмо длинное ({len(pitch.text)} знаков) — студии читают первые строки")
        if settings.humanizer_check and pitch.language == "ru":
            warnings += await humanizer.letter_warnings(pitch.text)
        clarifications = list(pitch.needs_clarification)
        for marker in re.findall(r"\[\[\s*УТОЧНИТЬ[^\]]*\]\]", pitch.text, re.IGNORECASE):
            if marker not in clarifications:
                clarifications.append(marker)
        await studios.finish_letter(
            session,
            req,
            subject=pitch.subject,
            text=pitch.text,
            batch_id=batch_id,
            needs_clarification=clarifications,
            warnings=warnings,
            style=pitch.style,
            current=n == 0,
        )
    outcome.pitches += 1


async def _finish_tender(
    session: AsyncSession, settings: Settings, batch_id: str, item: ResultItem, outcome: ImportOutcome
) -> None:
    """`tender-<id>` results: the answer to the tender becomes its current version (or the request is closed with
    the error and the tender returns to where it was)."""
    from app.services import ws_tenders

    req = await session.get(WsTenderReplyRequest, _stub_id(item.vacancy_id))
    if req is None or req.batch_id != batch_id:
        outcome.errors.append(f"{item.vacancy_id}: запрос отклика на тендер не найден в пакете")
        return
    if item.error or item.bid is None:
        error = item.error or "результат без bid"
        await ws_tenders.fail_reply(session, req, error)
        outcome.errors.append(f"{item.vacancy_id}: {error}")
        return
    bid = item.bid
    warnings: list[str] = []
    if len(bid.text) > 4000:
        warnings.append(f"отклик длинный ({len(bid.text)} знаков) — заказчик читает первые строки")
    if LINK_RE.search(bid.text):
        warnings.append("в отклике ссылка — контакты и ссылки workspace.ru открывает после отклика, лучше без них")
    if settings.humanizer_check and bid.language == "ru":
        warnings += await humanizer.letter_warnings(bid.text)
    clarifications = list(bid.needs_clarification)
    for marker in re.findall(r"\[\[\s*УТОЧНИТЬ[^\]]*\]\]", bid.text, re.IGNORECASE):
        if marker not in clarifications:
            clarifications.append(marker)
    await ws_tenders.finish_reply(
        session, req, text=bid.text, batch_id=batch_id, needs_clarification=clarifications, warnings=warnings
    )
    outcome.pitches += 1


async def _finish_revision(session: AsyncSession, vacancy: Vacancy, batch_id: str, *, done: bool) -> None:
    rows = (
        (
            await session.execute(
                select(LetterRevisionRequest).where(
                    LetterRevisionRequest.vacancy_id == vacancy.id, LetterRevisionRequest.batch_id == batch_id
                )
            )
        )
        .scalars()
        .all()
    )
    for r in rows:
        r.status = "done" if done else "queued"
        if not done:
            r.batch_id = None


def _style_texts(result: ResultFile) -> list[str]:
    """The texts the import runs the humanizer-ru scanner on: letters, drafts to studios, answers to tenders."""
    texts: list[str] = []
    for item in result.results:
        if item.error:
            continue
        if item.letter is not None:
            texts.append(item.letter.text)
        texts += [p.text for p in item.studio_pitches()]
        if item.bid is not None:
            texts.append(item.bid.text)
    return texts


async def import_result_file(session: AsyncSession, settings: Settings, path: Path) -> ImportOutcome:
    """Imports one result file; the style scans of its texts are made before its first write (_import_file)."""
    with humanizer.prescan_scope():
        return await _import_file(session, settings, path)


async def _import_file(session: AsyncSession, settings: Settings, path: Path) -> ImportOutcome:
    outcome = ImportOutcome(file=path.name)
    try:
        raw_text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        outcome.errors.append(f"не удалось прочитать файл: {exc}")
        return outcome
    try:
        raw = json.loads(raw_text)
    except ValueError as exc:
        return _reject(settings, path, f"невалидный JSON: {exc}", outcome)
    try:
        result = ResultFile.model_validate(raw)
    except ValidationError as exc:
        return _reject(settings, path, f"результат не соответствует схеме: {exc.errors()[:5]}", outcome)

    outcome.batch_id = result.batch_id
    if result.protocol_version not in SUPPORTED_PROTOCOLS:
        return _reject(settings, path, f"protocol_version {result.protocol_version} не поддерживается", outcome)
    if path.stem != f"batch_{result.batch_id}":
        return _reject(settings, path, f"имя файла {path.name} не совпадает с batch_id {result.batch_id}", outcome)
    batch = (
        await session.execute(select(ClaudeBatch).where(ClaudeBatch.batch_id == result.batch_id))
    ).scalar_one_or_none()
    if batch is None:
        return _reject(settings, path, f"пакет {result.batch_id} неизвестен панели", outcome)
    if batch.status in ("imported", "cancelled"):
        outcome.status = "skipped"
        outcome.warnings.append(f"пакет {batch.batch_id} уже в статусе {batch.status} — файл проигнорирован")
        _move(path, imported_dir(settings), rename=f"{path.stem}.duplicate.json")
        return outcome

    # the stored path may come from another machine (data/ moved between Windows and Linux) or the file was moved by
    # hand: then the packet is the file with its name in pending/ or imported/ (services/relocate.py)
    packet_path = local_path(batch.packet_path, pending_dir(settings), imported_dir(settings)) or Path(
        batch.packet_path
    )
    try:
        packet = Packet.model_validate_json(packet_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        return _reject(settings, path, f"не удалось прочитать пакет {packet_path.name}: {exc}", outcome)

    profile = await active_profile(session)
    if profile is not None and result.profile_version is not None and result.profile_version != profile.version:
        outcome.stale_profile = True
        outcome.warnings.append(
            f"результат сделан для профиля версии {result.profile_version}, текущая {profile.version}"
        )

    task_types = {t.vacancy.id: t.type for t in packet.tasks}
    secrets = await _known_secrets(session, settings)
    if settings.humanizer_check:
        # every scan is a Python process (1–2 s): made now, outside a write transaction — the writes of the previous
        # file of the same run are committed first — the letters below take the verdicts from the scope
        await release_writes(session)
        await humanizer.prescan(_style_texts(result))
    seen: set[str] = set()  # every vacancy_id in the file (for dedup)
    applied: set[str] = set()  # results that matched a task
    for item in result.results:
        if item.vacancy_id in seen:
            outcome.warnings.append(f"{item.vacancy_id}: дубликат результата пропущен")
            continue
        seen.add(item.vacancy_id)
        task_type = task_types.get(item.vacancy_id)
        if task_type is None:
            outcome.warnings.append(f"{item.vacancy_id}: результата нет среди задач пакета — пропущен")
            continue
        applied.add(item.vacancy_id)
        leak = (
            None if item.error else leaked_secret(json.dumps(item.model_dump(mode="json"), ensure_ascii=False), secrets)
        )
        if leak is not None:
            log.warning("result_rejected_secret", batch_id=batch.batch_id, vacancy_id=item.vacancy_id, reason=leak)
            await audit(
                session,
                "batches",
                f"Результат по {item.vacancy_id} отклонён: {leak}",
                level="error",
                batch_id=batch.batch_id,
            )
            item = item.model_copy(
                update={
                    "error": f"результат отклонён панелью: {leak}",
                    "evaluation": None,
                    "letter": None,
                    "reply": None,
                    "prep": None,
                    "advice": None,
                    "answers": None,
                    "pitches": None,
                    "pitch": None,
                    "bid": None,
                }
            )
        await _apply_item(session, settings, batch, task_type, item, packet.letter_defaults, outcome)

    # tasks without results: return to the queue so the next packet picks them up
    for hh_id, task_type in task_types.items():
        if hh_id in applied:
            continue
        outcome.missing.append(hh_id)
        if task_type == "studio_letter":
            for sl in (
                await session.execute(
                    select(StudioLetterRequest).where(
                        StudioLetterRequest.batch_id == batch.batch_id,
                        StudioLetterRequest.status == "sent_in_batch",
                        StudioLetterRequest.id == _stub_id(hh_id),
                    )
                )
            ).scalars():
                sl.status, sl.batch_id = "queued", None  # no result → the next packet
            continue
        if task_type == "tender_reply":
            tr = await session.get(WsTenderReplyRequest, _stub_id(hh_id))
            if tr is not None and tr.batch_id == batch.batch_id and tr.status == "sent_in_batch":
                tr.status, tr.batch_id = "queued", None
            continue
        if task_type == "resume_advice":
            # only the requests still waiting for this packet: one that got its edits (or an error) above stays so
            for adv in (
                await session.execute(
                    select(ResumeAdvice).where(
                        ResumeAdvice.batch_id == batch.batch_id, ResumeAdvice.status == "sent_in_batch"
                    )
                )
            ).scalars():
                adv.status, adv.batch_id = "queued", None
            continue
        vacancy = (await session.execute(select(Vacancy).where(Vacancy.hh_id == hh_id))).scalar_one_or_none()
        if vacancy is None:
            continue
        if task_type == "evaluate_and_letter" and vacancy.pipeline_status == "awaiting_claude":
            _requeue(vacancy, f"не разобрана в пакете {batch.batch_id}, будет включена снова")
        elif task_type == "letter_revision":
            await _finish_revision(session, vacancy, batch.batch_id, done=False)
        elif task_type == "reply_draft":
            for r in (
                await session.execute(
                    select(ReplyDraft).where(ReplyDraft.vacancy_id == vacancy.id, ReplyDraft.batch_id == batch.batch_id)
                )
            ).scalars():
                r.status, r.batch_id = "queued", None  # no result → next packet
        elif task_type == "interview_prep":
            for p in (
                await session.execute(
                    select(InterviewPrep).where(
                        InterviewPrep.vacancy_id == vacancy.id, InterviewPrep.batch_id == batch.batch_id
                    )
                )
            ).scalars():
                p.status, p.batch_id = "queued", None
        elif task_type == "answer_questions":
            for q in (
                await session.execute(
                    select(QuestionAnswers).where(
                        QuestionAnswers.vacancy_id == vacancy.id, QuestionAnswers.batch_id == batch.batch_id
                    )
                )
            ).scalars():
                q.status, q.batch_id = "queued", None

    # the packet is closed: no vacancy points to it any more (a hidden or skipped one without a result kept the id)
    for leftover in (await session.execute(select(Vacancy).where(Vacancy.batch_id == batch.batch_id))).scalars():
        leftover.batch_id = None
    outcome.status = "partial" if outcome.missing or outcome.errors else "imported"
    batch.status = outcome.status
    batch.results_count = len(applied)
    batch.imported_at = utcnow()
    batch.error = "; ".join(outcome.errors)[:2000] if outcome.errors else None
    batch.summary_json = _json(outcome.to_dict())
    dest = _move(path, imported_dir(settings), rename=f"{path.stem}.result.json")
    batch.result_path = str(dest)
    # a packet left in pending/ would be taken by the next background claude -p again
    packet_file = local_path(batch.packet_path, pending_dir(settings))
    if packet_file is not None:
        batch.packet_path = str(_move(packet_file, imported_dir(settings)))
    await session.flush()
    await audit(
        session,
        "batches",
        f"Импортирован пакет {batch.batch_id}: оценок {outcome.evaluations}, писем {outcome.letters}, "
        f"ошибок {len(outcome.errors)}, без результата {len(outcome.missing)}",
        batch_id=batch.batch_id,
        level="warning" if outcome.errors or outcome.missing else "info",
    )
    log.info("result_imported", batch_id=batch.batch_id, status=outcome.status, evaluations=outcome.evaluations)
    await _notify_import(session, settings, outcome)
    return outcome


async def _notify_import(session: AsyncSession, settings: Settings, outcome: ImportOutcome) -> None:
    """M5: counters and Telegram/UI events after a successful import."""
    from app.services.counters import bump_counter
    from app.services.notify import emit_event

    await bump_counter(session, settings, "letters_imported", outcome.letters)
    await emit_event(
        session,
        settings,
        "batch_imported",
        {
            "batch_id": outcome.batch_id,
            "evaluations": outcome.evaluations,
            "letters": outcome.letters,
            "suitable": outcome.suitable,
            "maybe": outcome.maybe,
            "errors": len(outcome.errors),
            "missing": len(outcome.missing),
        },
    )
    if outcome.suitable or outcome.maybe:
        top = sorted(outcome.top, key=lambda i: int(i.get("score") or 0), reverse=True)[:5]
        await emit_event(
            session, settings, "digest", {"suitable": outcome.suitable, "maybe": outcome.maybe, "top": top}
        )
    # the vacancy with its letter, one message each; the switch
    # and the score threshold live in Settings (services/tg_letters.letters_config)
    from app.services.tg_letters import letters_config, settings_for_letters

    config = await letters_config(session, settings)
    if config["enabled"]:
        letter_settings = settings_for_letters(settings)
        for payload in sorted(outcome.with_letters, key=lambda i: int(i.get("score") or 0), reverse=True):
            if int(payload.get("score") or 0) >= int(config["min_score"]):
                await emit_event(session, letter_settings, "letter", payload)


async def import_pending_results(
    session: AsyncSession, settings: Settings, *, require_stable: bool = True
) -> list[ImportOutcome]:
    folder = done_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    outcomes: list[ImportOutcome] = []
    for path in sorted(folder.glob("*.json")):
        if require_stable and not is_stable(path):
            continue
        outcomes.append(await import_result_file(session, settings, path))
    return outcomes


async def import_result_text(session: AsyncSession, settings: Settings, text: str) -> ImportOutcome:
    """Manual paste: writes the JSON into done/ under the batch name, then imports it."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        return ImportOutcome(file="<paste>", errors=[f"невалидный JSON: {exc}"])
    batch_id = str(raw.get("batch_id") or "").strip() if isinstance(raw, dict) else ""
    if not batch_id:
        return ImportOutcome(file="<paste>", errors=["в JSON нет batch_id"])
    if not BATCH_ID_RE.match(batch_id):  # it becomes a file name in done/
        return ImportOutcome(file="<paste>", errors=[f"неверный batch_id: {batch_id[:60]!r}"])
    folder = done_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"batch_{batch_id}.json"
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return await import_result_file(session, settings, path)
