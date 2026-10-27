"""«История действий» in the vacancy card: the journal records of that vacancy and no other. Taking them by
LIKE '%"vacancy_id": 1%' would give №1 also the records of 10–19, 100–199, … — and of the newest 30 shown, all could be
someone else's."""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import delete

from app.config import Settings
from app.db.models import AuditLog, Vacancy
from app.main import app
from app.runtime import Runtime
from app.services.audit import audit


@pytest.fixture
async def api(settings: Settings):
    runtime = await Runtime.create(settings.model_copy(update={"batch_auto_create": False}))
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


async def _add_vacancies(runtime: Runtime, *ids: int) -> None:
    async with runtime.session_factory() as session:
        for vid in ids:
            session.add(Vacancy(id=vid, hh_id=str(700000 + vid), name=f"Вакансия {vid}", pipeline_status="prescored"))
        await session.commit()


async def _history(client: httpx.AsyncClient, vid: int) -> list[str]:
    r = await client.get(f"/api/vacancies/{vid}")
    assert r.status_code == 200, r.text
    return [h["message"] for h in r.json()["history"]]


async def test_card_history_holds_only_the_records_of_its_vacancy(api) -> None:
    client, runtime = api
    await _add_vacancies(runtime, 1, 10, 12)
    # the actions write the journal themselves: {"vacancy_id": 1} and {"vacancy_id": 1, "reason": "…"}
    for vid in (1, 10, 12):
        assert (await client.post(f"/api/vacancies/{vid}/snooze", json={"days": 3})).status_code == 200
    assert (await client.post("/api/vacancies/1/skip", json={"reason": "далеко"})).status_code == 200
    async with runtime.session_factory() as session:
        # newer than the records of №1 and more than the card shows (30): the old LIKE showed only these
        for n in range(31):
            vid = 10 if n % 2 == 0 else 12
            await audit(session, "vacancies", f"запись вакансии {vid}", vacancy_id=vid)
        # json_extract fails the whole query on malformed JSON: the card must not
        session.add(AuditLog(area="vacancies", message="битый контекст", context_json='{"vacancy_id": 1, "reason"'))
        await session.commit()

    assert await _history(client, 1) == ["Пропущена «Вакансия 1»", "Отложена «Вакансия 1» на 3 дн."]
    assert await _history(client, 10) == ["запись вакансии 10"] * 16 + ["Отложена «Вакансия 10» на 3 дн."]
    assert await _history(client, 12) == ["запись вакансии 12"] * 15 + ["Отложена «Вакансия 12» на 3 дн."]


async def test_records_of_a_deleted_vacancy_do_not_pass_to_the_next_one_with_its_number(api) -> None:
    """SQLite gives a new row the number of a deleted last one: deleted demo vacancies would leave their records to
    the first real №1–4."""
    client, runtime = api
    async with runtime.session_factory() as session:
        session.add(Vacancy(id=1, hh_id="700000", name="Демо-вакансия", pipeline_status="prescored"))
        await session.commit()
    assert (await client.post("/api/vacancies/1/snooze", json={"days": 3})).status_code == 200
    async with runtime.session_factory() as session:
        await session.execute(delete(Vacancy))
        await session.commit()
    await _add_vacancies(runtime, 1)
    assert await _history(client, 1) == []
    assert (await client.post("/api/vacancies/1/skip", json={"reason": "далеко"})).status_code == 200
    assert await _history(client, 1) == ["Пропущена «Вакансия 1»"]
