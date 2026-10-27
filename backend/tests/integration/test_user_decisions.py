"""What the user decided stays decided: a Claude result imported later does not resurrect a vacancy
the user hid, skipped or applied to; «Вернуть» keeps the Claude verdict; un-hiding an employer brings its vacancies
back; «В пакет» is refused once an application exists; skipping/hiding withdraws an application the API queue has not
sent; a vacancy Claude keeps failing on is parked after MAX_CLAUDE_ATTEMPTS; a result carrying secrets is rejected."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx
from sqlalchemy import select

from app.db.models import Application, Notification, Vacancy
from app.exchange.importer import MAX_CLAUDE_ATTEMPTS
from tests.integration.test_api_m3 import _good_letter, _login, _result, _seed, api  # noqa: F401
from tests.integration.test_api_m4 import _consent
from tests.integration.test_api_m4 import _seed as _seed_api_mode
from tests.integration.test_api_m4 import api as api_mode  # noqa: F401

__all__ = ["api", "api_mode"]  # fixtures shared with test_api_m3 / test_api_m4


async def _packet(client: httpx.AsyncClient) -> tuple[Path, dict]:
    r = await client.post("/api/batches/create", json={})
    assert r.status_code == 200, r.text
    created = r.json()["created"]
    assert created, "no packet was created"
    path = Path(created[0]["packet_path"])
    return path, json.loads(path.read_text(encoding="utf-8"))


def _evaluated(vacancy_id: str, score: int = 85, letter: str | None = None) -> dict:
    return {
        "vacancy_id": vacancy_id,
        "evaluation": {"score": score, "verdict": "apply" if score >= 70 else "maybe", "summary": "совпадение"},
        "letter": {"text": letter or _good_letter(), "language": "ru"},
        "error": None,
    }


async def _import(client: httpx.AsyncClient, runtime, path: Path, packet: dict, items: list[dict]) -> dict:
    done = runtime.settings.exchange_dir / "done" / path.name
    done.write_text(json.dumps(_result(packet["batch_id"], items), ensure_ascii=False), encoding="utf-8")
    r = await client.post("/api/batches/import")
    assert r.status_code == 200, r.text
    return r.json()["outcomes"][0]


async def _vacancy(client: httpx.AsyncClient, vid: int) -> dict:
    return (await client.get(f"/api/vacancies/{vid}")).json()


# ---- the import keeps the user's decisions -------------------------------------------------------------------


@respx.mock(assert_all_called=False)
async def test_hidden_while_with_claude_stays_hidden(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    ids = await _seed(client, runtime, respx_mock, ["71"])
    path, packet = await _packet(client)
    await client.post(f"/api/vacancies/{ids['71']}/hide")
    outcome = await _import(client, runtime, path, packet, [_evaluated("71")])
    v = await _vacancy(client, ids["71"])
    assert v["pipeline_status"] == "hidden" and v["hidden"] is True
    assert v["score"] == 85 and v["evaluation"]["score"] == 85  # the evaluation is kept
    assert v["letter"] is None and v["batch_id"] is None  # no letter for a vacancy the user dropped
    assert outcome["status"] == "imported" and any("оценка сохранена" in w for w in outcome["warnings"])


@respx.mock(assert_all_called=False)
async def test_applied_while_with_claude_stays_sent_and_no_letter_goes_to_the_bot(
    api, respx_mock: respx.MockRouter
) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    ids = await _seed(client, runtime, respx_mock, ["72"])
    path, packet = await _packet(client)
    vid = ids["72"]
    # the user did not wait for Claude: own letter, applied on hh.ru in the assist mode
    await client.put("/api/settings", json={"apply_mode_override": "assist"})
    await client.post("/api/applications/consent")
    await client.put(f"/api/vacancies/{vid}/letter", json={"text": _good_letter()})
    opened = (await client.post("/api/applications/assist/open", json={"vacancy_id": vid})).json()
    assert (await client.post(f"/api/applications/{opened['id']}/confirm", json={"sent": True})).json()[
        "status"
    ] == "sent"
    await _import(client, runtime, path, packet, [_evaluated("72", letter=_good_letter(650) + " Claude")])
    v = await _vacancy(client, vid)
    assert v["pipeline_status"] == "sent" and v["score"] == 85
    assert not v["letter"]["text"].endswith(" Claude")  # the sent letter stays current
    async with runtime.session_factory() as session:
        letters = (await session.execute(select(Notification).where(Notification.event_type == "letter"))).scalars()
        assert list(letters) == []


# ---- «Вернуть», «В пакет», un-hiding an employer ----------------------------------------------------------------


@respx.mock(assert_all_called=False)
async def test_restore_returns_to_the_verdict_not_to_claude(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    ids = await _seed(client, runtime, respx_mock, ["73"])
    path, packet = await _packet(client)
    await _import(client, runtime, path, packet, [_evaluated("73", score=60)])
    vid = ids["73"]
    await client.post(f"/api/vacancies/{vid}/skip", json={"reason": "подумать"})
    r = await client.post(f"/api/vacancies/{vid}/restore")
    assert r.json()["pipeline_status"] == "maybe" and r.json()["user_override"] is True
    assert (await client.post("/api/batches/create", json={})).json()["created"] == []  # not re-packed


@respx.mock(assert_all_called=False)
async def test_restore_while_its_packet_still_waits(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    ids = await _seed(client, runtime, respx_mock, ["74"])
    path, packet = await _packet(client)
    vid = ids["74"]
    await client.post(f"/api/vacancies/{vid}/hide")
    assert (await client.post(f"/api/vacancies/{vid}/restore")).json()["pipeline_status"] == "awaiting_claude"
    assert (await client.post("/api/batches/create", json={})).json()["created"] == []  # no second packet
    await _import(client, runtime, path, packet, [_evaluated("74", score=90)])
    assert (await _vacancy(client, vid))["pipeline_status"] == "suitable"


@respx.mock(assert_all_called=False)
async def test_unhide_employer_brings_back_only_what_it_hid(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    ids = await _seed(client, runtime, respx_mock, ["75", "76"])  # same employer (fixture)
    await client.post(f"/api/vacancies/{ids['76']}/hide")  # hidden by hand before
    r = await client.post(f"/api/vacancies/{ids['75']}/hide-employer")
    assert r.json()["pipeline_status"] == "hidden"
    hidden = (await client.get("/api/vacancies/hidden-employers")).json()
    r = await client.delete(f"/api/vacancies/hidden-employers/{hidden[0]['id']}")
    assert r.json() == {"ok": True, "restored": 1}
    assert (await _vacancy(client, ids["75"]))["pipeline_status"] == "prescored"
    assert (await _vacancy(client, ids["76"]))["pipeline_status"] == "hidden"  # the user's own decision


@respx.mock(assert_all_called=False)
async def test_to_batch_is_refused_once_applied(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    ids = await _seed(client, runtime, respx_mock, ["77"])
    vid = ids["77"]
    await client.put("/api/settings", json={"apply_mode_override": "assist"})
    await client.post("/api/applications/consent")
    await client.put(f"/api/vacancies/{vid}/letter", json={"text": _good_letter()})
    opened = (await client.post("/api/applications/assist/open", json={"vacancy_id": vid})).json()
    await client.post(f"/api/applications/{opened['id']}/confirm", json={"sent": True})
    r = await client.post(f"/api/vacancies/{vid}/to-batch")
    assert r.status_code == 409 and "отклик" in r.json()["detail"]
    await client.post(f"/api/vacancies/{vid}/skip", json={})
    assert (await client.post(f"/api/vacancies/{vid}/restore")).json()["pipeline_status"] == "sent"


@respx.mock(assert_all_called=False)
async def test_skip_and_hide_withdraw_an_unsent_api_application(api_mode, respx_mock: respx.MockRouter) -> None:
    client, runtime = api_mode
    ids = await _seed_api_mode(client, runtime, respx_mock, ["81", "82"])
    await _consent(client)
    runtime.settings = runtime.settings.model_copy(update={"apply_window": "03:00-03:01"})  # the queue waits
    route = respx_mock.post("https://api.hh.ru/negotiations").mock(return_value=httpx.Response(201))
    r = await client.post("/api/applications/queue", json={"vacancy_ids": [ids["81"], ids["82"]]})
    assert r.status_code == 200, r.text
    await runtime.jobs.wait("apply")
    await client.post(f"/api/vacancies/{ids['81']}/skip", json={"reason": "передумал"})
    await client.post(f"/api/vacancies/{ids['82']}/hide")
    async with runtime.session_factory() as session:
        statuses = {a.vacancy_id: a.status for a in (await session.execute(select(Application))).scalars()}
    assert statuses == {ids["81"]: "cancelled", ids["82"]: "cancelled"}
    runtime.settings = runtime.settings.model_copy(update={"apply_window": "00:00-23:59"})
    await client.post("/api/applications/run")
    await runtime.jobs.wait("apply")
    assert route.call_count == 0  # nothing was sent for the dropped vacancies


# ---- failing results ---------------------------------------------------------------------------------------------


@respx.mock(assert_all_called=False)
async def test_a_vacancy_claude_keeps_failing_on_is_parked(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    ids = await _seed(client, runtime, respx_mock, ["78"])
    vid = ids["78"]
    for attempt in range(1, MAX_CLAUDE_ATTEMPTS + 1):
        path, packet = await _packet(client)
        await _import(client, runtime, path, packet, [{"vacancy_id": "78", "error": "не могу разобрать"}])
        v = await _vacancy(client, vid)
        if attempt < MAX_CLAUDE_ATTEMPTS:
            assert v["pipeline_status"] == "prescored", attempt
    assert v["pipeline_status"] == "needs_manual" and "раза подряд" in v["pipeline_reason"]
    assert (await client.post("/api/batches/create", json={})).json()["created"] == []
    # the user decides to try again: the counter starts over
    assert (await client.post(f"/api/vacancies/{vid}/to-batch")).json()["pipeline_status"] == "prescored"
    path, packet = await _packet(client)
    await _import(client, runtime, path, packet, [_evaluated("78", score=75)])
    assert (await _vacancy(client, vid))["pipeline_status"] == "suitable"
    async with runtime.session_factory() as session:
        assert (await session.get(Vacancy, vid)).claude_attempts == 0


@respx.mock(assert_all_called=False)
async def test_a_result_with_secrets_is_rejected(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    runtime.settings = runtime.settings.model_copy(update={"imap_password": "Imap-App-Password-42"})
    await _login(client, respx_mock)
    ids = await _seed(client, runtime, respx_mock, ["79", "80"])
    path, packet = await _packet(client)
    token = "1234567890:" + "A" * 35  # a Telegram bot token pasted by a prompt injection
    outcome = await _import(
        client,
        runtime,
        path,
        packet,
        [
            _evaluated("79", letter=_good_letter() + f" Токен: {token}"),
            _evaluated("80", letter=_good_letter() + " пароль Imap-App-Password-42"),
        ],
    )
    assert outcome["status"] == "partial" and len(outcome["errors"]) == 2
    assert all("отклонён" in e for e in outcome["errors"])
    for hh_id in ("79", "80"):
        v = await _vacancy(client, ids[hh_id])
        assert v["pipeline_status"] == "prescored" and v["letter"] is None and v["score"] is None
