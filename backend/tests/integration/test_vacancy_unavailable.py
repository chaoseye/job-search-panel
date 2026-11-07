"""«Вакансия недоступна»: the response page on hh.ru says «Вам недоступна эта вакансия» — the user marks it from the
assistant's panel or the board. The open response is cancelled (it could not go), the vacancy is hidden with the
reason, leaves the candidates, the board and the bot, comes back with «Вернуть», and a repost of it is a new vacancy,
not a duplicate. The nightly check of closed vacancies cancels such a response by itself (otherwise the open page of
the assistant would stay in «Нужно действие» for weeks)."""

from __future__ import annotations

from datetime import timedelta

import httpx
import respx
from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Application, Vacancy, VacancyTgMessage
from app.services import freshness
from app.services import vacancies as vs
from app.services.freshness import check_closed
from app.services.scanner import find_duplicate
from app.services.tg_letters import remember
from tests.integration.test_api_m2 import _full
from tests.integration.test_api_m4 import BASE, _consent, _seed, api  # noqa: F401

__all__ = ["api"]


async def _application(runtime, app_id: int) -> Application:  # type: ignore[no-untyped-def]
    async with runtime.session_factory() as session:
        row = await session.get(Application, app_id)
        assert row is not None
        return row


@respx.mock(assert_all_called=False)
async def test_an_unavailable_vacancy_cancels_the_open_response_and_is_hidden(
    api, respx_mock: respx.MockRouter
) -> None:
    client, runtime = api
    ids = await _seed(client, runtime, respx_mock, ["1", "2"])
    await client.put("/api/settings", json={"apply_mode_override": "assist"})
    await _consent(client)
    vid = ids["1"]
    opened = (await client.post("/api/applications/assist/open", json={"vacancy_id": vid})).json()
    assert opened["status"] == "needs_manual"
    async with runtime.session_factory() as session:  # its letter went to the bot too
        await remember(session, vacancy_id=vid, chat_id="42", message_id=7, notification_id=None)
        await session.commit()

    r = await client.post(f"/api/vacancies/{vid}/unavailable")
    assert r.status_code == 200, r.text
    card = r.json()
    assert (card["pipeline_status"], card["hidden"], card["cancelled_responses"]) == ("hidden", True, 1)
    assert card["pipeline_reason"] == vs.UNAVAILABLE_REASON
    assert any("Недоступна на hh.ru" in h["message"] for h in card["history"])
    app_row = await _application(runtime, opened["id"])
    assert (app_row.status, app_row.error_text) == ("cancelled", "не отправлен: вакансия недоступна на hh.ru")

    # nothing waits for the user any more: not on «Вакансии», not in «Отклики», not on the board, not in the bot
    hidden = {v["id"] for v in (await client.get("/api/vacancies", params={"group": "hidden"})).json()["items"]}
    assert vid in hidden
    for group in ("candidates", "needs_manual"):
        listed = {v["id"] for v in (await client.get("/api/vacancies", params={"group": group})).json()["items"]}
        assert vid not in listed, group
    assert (await client.get("/api/applications", params={"status": "needs_manual"})).json()["items"] == []
    confirm = (await client.get("/api/board/column/confirm")).json()["items"]
    assert vid not in {item["vacancy"]["id"] for item in confirm}
    async with runtime.session_factory() as session:
        message = (
            await session.execute(select(VacancyTgMessage).where(VacancyTgMessage.vacancy_id == vid))
        ).scalar_one()
        assert (message.status, message.resolution) == ("resolve", "hidden")

        # the employer reposts it: a new vacancy, not a duplicate of the hidden one
        old = await session.get(Vacancy, vid)
        assert old is not None
        repost = Vacancy(hh_id="777", name=old.name, employer_id=old.employer_id, employer_name=old.employer_name)
        session.add(repost)
        await session.flush()
        assert await find_duplicate(session, repost) is None
        await session.rollback()

    # «Вернуть» brings it back as a candidate (no Claude verdict here: prescored), the cancelled response stays
    r = await client.post(f"/api/vacancies/{vid}/restore")
    assert (r.json()["pipeline_status"], r.json()["hidden"]) == ("prescored", False)
    assert (await _application(runtime, opened["id"])).status == "cancelled"


