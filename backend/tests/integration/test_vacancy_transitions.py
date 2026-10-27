"""The user's actions on a vacancy from every pipeline status: the table below is the rule
book of «Пропустить», «Скрыть», «Вернуть в кандидаты», «В пакет для Claude» and «Переоценить». hh.ru answers 503 to everything: no action may need it (reprocess works on the
stored card)."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select

from app.config import Settings
from app.db.base import utcnow
from app.db.models import Application, ClaudeBatch, Vacancy, VacancyEvaluation
from app.main import app
from app.runtime import Runtime
from tests.unit.test_vacancy_norm import load_full, short_item

PENDING_BATCH = "20260924_0100_abcd"

# key → (pipeline_status, prescore, claude score, application (mode, status))
CASES: dict[str, tuple[str, int | None, int | None, tuple[str, str] | None]] = {
    "new": ("new", None, None, None),
    "filtered_out": ("filtered_out", None, None, None),
    "prescored": ("prescored", 60, None, None),
    "below_prescore": ("not_suitable", 20, None, None),
    "needs_manual": ("needs_manual", 55, None, None),
    "skipped": ("skipped", 60, None, None),
    "hidden": ("hidden", 60, None, None),
    "watched": ("watched", None, None, None),
    "already_applied": ("already_applied", None, None, None),
    "awaiting_claude": ("awaiting_claude", 60, None, None),
    "suitable": ("suitable", 60, 85, None),
    "maybe": ("maybe", 60, 60, None),
    "rejected_by_claude": ("not_suitable", 60, 30, None),
    "queued": ("queued", 60, 85, ("api", "queued")),
    "sent": ("sent", 60, 85, ("assist", "sent")),
    "assist_open": ("needs_manual", 60, 85, ("assist", "needs_manual")),
}
WITH_RESPONSE = {"queued", "sent", "assist_open"}
# where «Вернуть в кандидаты» leads (revived_status: the application's stage, the pending packet, the Claude verdict,
# else prescored with a prescore / new without one)
RESTORED = {
    "new": "new",
    "filtered_out": "new",
    "prescored": "prescored",
    "below_prescore": "prescored",
    "needs_manual": "prescored",
    "skipped": "prescored",
    "hidden": "prescored",
    "watched": "new",
    "awaiting_claude": "awaiting_claude",
    "suitable": "suitable",
    "maybe": "maybe",
    "rejected_by_claude": "not_suitable",
    "queued": "queued",
    "sent": "sent",
    "assist_open": "needs_manual",
}
# «Переоценить» leaves these as they are: a response, a packet, a Claude verdict (scanner.reprocess_keeps_status)
KEPT_ON_REPROCESS = {"already_applied", "awaiting_claude", "suitable", "maybe", "rejected_by_claude", *WITH_RESPONSE}


@pytest.fixture
async def api(settings: Settings):
    runtime = await Runtime.create(settings.model_copy(update={"batch_auto_create": False}))
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


async def _seed(runtime: Runtime) -> dict[str, int]:
    """One vacancy per case, with the rows that make the case: a pending packet, an evaluation, an application."""
    full = load_full()
    ids: dict[str, int] = {}
    async with runtime.session_factory() as session:
        session.add(ClaudeBatch(batch_id=PENDING_BATCH, packet_path="batch.json", status="pending", tasks_count=1))
        for n, (key, (status, prescore, claude, application)) in enumerate(CASES.items(), start=1):
            hh_id = str(900000 + n)
            name = f"Python-разработчик {key}"  # distinct names: the duplicate check must not pair them up
            row = Vacancy(
                hh_id=hh_id,
                name=name,
                employer_id=f"e{n}",
                employer_name=f"Компания {n}",
                pipeline_status=status,
                prescore=prescore,
                score=claude,
                hidden=status == "hidden",
                batch_id=PENDING_BATCH if status == "awaiting_claude" else None,
                raw_short_json=json.dumps(short_item(id=hh_id, name=name), ensure_ascii=False),
                raw_full_json=json.dumps({**full, "id": hh_id, "name": name}, ensure_ascii=False),
                full_fetched_at=utcnow(),
                last_seen_at=utcnow() - timedelta(hours=1),
            )
            session.add(row)
            await session.flush()
            ids[key] = row.id
            if claude is not None:
                verdict = "apply" if claude >= 70 else "maybe" if claude >= 50 else "skip"
                session.add(
                    VacancyEvaluation(
                        vacancy_id=row.id, batch_id="20260920_1200_0000", score=claude, verdict=verdict, is_current=True
                    )
                )
            if application is not None:
                mode, app_status = application
                session.add(Application(vacancy_id=row.id, mode=mode, status=app_status))
        await session.commit()
    return ids


async def _status(runtime: Runtime, vid: int) -> tuple[str, bool]:
    async with runtime.session_factory() as session:
        row = await session.get(Vacancy, vid)
        assert row is not None
        return row.pipeline_status, row.hidden


async def _apps(runtime: Runtime, vid: int) -> list[str]:
    async with runtime.session_factory() as session:
        return list((await session.execute(select(Application.status).where(Application.vacancy_id == vid))).scalars())


def _post(client: httpx.AsyncClient, vid: int, action: str) -> Any:
    if action == "skip":
        return client.post(f"/api/vacancies/{vid}/skip", json={"reason": "не интересно"})
    if action == "reprocess":
        return client.post(f"/api/vacancies/{vid}/reprocess", params={"refetch": "false"})
    return client.post(f"/api/vacancies/{vid}/{action}")


@pytest.mark.parametrize("action", ["skip", "hide", "restore", "to-batch", "reprocess"])
@respx.mock(assert_all_called=False)
async def test_actions_from_every_status(api, respx_mock: respx.MockRouter, action: str) -> None:
    respx_mock.route().mock(return_value=httpx.Response(503, json={"errors": [{"type": "service_unavailable"}]}))
    client, runtime = api
    ids = await _seed(runtime)
    problems: list[str] = []
    for key, vid in ids.items():
        before, _ = await _status(runtime, vid)
        r = await _post(client, vid, action)
        after, hidden = await _status(runtime, vid)
        apps = await _apps(runtime, vid)
        expected_code = 200
        if action == "restore" and key == "already_applied":
            expected_code = 409
        if action == "to-batch" and (key in WITH_RESPONSE or key == "already_applied"):
            expected_code = 409
        if r.status_code != expected_code:
            problems.append(f"{action} {key}: HTTP {r.status_code} (ждали {expected_code}) {r.text[:150]}")
            continue
        if expected_code == 409:
            if after != before:
                problems.append(f"{action} {key}: отказ, но статус {before} → {after}")
            continue
        if action == "skip":
            ok = after == "skipped" and ("queued" not in apps)
        elif action == "hide":
            ok = after == "hidden" and hidden and ("queued" not in apps)
        elif action == "restore":
            ok = after == RESTORED[key] and not hidden
        elif action == "to-batch":
            ok = after == ("awaiting_claude" if key == "awaiting_claude" else "prescored") and not hidden
        else:  # reprocess on the stored card
            ok = after == before if key in KEPT_ON_REPROCESS else after not in ("sent", "queued", "already_applied")
        if not ok:
            problems.append(f"{action} {key}: {before} → {after} (hidden={hidden}, отклики {apps})")
        # a response that was sent or is waiting for the user is never cancelled by a vacancy action
        if key in ("sent", "assist_open") and apps != [CASES[key][3][1]]:  # type: ignore[index]
            problems.append(f"{action} {key}: отклик изменился: {apps}")
        if key == "queued" and action in ("skip", "hide") and apps != ["cancelled"]:
            problems.append(f"{action} {key}: отклик в очереди не отменён: {apps}")
    assert problems == [], "\n".join(problems)


@respx.mock(assert_all_called=False)
async def test_reprocess_refreshes_the_card_of_a_sent_vacancy_without_touching_its_status(
    api, respx_mock: respx.MockRouter
) -> None:
    client, runtime = api
    ids = await _seed(runtime)
    vid = ids["sent"]
    async with runtime.session_factory() as session:
        row = await session.get(Vacancy, vid)
        assert row is not None
        hh_id = row.hh_id
    fresh = {**load_full(), "id": hh_id, "name": "Python-разработчик (обновлено)", "archived": True}
    respx_mock.get(f"https://api.hh.ru/vacancies/{hh_id}").mock(return_value=httpx.Response(200, json=fresh))
    respx_mock.route().mock(return_value=httpx.Response(503, json={"errors": [{"type": "service_unavailable"}]}))

    r = await client.post(f"/api/vacancies/{vid}/reprocess")  # refetch=true by default
    assert r.status_code == 200, r.text
    async with runtime.session_factory() as session:
        row = await session.get(Vacancy, vid)
        assert row is not None
        # the card is fresh (an archived vacancy would fail the rules), the status of the sent response is kept
        assert (row.pipeline_status, row.name, row.archived) == ("sent", "Python-разработчик (обновлено)", True)
