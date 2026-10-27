"""Applying to vacancies: consent, limits, queue, API executor, assist mode."""

from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import and_, asc, case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import Application, Negotiation, Resume, SystemState, Vacancy
from app.hh.client import HHClient
from app.hh.errors import (
    HHAuthError,
    HHBadArgument,
    HHCaptchaRequired,
    HHError,
    HHNegotiationError,
    HHNotFound,
)
from app.services.audit import audit
from app.services.letters import check_before_send, current_letter, current_letters_map, letter_status
from app.services.state import get_setting, get_system_state, read_capabilities, set_pause

if TYPE_CHECKING:  # pragma: no cover
    from app.runtime import Runtime

log = structlog.get_logger("applier")

HH_DAILY_CAP = 200
APPLICABLE_STATUSES = {"suitable", "maybe", "prescored", "awaiting_claude", "evaluated", "not_suitable", "needs_manual"}
FINAL_STATUSES = {"sent"}
MAX_ATTEMPTS = 3
# negotiations error value -> (application status, vacancy status or None, human text)
NEGOTIATION_OUTCOMES: dict[str, tuple[str, str | None, str]] = {
    "already_applied": ("sent", "already_applied", "по вакансии уже есть отклик"),
    "test_required": ("needs_manual", "needs_manual", "нужно пройти тест на hh.ru"),
    "resume_visibility_conflict": (
        "needs_manual",
        "needs_manual",
        "видимость резюме не позволяет откликнуться — измените на hh.ru",
    ),
    "disabled_by_employer": ("failed", "needs_manual", "работодатель закрыл отклики"),
    "application_denied": ("failed", "needs_manual", "отклик запрещён без уточнения причины"),
    "invalid_vacancy": ("failed", "filtered_out", "вакансия в архиве или скрыта"),
    "archived": ("failed", "filtered_out", "вакансия в архиве"),
    "vacancy_not_found": ("failed", "filtered_out", "вакансия не найдена"),
    "resume_not_found": ("failed", None, "резюме не найдено или скрыто"),
    "wrong_state": ("failed", None, "действие недоступно в текущем состоянии"),
    "empty_message": ("failed", None, "пустое сопроводительное письмо"),
    "message_cannot_be_empty": ("failed", None, "пустое сопроводительное письмо"),
    "too_long_message": ("failed", None, "письмо слишком длинное"),
}


class ApplyError(Exception):
    def __init__(self, message: str, status: int = 400, code: str | None = None) -> None:
        self.status = status
        self.code = code
        super().__init__(message)


# ---- consent / mode / resume ----------------------------------------------------


async def require_consent(session: AsyncSession) -> SystemState:
    state = await get_system_state(session)
    if state.consent_accepted_at is None:
        raise ApplyError(
            "Сначала подтвердите согласие с условиями отправки откликов", status=409, code="consent_required"
        )
    return state


async def accept_consent(session: AsyncSession) -> None:
    state = await get_system_state(session)
    if state.consent_accepted_at is None:
        state.consent_accepted_at = utcnow()
        await audit(session, "apply", "Пользователь подтвердил согласие на отправку откликов от его имени")


async def resolve_resume_id(session: AsyncSession) -> str | None:
    resume = (
        await session.execute(select(Resume).where(Resume.source == "hh", Resume.is_primary.is_(True)).limit(1))
    ).scalar_one_or_none()
    if resume is not None and resume.hh_resume_id:
        return resume.hh_resume_id
    value = await get_setting(session, "manual_resume_id")
    return str(value) if value else None


# ---- limits -----------------------------------------------------------------


def _tz(settings: Settings) -> ZoneInfo:
    try:
        return ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def parse_window(value: str) -> tuple[time, time]:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", value or "")
    if not m:
        return time(0, 0), time(23, 59)
    try:
        return time(int(m.group(1)), int(m.group(2))), time(int(m.group(3)), int(m.group(4)))
    except ValueError:  # «25:00-21:00»: Settings refuses it, but a stale value must not take the limits down
        return time(0, 0), time(23, 59)


def in_window(now_local: datetime, window: str) -> bool:
    start, end = parse_window(window)
    t = now_local.time().replace(second=0, microsecond=0)
    if start <= end:
        return start <= t <= end
    return t >= start or t <= end  # overnight window


def local_day_bounds(settings: Settings, now: datetime | None = None) -> tuple[datetime, datetime]:
    tz = _tz(settings)
    local_now = (now or utcnow()).astimezone(tz)
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(ZoneInfo("UTC")), (start + timedelta(days=1)).astimezone(ZoneInfo("UTC"))


