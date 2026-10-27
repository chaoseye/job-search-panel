"""Vacancies added by hand: the user pastes hh.ru links, the panel fetches the cards, puts them into the pipeline
with `user_override` (rules and the prescore threshold do not filter them) and builds a packet for /hh-batch."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.db.models import Vacancy
from app.db.session import release_writes
from app.hh.client import HHClient
from app.hh.errors import HHError, HHNotFound
from app.hh.vacancy import HH_VACANCY_LINK, normalize_vacancy, storable_full
from app.services.audit import audit
from app.services.scanner import _json, apply_data_to_row, build_context, context_for, process_vacancy

# a link of an hh site carries any id; a bare number must look like an hh id to be picked up: 5–10 digits
# (ids are 9 digits now; 11 digits is a phone number «79161234567», not a vacancy)
_BARE_RE = re.compile(r"^(\d{5,10})$")
FINAL_STATUSES = ("sent", "queued", "already_applied")


@dataclass
class AddOutcome:
    added: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    batch_ids: list[str] = field(default_factory=list)


def parse_vacancy_ids(text: str) -> list[str]:
    """Links of hh sites (any subdomain), bare ids; order kept, duplicates dropped."""
    out: list[str] = []
    for token in re.split(r"[\s,;]+", text.strip()):
        if not token:
            continue
        m = HH_VACANCY_LINK.search(token) or _BARE_RE.match(token)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


async def add_vacancies(
    session: AsyncSession,
    settings: Settings,
    client: HHClient,
    text: str,
    *,
    profile_id: int | None = None,
    create_packet: bool = True,
) -> AddOutcome:
    outcome = AddOutcome()
    ids = parse_vacancy_ids(text)
    if not ids:
        outcome.errors.append(
            {"url": text[:80], "error": "не нашёл id вакансии: нужна ссылка hh.ru вида https://hh.ru/vacancy/123456"}
        )
        return outcome
    ctx = await build_context(session, client, settings, profile_id=profile_id)
    ready: list[int] = []
    for hh_id in ids:
        await release_writes(session)  # the previous vacancy is committed before the next hh request
        try:
            raw = await client.public(f"/vacancies/{hh_id}")
        except HHNotFound:
            outcome.errors.append({"url": hh_id, "error": "вакансия не найдена на hh.ru (удалена или в архиве)"})
            continue
        except HHError as exc:
            outcome.errors.append({"url": hh_id, "error": f"hh.ru: {exc.summary()}"})
            continue
        if not isinstance(raw, dict):
            outcome.errors.append({"url": hh_id, "error": "неожиданный ответ hh.ru"})
            continue
        data = normalize_vacancy(raw, full=True)
        if data.archived:
            outcome.errors.append({"url": hh_id, "error": f"«{data.name}» в архиве"})
            continue
        row = (await session.execute(select(Vacancy).where(Vacancy.hh_id == hh_id))).scalar_one_or_none()
        created = row is None
        if row is None:
            row = Vacancy(
                hh_id=hh_id,
                first_seen_at=utcnow(),
                pipeline_status="new",
                source_search_ids=",",
                selection_profile_id=profile_id,
            )
            session.add(row)
        elif row.pipeline_status in FINAL_STATUSES:
            outcome.errors.append({"url": hh_id, "error": f"«{row.name}»: уже есть отклик или в очереди на отправку"})
            continue
        elif row.pipeline_status == "awaiting_claude":
            outcome.errors.append({"url": hh_id, "error": f"«{row.name}» уже в пакете для Claude"})
            continue
        row_ctx = await context_for(session, client, settings, ctx, row.selection_profile_id)
        apply_data_to_row(row, data, row_ctx.rules.currency_rates)
        row.raw_full_json = _json(storable_full(raw))
        row.full_fetched_at = utcnow()
        row.full_fetch_errors = 0
        row.last_seen_at = utcnow()
        row.user_override = True  # the user asked for it: rules and the threshold do not filter it out
        row.hidden = False
        row.batch_id = None
        row.pipeline_status = "new"
        await session.flush()
        status = await process_vacancy(session, client, row, row_ctx)
        row.pipeline_reason = "добавлена пользователем по ссылке"
        await audit(
            session,
            "vacancies",
            f"Добавлена по ссылке: «{row.name}» ({'новая' if created else 'уже была в базе'}), prescore {row.prescore}",
            vacancy_id=row.id,
        )
        ready.append(row.id)
        outcome.added.append(
            {
                "id": row.id,
                "hh_id": hh_id,
                "name": row.name,
                "employer": row.employer_name,
                "prescore": row.prescore,
                "status": status,
                "created": created,
                "url": row.alternate_url,
            }
        )
    await session.flush()
    if ready and create_packet:
        from app.exchange.packets import PacketError, build_batches

        try:
            batches = await build_batches(session, settings, vacancy_ids=ready, include_revisions=False)
            outcome.batch_ids = [b.batch_id for b in batches]
        except PacketError as exc:
            outcome.errors.append({"url": "", "error": str(exc)})
    return outcome