@respx.mock(assert_all_called=False)
async def test_the_api_queue_is_cancelled_and_a_sent_response_refuses(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    ids = await _seed(client, runtime, respx_mock, ["1", "2"])
    await _consent(client)
    # mode api: the response waits in the queue — hh.ru would refuse it (invalid_vacancy), so it goes too
    queued = (await client.post("/api/applications/queue", json={"vacancy_ids": [ids["1"]]})).json()["queued"]
    assert len(queued) == 1
    r = await client.post(f"/api/vacancies/{ids['1']}/unavailable")
    assert r.status_code == 200 and r.json()["cancelled_responses"] == 1
    async with runtime.session_factory() as session:
        statuses = (
            await session.execute(select(Application.status).where(Application.vacancy_id == ids["1"]))
        ).scalars()
        assert list(statuses) == ["cancelled"]

    # a response the user confirmed as sent: the vacancy stays as it is
    await client.put("/api/settings", json={"apply_mode_override": "assist"})
    opened = (await client.post("/api/applications/assist/open", json={"vacancy_id": ids["2"]})).json()
    await client.post(f"/api/applications/{opened['id']}/confirm", json={"sent": True})
    r = await client.post(f"/api/vacancies/{ids['2']}/unavailable")
    assert r.status_code == 409 and "уже отправлен" in r.json()["detail"]
    card = (await client.get(f"/api/vacancies/{ids['2']}")).json()
    assert (card["pipeline_status"], card["hidden"]) == ("sent", False)
    assert (await client.post("/api/vacancies/999999/unavailable")).status_code == 404


@respx.mock(assert_all_called=False)
async def test_the_nightly_check_cancels_only_long_unconfirmed_responses_of_closed_vacancies(
    api, respx_mock: respx.MockRouter
) -> None:
    """A page opened in the evening may have been sent on hh.ru before the employer closed the vacancy that night —
    «Да, отправлено» must still work in the morning. Only a response nobody confirmed for
    UNCONFIRMED_GRACE_DAYS goes, with a reason that does not claim it was not sent."""
    client, runtime = api
    ids = await _seed(client, runtime, respx_mock, ["1", "2", "3", "4", "5"])
    await client.put("/api/settings", json={"apply_mode_override": "assist"})
    await _consent(client)
    opened = {}
    for hh_id in ids:
        r = await client.post("/api/applications/assist/open", json={"vacancy_id": ids[hh_id]})
        opened[hh_id] = r.json()["id"]
    long_ago = utcnow() - timedelta(days=freshness.UNCONFIRMED_GRACE_DAYS + 1)
    async with runtime.session_factory() as session:
        for hh_id, vid in ids.items():
            row = await session.get(Vacancy, vid)
            assert row is not None
            row.last_seen_at = utcnow() - timedelta(days=3)  # no search has returned them for three days
            if hh_id in ("1", "2", "3"):  # pages opened four days ago; «4» and «5» this evening
                app_row = await session.get(Application, opened[hh_id])
                assert app_row is not None
                app_row.queued_at = long_ago
        await session.commit()
    # «1» archived, «2» gone, «3» still open; «4» and «5» closed tonight right after their pages were opened
    respx_mock.get(f"{BASE}/vacancies/1").mock(return_value=httpx.Response(200, json=_full("1", archived=True)))
    respx_mock.get(f"{BASE}/vacancies/2").mock(return_value=httpx.Response(404, json={"errors": []}))
    respx_mock.get(f"{BASE}/vacancies/3").mock(return_value=httpx.Response(200, json=_full("3")))
    for hh_id in ("4", "5"):
        respx_mock.get(f"{BASE}/vacancies/{hh_id}").mock(
            return_value=httpx.Response(200, json=_full(hh_id, archived=True))
        )

    summary = await check_closed(runtime, stale_days=2)
    assert (summary["closed"], summary["refreshed"], summary["responses_cancelled"]) == (4, 1, 2)
    for hh_id, reason in (("1", "архив"), ("2", "удалена")):
        app_row = await _application(runtime, opened[hh_id])
        assert (app_row.status, app_row.error_text) == ("cancelled", freshness.UNCONFIRMED_ERROR), hh_id
        card = (await client.get(f"/api/vacancies/{ids[hh_id]}")).json()
        assert card["pipeline_status"] == "filtered_out" and reason in card["pipeline_reason"]
        assert any("отправку так и не подтвердили" in h["message"] for h in card["history"])
    # the open vacancy and the pages opened this evening keep their «Да, отправлено»
    for hh_id in ("3", "4", "5"):
        assert (await _application(runtime, opened[hh_id])).status == "needs_manual", hh_id
    r = await client.post(f"/api/applications/{opened['4']}/confirm", json={"sent": True})
    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert (await client.get(f"/api/vacancies/{ids['4']}")).json()["pipeline_status"] == "sent"
    assert [a["id"] for a in (await client.get("/api/applications", params={"status": "sent"})).json()["items"]] == [
        opened["4"]
    ]

    # «3» brought back by the user after it closed keeps its page; «5» waits past the grace: the next night takes it
    # (its vacancy is closed already, the loop does not fetch it again)
    async with runtime.session_factory() as session:
        row3 = await session.get(Vacancy, ids["3"])
        assert row3 is not None
        row3.archived, row3.pipeline_status = True, "filtered_out"
        await session.commit()
    assert (await client.post(f"/api/vacancies/{ids['3']}/restore")).json()["pipeline_status"] == "needs_manual"
    async with runtime.session_factory() as session:
        app5 = await session.get(Application, opened["5"])
        assert app5 is not None
        app5.queued_at = long_ago
        await session.commit()
    summary = await check_closed(runtime, stale_days=2)
    assert (summary["candidates"], summary["responses_cancelled"]) == (0, 1)
    assert (await _application(runtime, opened["5"])).status == "cancelled"
    assert (await _application(runtime, opened["3"])).status == "needs_manual"