def next_day_resume_at(settings: Settings, now: datetime | None = None) -> datetime:
    tz = _tz(settings)
    local_now = (now or utcnow()).astimezone(tz)
    nxt = (local_now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return nxt.astimezone(ZoneInfo("UTC"))


@dataclass
class LimitsStatus:
    today_sent: int
    max_per_day: int
    hour_sent: int
    max_per_hour: int
    window: str
    in_window: bool
    paused_until: datetime | None
    paused_reason: str | None
    hh_cap: int = HH_DAILY_CAP
    blocked_reasons: list[str] = field(default_factory=list)

    @property
    def remaining_day(self) -> int:
        return max(0, min(self.max_per_day, self.hh_cap) - self.today_sent)

    @property
    def remaining_hour(self) -> int:
        return max(0, self.max_per_hour - self.hour_sent)

    @property
    def can_send(self) -> bool:
        return not self.blocked_reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "today_sent": self.today_sent,
            "max_per_day": self.max_per_day,
            "remaining_day": self.remaining_day,
            "hour_sent": self.hour_sent,
            "max_per_hour": self.max_per_hour,
            "remaining_hour": self.remaining_hour,
            "window": self.window,
            "in_window": self.in_window,
            "paused_until": self.paused_until.isoformat(timespec="seconds") if self.paused_until else None,
            "paused_reason": self.paused_reason,
            "hh_cap": self.hh_cap,
            "can_send": self.can_send,
            "blocked_reasons": self.blocked_reasons,
        }


async def sent_count_since(session: AsyncSession, since: datetime, until: datetime | None = None) -> int:
    cond = [Application.status == "sent", Application.sent_at >= since, Application.dry_run.is_(False)]
    if until is not None:
        cond.append(Application.sent_at < until)
    return int((await session.execute(select(func.count(Application.id)).where(and_(*cond)))).scalar() or 0)


async def limits_status(session: AsyncSession, settings: Settings, *, now: datetime | None = None) -> LimitsStatus:
    now = now or utcnow()
    day_start, day_end = local_day_bounds(settings, now)
    today = await sent_count_since(session, day_start, day_end)
    hour = await sent_count_since(session, now - timedelta(hours=1))
    state = await get_system_state(session)
    paused_until = as_utc(state.apply_paused_until)
    if paused_until is not None and paused_until <= now:
        state.apply_paused_until = None
        state.apply_paused_reason = None
        paused_until = None
    local_now = now.astimezone(_tz(settings))
    status = LimitsStatus(
        today_sent=today,
        max_per_day=settings.apply_max_per_day,
        hour_sent=hour,
        max_per_hour=settings.apply_max_per_hour,
        window=settings.apply_window,
        in_window=in_window(local_now, settings.apply_window),
        paused_until=paused_until,
        paused_reason=state.apply_paused_reason if paused_until else None,
    )
    if state.paused_reason:
        status.blocked_reasons.append(f"панель на паузе: {state.paused_reason}")
    if paused_until is not None:
        status.blocked_reasons.append(f"отклики приостановлены до {paused_until.astimezone(_tz(settings)):%d.%m %H:%M}")
    if not status.in_window:
        status.blocked_reasons.append(f"вне окна отправки {settings.apply_window}")
    if status.remaining_day <= 0:
        status.blocked_reasons.append("дневной лимит исчерпан")
    if status.remaining_hour <= 0:
        status.blocked_reasons.append("часовой лимит исчерпан")
    return status


async def employer_recent_count(session: AsyncSession, employer_id: str | None, days: int = 7) -> int:
    if not employer_id:
        return 0
    since = utcnow() - timedelta(days=days)
    stmt = (
        select(func.count(Application.id))
        .join(Vacancy, Vacancy.id == Application.vacancy_id)
        .where(
            Vacancy.employer_id == employer_id,
            Application.status == "sent",
            Application.dry_run.is_(False),  # a dry run is not a response the employer received
            Application.sent_at >= since,
        )
    )
    return int((await session.execute(stmt)).scalar() or 0)


# ---- validation -------------------------------------------------------------


async def validate_for_apply(
    session: AsyncSession, settings: Settings, vacancy: Vacancy, *, mode: str
) -> tuple[list[str], str | None]:
    """Returns (blocking problems, letter text)."""
    problems: list[str] = []
    if vacancy.archived:
        problems.append("вакансия в архиве")
    if vacancy.hidden:
        problems.append("вакансия скрыта")
    if vacancy.pipeline_status in ("sent", "already_applied"):
        problems.append("по вакансии уже есть отклик")
    if vacancy.pipeline_status == "queued":
        problems.append("вакансия уже в очереди")
    if vacancy.test_required and mode == "api":
        problems.append("вакансия требует тест — откликнуться можно только на hh.ru")
    if (vacancy.type_id == "direct" or vacancy.response_url) and mode == "api":
        problems.append("отклик на внешнем сайте работодателя — только вручную")
    existing = (
        await session.execute(
            select(Application).where(
                Application.vacancy_id == vacancy.id, Application.status.in_(["queued", "sending", "sent"])
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        problems.append(f"уже есть отклик #{existing.id} ({existing.status})")
    letter = await current_letter(session, vacancy.id)
    text = letter.text if letter else None
    problems += check_before_send(text, required=vacancy.response_letter_required)
    recent = await employer_recent_count(session, vacancy.employer_id)
    if recent >= settings.apply_max_per_employer_7d:
        limit = settings.apply_max_per_employer_7d
        problems.append(f"у этого работодателя уже {recent} откликов за 7 дней (лимит {limit})")
    return problems, text


# ---- queue (mode A) ---------------------------------------------------------


async def enqueue(
    session: AsyncSession, settings: Settings, vacancy_ids: list[int], *, autopilot: bool = False
) -> dict[str, Any]:
    await require_consent(session)
    state = await get_system_state(session)
    if state.apply_mode != "api":
        raise ApplyError(
            "Режим отклика — «Ассистент»: отправка через API недоступна. Используйте «Открыть отклик на hh.ru».",
            409,
            "mode",
        )
    resume_id = await resolve_resume_id(session)
    if not resume_id:
        raise ApplyError(
            "Не выбрано резюме для отклика: импортируйте резюме из hh.ru или укажите resume_id (Профиль)", 409, "resume"
        )
    queued: list[int] = []
    rejected: list[dict[str, Any]] = []
    for vid in vacancy_ids:
        vacancy = await session.get(Vacancy, vid)
        if vacancy is None:
            rejected.append({"vacancy_id": vid, "reason": "вакансия не найдена"})
            continue
        problems, text = await validate_for_apply(session, settings, vacancy, mode="api")
        if problems:
            rejected.append({"vacancy_id": vid, "name": vacancy.name, "reason": "; ".join(problems)})
            continue
        letter = await current_letter(session, vacancy.id)
        app_row = Application(
            vacancy_id=vacancy.id,
            resume_hh_id=resume_id,
            mode="api",
            status="queued",
            cover_letter_id=letter.id if letter else None,
            letter_text=text,
            prev_status=vacancy.pipeline_status,
            queued_at=utcnow(),
            autopilot=autopilot,
        )
        session.add(app_row)
        vacancy.pipeline_status = "queued"
        vacancy.pipeline_reason = None
        queued.append(vacancy.id)
        await session.flush()
        await audit(
            session,
            "apply",
            f"В очередь на отклик: «{vacancy.name}»",
            vacancy_id=vacancy.id,
            application_id=app_row.id,
            autopilot=autopilot,
        )
    return {"queued": queued, "rejected": rejected}


async def cancel_application(session: AsyncSession, app_row: Application) -> None:
    if app_row.status not in ("queued", "needs_manual", "failed"):
        raise ApplyError(f"Отклик в статусе {app_row.status} отменить нельзя", 409)
    vacancy = await session.get(Vacancy, app_row.vacancy_id)
    app_row.status = "cancelled"
    if vacancy is not None and vacancy.pipeline_status in ("queued", "needs_manual"):
        vacancy.pipeline_status = app_row.prev_status or "suitable"
    await audit(session, "apply", f"Отклик #{app_row.id} отменён", vacancy_id=app_row.vacancy_id)


async def retry_application(session: AsyncSession, app_row: Application) -> None:
    if app_row.status != "failed":
        raise ApplyError("Повторить можно только неудавшийся отклик", 409)
    vacancy = await session.get(Vacancy, app_row.vacancy_id)
    app_row.status = "queued"
    app_row.attempts = 0
    app_row.error_type = app_row.error_value = app_row.error_text = None
    app_row.queued_at = utcnow()
    if vacancy is not None:
        vacancy.pipeline_status = "queued"
    await audit(session, "apply", f"Отклик #{app_row.id} снова в очереди", vacancy_id=app_row.vacancy_id)


def negotiation_id_from_location(location: str | None) -> str | None:
    if not location:
        return None
    m = re.search(r"/negotiations/([^/?#]+)", location)
    return m.group(1) if m else None


async def _mark_sent(
    session: AsyncSession,
    app_row: Application,
    vacancy: Vacancy | None,
    *,
    negotiation_id: str | None,
    dry_run: bool = False,
    settings: Settings | None = None,
) -> None:
    app_row.status = "sent"
    app_row.sent_at = utcnow()
    app_row.hh_negotiation_id = negotiation_id
    app_row.dry_run = dry_run
    app_row.error_type = app_row.error_value = app_row.error_text = None
    if vacancy is not None:
        vacancy.pipeline_status = "sent"
        vacancy.pipeline_reason = "сухой прогон" if dry_run else None
    if settings is not None and not dry_run:
        await _after_real_send(session, settings, app_row, vacancy)


async def _resolve_bot_message(session: AsyncSession, vacancy_id: int, resolution: str) -> None:
    from app.services.tg_letters import mark_resolved

    await mark_resolved(session, vacancy_id, resolution)


async def _after_real_send(
    session: AsyncSession, settings: Settings, app_row: Application, vacancy: Vacancy | None
) -> None:
    """Counters and the `applied` notification for a real (non dry-run) send in any mode (M5)."""
    from app.services.counters import bump_counter
    from app.services.notify import emit_event

    field = "applies_assist_confirmed" if app_row.mode == "assist" else "applies_api"
    if app_row.mode in ("api", "assist"):
        await bump_counter(session, settings, field, moment=app_row.sent_at)
    await emit_event(
        session,
        settings,
        "applied",
        {
            "application_id": app_row.id,
            "vacancy_id": app_row.vacancy_id,
            "name": vacancy.name if vacancy is not None else f"#{app_row.vacancy_id}",
            "employer": vacancy.employer_name if vacancy is not None else None,
            "url": vacancy.alternate_url if vacancy is not None else None,
            "mode": app_row.mode,
            "mode_label": MODE_LABELS.get(app_row.mode, app_row.mode),
            "negotiation_id": app_row.hh_negotiation_id,
        },
    )
    await _resolve_bot_message(session, app_row.vacancy_id, "sent")


async def send_one(session: AsyncSession, settings: Settings, client: HHClient, app_row: Application) -> str:
    """Sends one queued application and returns its resulting status. Raises on captcha/auth to stop the loop."""
    vacancy = await session.get(Vacancy, app_row.vacancy_id)
    if vacancy is None:
        app_row.status = "failed"
        app_row.error_text = "вакансия удалена из базы"
        return app_row.status
    app_row.status = "sending"
    app_row.attempts += 1
    await session.flush()

    if settings.apply_dry_run:
        await _mark_sent(session, app_row, vacancy, negotiation_id=None, dry_run=True)
        await audit(
            session,
            "apply",
            f"[сухой прогон] отклик на «{vacancy.name}» не отправлен",
            vacancy_id=vacancy.id,
            application_id=app_row.id,
        )
        return app_row.status

    form: dict[str, Any] = {"vacancy_id": vacancy.hh_id, "resume_id": app_row.resume_hh_id or ""}
    if app_row.letter_text:
        form["message"] = app_row.letter_text
    try:
        resp = await client.request("POST", "/negotiations", data=form, multipart=True)
    except (HHCaptchaRequired, HHAuthError):
        app_row.status = "queued"
        raise
    except HHNegotiationError as exc:
        return await _handle_negotiation_error(session, settings, app_row, vacancy, exc)
    except HHBadArgument as exc:
        app_row.status = "failed"
        app_row.error_type = "bad_argument"
        app_row.error_value = exc.field[:100]
        hint = ""
        if "visibility" in exc.field:
            hint = " (hh требует новый параметр видимости резюме: required_applicant_visibility_id)"
        app_row.error_text = f"hh отклонил параметры: {exc.summary()}{hint}"
        await audit(
            session,
            "apply",
            f"Отклик на «{vacancy.name}» отклонён: {app_row.error_text}",
            level="error",
            vacancy_id=vacancy.id,
            application_id=app_row.id,
        )
        return app_row.status
    except HHNotFound as exc:
        app_row.status = "failed"
        app_row.error_type = "not_found"
        app_row.error_text = exc.summary()
        vacancy.pipeline_status = "filtered_out"
        vacancy.pipeline_reason = "вакансия недоступна (404 при отклике)"
        await audit(
            session, "apply", f"Отклик на «{vacancy.name}»: {exc.summary()}", level="warning", vacancy_id=vacancy.id
        )
        return app_row.status
    except HHError as exc:
        app_row.error_type = type(exc).__name__
        app_row.error_text = exc.summary()
        if app_row.attempts >= MAX_ATTEMPTS:
            app_row.status = "failed"
            message = f"Отклик на «{vacancy.name}» не удался после {app_row.attempts} попыток: {exc.summary()}"
            await audit(session, "apply", message, level="error", vacancy_id=vacancy.id, application_id=app_row.id)
        else:
            app_row.status = "queued"
            await audit(
                session,
                "apply",
                f"Отклик на «{vacancy.name}»: временная ошибка {exc.summary()}, повтор позже",
                level="warning",
                vacancy_id=vacancy.id,
                application_id=app_row.id,
            )
        return app_row.status

    if resp.status == 303:
        app_row.status = "needs_manual"
        app_row.response_url = resp.location
        vacancy.pipeline_status = "needs_manual"
        vacancy.pipeline_reason = "отклик на внешнем сайте работодателя"
        message = f"«{vacancy.name}»: отклик на внешнем сайте {resp.location}"
        await audit(session, "apply", message, vacancy_id=vacancy.id, application_id=app_row.id)
        return app_row.status
    negotiation_id = negotiation_id_from_location(resp.location)
    await _mark_sent(session, app_row, vacancy, negotiation_id=negotiation_id, settings=settings)
    await audit(
        session,
        "apply",
        f"Отклик отправлен: «{vacancy.name}»",
        vacancy_id=vacancy.id,
        application_id=app_row.id,
        negotiation_id=negotiation_id,
    )
    log.info("application_sent", vacancy=vacancy.hh_id, negotiation_id=negotiation_id)
    return app_row.status


async def _handle_negotiation_error(
    session: AsyncSession, settings: Settings, app_row: Application, vacancy: Vacancy, exc: HHNegotiationError
) -> str:
    value = exc.value
    app_row.error_type = "negotiations"
    app_row.error_value = value[:100]
    app_row.error_text = exc.summary()
    if value == "limit_exceeded":
        state = await get_system_state(session)
        state.apply_paused_until = next_day_resume_at(settings)
        state.apply_paused_reason = "лимит откликов hh.ru исчерпан"
        app_row.status = "queued"
        app_row.attempts = max(0, app_row.attempts - 1)
        await audit(
            session,
            "apply",
            "hh.ru: лимит откликов исчерпан — очередь на паузе до завтра",
            level="warning",
            vacancy_id=vacancy.id,
        )
        from app.services.notify import emit_event

        await emit_event(session, settings, "limit", {"until": state.apply_paused_until.isoformat()})
        return "paused"
    status, vacancy_status, text = NEGOTIATION_OUTCOMES.get(value, ("failed", None, f"hh отклонил отклик: {value}"))
    app_row.status = status
    app_row.error_text = text
    if status == "sent":
        app_row.mode = "external"
        app_row.sent_at = utcnow()
    if vacancy_status:
        vacancy.pipeline_status = vacancy_status
        vacancy.pipeline_reason = text
    await audit(
        session,
        "apply",
        f"«{vacancy.name}»: {text}",
        level="warning" if status != "sent" else "info",
        vacancy_id=vacancy.id,
        application_id=app_row.id,
        value=value,
    )
    return app_row.status


async def run_apply(runtime: Runtime, *, max_items: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Job body: sends queued applications one by one respecting limits, window, pauses and random delays.
    `dry_run=True` forces a dry run for this call only (the CLI default), whatever APPLY_DRY_RUN says."""
    settings = runtime.settings
    if dry_run and not settings.apply_dry_run:
        settings = settings.model_copy(update={"apply_dry_run": True})
    client = runtime.hh_client()
    summary: dict[str, Any] = {
        "sent": 0,
        "failed": 0,
        "needs_manual": 0,
        "retry_later": 0,
        "skipped": [],
        "stopped": None,
    }
    async with runtime.session_factory() as session:
        state = await get_system_state(session)
        if state.consent_accepted_at is None:
            summary["stopped"] = "нет согласия пользователя"
            return summary
        if state.apply_mode != "api":
            summary["stopped"] = "режим не api"
            return summary
        queue = (
            (
                await session.execute(
                    select(Application)
                    .where(Application.status == "queued")
                    .order_by(Application.queued_at, Application.id)
                )
            )
            .scalars()
            .all()
        )
        if not queue:
            return summary
        processed = 0
        first = True
        for app_row in queue:
            if max_items is not None and processed >= max_items:
                break
            limits = await limits_status(session, settings)
            if not limits.can_send:
                summary["stopped"] = "; ".join(limits.blocked_reasons)
                await session.commit()
                break
            if not first:
                lo = settings.apply_min_delay_sec
                delay = random.uniform(lo, max(lo, settings.apply_max_delay_sec))
                if delay > 0:
                    await asyncio.sleep(delay)
                # the user may have cancelled the row from the UI while we were sleeping
                await session.refresh(app_row)
                if app_row.status != "queued":
                    summary["skipped"].append(app_row.id)
                    continue
            first = False
            try:
                result = await send_one(session, settings, client, app_row)
            except (HHCaptchaRequired, HHAuthError) as exc:
                summary["stopped"] = exc.summary()
                if isinstance(exc, HHCaptchaRequired):
                    await set_pause(session, "captcha", runtime.captcha_url)
                await session.commit()
                break
            processed += 1
            if result == "sent":
                summary["sent"] += 1
            elif result == "failed":
                summary["failed"] += 1
            elif result == "needs_manual":
                summary["needs_manual"] += 1
            elif result == "queued":
                summary["retry_later"] += 1
            elif result == "paused":
                summary["stopped"] = "лимит hh.ru исчерпан"
                await session.commit()
                break
            await session.commit()
        from app.services.counters import bump_counter

        await bump_counter(session, settings, "hh_requests", client.requests_made)
        await session.commit()
    summary["hh_requests"] = client.requests_made
    return summary


async def autopilot_enqueue(session: AsyncSession, settings: Settings) -> list[int]:
    """Queues suitable vacancies with a ready letter when the autopilot is on."""
    if not settings.apply_autopilot:
        return []
    state = await get_system_state(session)
    if state.apply_mode != "api" or state.consent_accepted_at is None:
        return []
    rows = (
        (
            await session.execute(
                select(Vacancy).where(
                    Vacancy.pipeline_status == "suitable",
                    Vacancy.hidden.is_(False),
                    Vacancy.score >= settings.apply_min_score_autopilot,
                    or_(Vacancy.snoozed_until.is_(None), Vacancy.snoozed_until <= utcnow()),
                )
            )
        )
        .scalars()
        .all()
    )
    # only with a ready letter — Claude's or the user's edit, without [[УТОЧНИТЬ]]; a template or no
    # letter at all waits for the user even if the vacancy itself does not require one
    letters = await current_letters_map(session, [r.id for r in rows])
    ready = [r.id for r in rows if letter_status(letters.get(r.id)) in ("ready", "edited")]
    if not ready:
        return []
    try:
        result = await enqueue(session, settings, ready, autopilot=True)
    except ApplyError:
        return []
    return list(result["queued"])


# ---- assist mode (mode B) ---------------------------------------------------


async def assist_open(session: AsyncSession, settings: Settings, vacancy: Vacancy) -> Application:
    await require_consent(session)
    problems, text = await validate_for_apply(session, settings, vacancy, mode="assist")
    problems = [p for p in problems if not p.startswith("вакансия уже в очереди")]
    if problems:
        raise ApplyError("; ".join(problems), 409, "validation")
    letter = await current_letter(session, vacancy.id)
    existing = (
        await session.execute(
            select(Application).where(
                Application.vacancy_id == vacancy.id, Application.status == "needs_manual", Application.mode == "assist"
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.letter_text = text
        existing.cover_letter_id = letter.id if letter else None
        return existing
    app_row = Application(
        vacancy_id=vacancy.id,
        resume_hh_id=await resolve_resume_id(session),
        mode="assist",
        status="needs_manual",
        cover_letter_id=letter.id if letter else None,
        letter_text=text,
        prev_status=vacancy.pipeline_status,
        queued_at=utcnow(),
    )
    session.add(app_row)
    vacancy.pipeline_status = "needs_manual"
    vacancy.pipeline_reason = "откройте страницу отклика на hh.ru и подтвердите отправку"
    await session.flush()
    await audit(
        session,
        "apply",
        f"Открыта страница отклика на hh.ru: «{vacancy.name}»",
        vacancy_id=vacancy.id,
        application_id=app_row.id,
    )
    return app_row


async def assist_confirm(
    session: AsyncSession, app_row: Application, *, sent: bool, settings: Settings | None = None
) -> None:
    if app_row.mode != "assist" or app_row.status not in ("needs_manual", "sent"):
        raise ApplyError("Это не ручной отклик, ожидающий подтверждения", 409)
    vacancy = await session.get(Vacancy, app_row.vacancy_id)
    if sent:
        already = app_row.status == "sent"
        app_row.status = "sent"
        app_row.sent_at = app_row.sent_at or utcnow()
        app_row.confirmed_by_user = True
        if vacancy is not None:
            vacancy.pipeline_status = "sent"
            vacancy.pipeline_reason = None
        name = vacancy.name if vacancy else str(app_row.vacancy_id)
        message = f"Пользователь подтвердил отклик на hh.ru: «{name}»"
        await audit(session, "apply", message, vacancy_id=app_row.vacancy_id, application_id=app_row.id)
        if not already and settings is not None:
            await _after_real_send(session, settings, app_row, vacancy)
    else:
        app_row.status = "cancelled"
        if vacancy is not None and vacancy.pipeline_status == "needs_manual":
            vacancy.pipeline_status = app_row.prev_status or "suitable"
            vacancy.pipeline_reason = None
        name = vacancy.name if vacancy else str(app_row.vacancy_id)
        await audit(
            session,
            "apply",
            f"Ручной отклик не отправлен: «{name}»",
            vacancy_id=app_row.vacancy_id,
            application_id=app_row.id,
        )


# ---- withdraw ---------------------------------------------------------------


async def withdraw(session: AsyncSession, client: HHClient, app_row: Application) -> bool:
    """Hides an application on hh.ru (DELETE /negotiations/active/{nid}); works only if hh still exposes the method."""
    if (
        app_row.status != "sent"
        or not app_row.hh_negotiation_id
        or app_row.hh_negotiation_id.startswith("manual-")
        or app_row.dry_run
    ):
        raise ApplyError("Отозвать можно только отправленный через API отклик с известным id", 409)
    caps = read_capabilities(await get_system_state(session))
    if caps.get("negotiations_delete") == "unavailable":
        raise ApplyError("По данным предыдущих попыток hh.ru не позволяет отзывать отклики через API", 409)
    try:
        await client.request("DELETE", f"/negotiations/active/{app_row.hh_negotiation_id}")
    except HHNotFound as exc:
        raise ApplyError(f"hh.ru: {exc.summary()}", 502) from exc
    except HHError as exc:
        raise ApplyError(f"hh.ru: {exc.summary()}", 502) from exc
    app_row.status = "cancelled"
    app_row.error_text = "отклик отозван (скрыт) на hh.ru"
    vacancy = await session.get(Vacancy, app_row.vacancy_id)
    if vacancy is not None:
        vacancy.pipeline_status = "skipped"
        vacancy.pipeline_reason = "отклик отозван"
    await audit(session, "apply", f"Отклик #{app_row.id} отозван на hh.ru", vacancy_id=app_row.vacancy_id)
    return True


# ---- listing ----------------------------------------------------------------


STATUS_LABELS = {
    "queued": "в очереди",
    "sending": "отправляется",
    "sent": "отправлен",
    "failed": "не удался",
    "needs_manual": "нужно действие",
    "cancelled": "отменён",
}
MODE_LABELS = {"api": "API", "assist": "вручную на hh.ru", "external": "вне панели"}


def application_row(
    app_row: Application, vacancy: Vacancy | None, negotiation: Negotiation | None = None
) -> dict[str, Any]:
    def iso(dt: datetime | None) -> str | None:
        d = as_utc(dt)
        return d.isoformat(timespec="seconds") if d else None

    from app.services.sync import negotiation_row

    return {
        "negotiation": negotiation_row(negotiation),
        "id": app_row.id,
        "vacancy_id": app_row.vacancy_id,
        "vacancy": (
            {
                "id": vacancy.id,
                "hh_id": vacancy.hh_id,
                "name": vacancy.name,
                "employer_name": vacancy.employer_name,
                "alternate_url": vacancy.alternate_url,
                "apply_alternate_url": vacancy.apply_alternate_url,
                "pipeline_status": vacancy.pipeline_status,
                "score": vacancy.score,
            }
            if vacancy
            else None
        ),
        "mode": app_row.mode,
        "mode_label": MODE_LABELS.get(app_row.mode, app_row.mode),
        "status": app_row.status,
        "status_label": STATUS_LABELS.get(app_row.status, app_row.status),
        "letter_text": app_row.letter_text,
        "queued_at": iso(app_row.queued_at),
        "sent_at": iso(app_row.sent_at),
        "hh_negotiation_id": app_row.hh_negotiation_id,
        "negotiation_url": (
            f"https://hh.ru/applicant/negotiations/item?id={app_row.hh_negotiation_id}"
            if app_row.hh_negotiation_id and not app_row.hh_negotiation_id.startswith("manual-")
            else None
        ),
        "response_url": app_row.response_url,
        "error_type": app_row.error_type,
        "error_value": app_row.error_value,
        "error_text": app_row.error_text,
        "attempts": app_row.attempts,
        "confirmed_by_user": app_row.confirmed_by_user,
        "dry_run": app_row.dry_run,
        "autopilot": app_row.autopilot,
        "created_at": iso(app_row.created_at),
    }


STAGES = ("sent", "viewed", "invitation", "discard", "hidden", "no_answer", "unread")
# column sorts of the «Отклики» table; `status` ranks the visible badge (hh stage for sent rows, else local status)
SORTS = ("sent", "vacancy", "employer", "viewed", "status", "updated", "mode", "score")
_STATUS_RANK = {
    "invitation": 1,
    "viewed": 2,
    "response": 3,
    "sent": 4,
    "needs_manual": 5,
    "queued": 6,
    "failed": 7,
    "cancelled": 8,
    "discard": 9,
    "hidden": 10,
}


def _sort_key(sort: str) -> Any:
    if sort == "vacancy":
        return func.lower(Vacancy.name)
    if sort == "employer":
        return func.lower(Vacancy.employer_name)
    if sort == "viewed":
        return Negotiation.first_viewed_at
    if sort == "updated":
        return func.coalesce(Negotiation.hh_updated_at, Application.sent_at)
    if sort == "mode":
        return Application.mode
    if sort == "score":
        return Vacancy.score
    if sort == "status":
        badge = case(
            (
                and_(Application.status == "sent", Negotiation.id.isnot(None)),
                case(
                    (Negotiation.state_id == "invitation", "invitation"),
                    (Negotiation.state_id == "discard", "discard"),
                    (Negotiation.hidden.is_(True), "hidden"),
                    (Negotiation.viewed_by_opponent.is_(True), "viewed"),
                    else_="response",
                ),
            ),
            else_=Application.status,
        )
        return case(*[(badge == k, v) for k, v in _STATUS_RANK.items()], else_=99)
    return func.coalesce(Application.sent_at, Application.queued_at, Application.created_at)


def _stage_condition(stage: str, *, days: int, now: datetime) -> Any:
    """Negotiation-based filters of the «Отклики» tab (F-8.9.4). `no_answer`: sent > N days ago, no reaction."""
    if stage == "sent":
        return Application.status == "sent"
    if stage == "viewed":
        return and_(Application.status == "sent", Negotiation.viewed_by_opponent.is_(True))
    if stage == "invitation":
        invited = or_(Negotiation.state_id == "invitation", Negotiation.invited_at.is_not(None))
        return and_(Application.status == "sent", invited)
    if stage == "discard":
        discarded = or_(Negotiation.state_id == "discard", Negotiation.discarded_at.is_not(None))
        return and_(Application.status == "sent", discarded)
    if stage == "hidden":
        return or_(Negotiation.state_id == "hidden", Negotiation.hidden.is_(True))
    if stage == "unread":
        return or_(Negotiation.has_updates.is_(True), Negotiation.unread_count > 0)
    if stage == "no_answer":
        threshold = now - timedelta(days=max(1, days))
        return and_(
            Application.status == "sent",
            Application.dry_run.is_(False),
            Application.sent_at < threshold,
            or_(
                Negotiation.id.is_(None),
                and_(
                    Negotiation.invited_at.is_(None),
                    Negotiation.discarded_at.is_(None),
                    Negotiation.last_employer_message_at.is_(None),
                    Negotiation.state_id.not_in(("invitation", "discard")),
                ),
            ),
        )
    return None


async def list_applications(
    session: AsyncSession,
    *,
    status: str | None = None,
    mode: str | None = None,
    stage: str | None = None,
    days: int = 7,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    page: int = 0,
    per_page: int = 50,
    sort: str = "sent",
    order: str = "desc",
) -> dict[str, Any]:
    now = utcnow()
    # the ids of the page first (the sort may read the vacancy name or score), then the rows of those ids: sorting
    # the full rows made SQLite carry every ~46 KB vacancy of the list through the sorter
    stmt = (
        select(Application.id)
        .select_from(Application)
        .join(Vacancy, Vacancy.id == Application.vacancy_id, isouter=True)
        .join(Negotiation, Negotiation.hh_id == Application.hh_negotiation_id, isouter=True)
    )
    count_stmt = (
        select(func.count(Application.id))
        .select_from(Application)
        .join(Negotiation, Negotiation.hh_id == Application.hh_negotiation_id, isouter=True)
    )
    conds: list[Any] = []
    if status:
        conds.append(Application.status.in_(status.split(",")))
    if mode:
        conds.append(Application.mode == mode)
    if stage and stage in STAGES:
        cond = _stage_condition(stage, days=days, now=now)
        if cond is not None:
            conds.append(cond)
    if date_from is not None:
        conds.append(func.coalesce(Application.sent_at, Application.queued_at, Application.created_at) >= date_from)
    if date_to is not None:
        conds.append(func.coalesce(Application.sent_at, Application.queued_at, Application.created_at) < date_to)
    for c in conds:
        stmt = stmt.where(c)
        count_stmt = count_stmt.where(c)
    total = int((await session.execute(count_stmt)).scalar() or 0)
    sort = sort if sort in SORTS else "sent"
    key = _sort_key(sort)
    direction = asc if order == "asc" else desc
    # empty cells (never viewed, no hh data) always sink to the bottom, whatever the direction
    stmt = stmt.order_by(direction(key).nulls_last(), desc(Application.id))
    page_ids = list((await session.execute(stmt.offset(page * per_page).limit(per_page))).scalars())
    found = (
        await session.execute(
            select(Application, Vacancy, Negotiation)
            .join(Vacancy, Vacancy.id == Application.vacancy_id, isouter=True)
            .join(Negotiation, Negotiation.hh_id == Application.hh_negotiation_id, isouter=True)
            .where(Application.id.in_(page_ids))
        )
    ).all()
    by_id = {a.id: (a, v, n) for a, v, n in found}
    rows = [by_id[i] for i in page_ids if i in by_id]
    count_stmt2 = select(Application.status, func.count(Application.id)).group_by(Application.status)
    count_rows = await session.execute(count_stmt2)
    counts = {str(k): int(v) for k, v in count_rows.all()}
    # stage counters for the filter chips (over all applications): one pass, a conditional sum per stage
    stage_sums = [
        func.coalesce(func.sum(case((_stage_condition(name, days=days, now=now), 1), else_=0)), 0) for name in STAGES
    ]
    sums = (
        await session.execute(
            select(*stage_sums)
            .select_from(Application)
            .join(Negotiation, Negotiation.hh_id == Application.hh_negotiation_id, isouter=True)
        )
    ).one()
    stage_counts = {name: int(value) for name, value in zip(STAGES, sums, strict=True)}
    return {
        "items": [application_row(a, v, n) for a, v, n in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "counts": counts,
        "stage_counts": stage_counts,
    }
