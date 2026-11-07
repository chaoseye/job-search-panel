"""The responses cap of «Вакансии»: only the vacancies with up to N responses on hh.ru. Off by default (the user sets
the number); then like the freshness filter: every group but the response stages, a vacancy without the counter stays,
the counters of the tabs and of the menu count what is shown. With both standing filters the list says how many each
took out: the stale ones first, then the fresh ones over the cap."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx

from app.db.base import utcnow
from app.db.models import Vacancy
from app.runtime import Runtime
from app.services import vacancies as vs
from app.services.preferences import Preferences, save_preferences
from tests.integration.test_api_m2 import api  # noqa: F401

__all__ = ["api"]

DAY = timedelta(days=1)
# hh_id: (responses on hh.ru, status, published so long ago, city)
ROWS: dict[str, tuple[int | None, str, timedelta, str]] = {
    "r10": (10, "suitable", DAY, "Москва"),
    "r999": (999, "maybe", DAY, "Москва"),
    "r1000": (1000, "prescored", DAY, "Москва"),
    "r1001": (1001, "suitable", DAY, "Москва"),
    "r5000": (5000, "maybe", DAY, "Москва"),
    "rnone": (None, "maybe", DAY, "Москва"),
    "r50-stale": (50, "maybe", 9 * DAY, "Москва"),
    "r2000-stale": (2000, "suitable", 9 * DAY, "Москва"),
    "r1500-kzn-old": (1500, "suitable", 20 * DAY, "Казань"),
    "r1500-watched": (1500, "watched", DAY, "Москва"),
    # the response is under way or done: never hidden
    "r3000-manual": (3000, "needs_manual", DAY, "Москва"),
    "r5000-sent": (5000, "sent", DAY, "Москва"),
}
CANDIDATES = {k for k, (_n, status, _a, _c) in ROWS.items() if status in vs.CANDIDATE_STATUSES}


async def _seed(runtime: Runtime) -> None:
    now = utcnow()
    async with runtime.session_factory() as session:
        await save_preferences(session, Preferences(areas=["Казань"]))
        for hh_id, (responses, status, age, city) in ROWS.items():
            session.add(
                Vacancy(
                    hh_id=hh_id,
                    name=f"Вакансия {hh_id}",
                    area_name=city,
                    published_at=now - age,
                    responses_count=responses,
                    pipeline_status=status,
                    work_format_ids_json=json.dumps(["REMOTE"]),
                    prescore=50,
                )
            )
        await session.commit()


async def _list(client: httpx.AsyncClient, group: str, **params: Any) -> dict[str, Any]:
    r = await client.get("/api/vacancies", params={"group": group, "per_page": 200, **params})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == len(body["items"])
    return body


async def _ids(client: httpx.AsyncClient, group: str, **params: Any) -> set[str]:
    return {v["hh_id"] for v in (await _list(client, group, **params))["items"]}


async def test_the_cap_keeps_vacancies_with_up_to_n_responses(api) -> None:
    client, runtime = api
    await _seed(runtime)
    assert await _ids(client, "candidates") == CANDIDATES  # off by default: as before
    # 1000 is still in, 1001 is out; no counter — nothing to judge by, stays
    assert await _ids(client, "candidates", max_responses=1000) == {"r10", "r999", "r1000", "rnone", "r50-stale"}
    assert await _ids(client, "candidates", max_responses=0) == {"rnone"}
    assert await _ids(client, "candidates", max_responses=5000) == CANDIDATES
    # the response stages keep everything, the other groups follow the cap
    assert await _ids(client, "needs_manual", max_responses=100) == {"r3000-manual"}
    assert await _ids(client, "sent", max_responses=100) == {"r5000-sent"}
    assert await _ids(client, "watched", max_responses=1000) == set()
    # «Меньше откликов» within the cap, the vacancy without a counter last
    items = (await _list(client, "candidates", max_responses=1000, sort="responses"))["items"]
    assert [v["hh_id"] for v in items] == ["r10", "r50-stale", "r999", "r1000", "rnone"]


async def test_both_standing_filters_count_what_they_show_and_say_what_they_hid(api) -> None:
    client, runtime = api
    await _seed(runtime)
    body = await _list(client, "candidates", fresh_days=5, max_responses=1000)
    assert {v["hh_id"] for v in body["items"]} == {"r10", "r999", "r1000", "rnone"}
    # the stale ones first (whatever their responses), then the fresh ones over the cap — together all that is hidden
    assert body["fresh"]["hidden"] == 2  # r50-stale, r2000-stale
    assert body["responses"] == {"max": 1000, "hidden": 3}  # r1001, r5000, r1500-kzn-old (Kazan: fresh by city)
    assert body["total"] + body["fresh"]["hidden"] + body["responses"]["hidden"] == len(CANDIDATES)
    counts = body["counts"]
    for group, statuses in vs.GROUPS.items():
        if statuses is None or group == "hidden":
            continue
        listed = (await _list(client, group, fresh_days=5, max_responses=1000))["total"]
        assert counts[group] == listed, group
    assert counts["candidates"] == 4 and counts["unseen_candidates"] == 4
    summary = (await client.get("/api/vacancies/summary", params={"fresh_days": 5, "max_responses": 1000})).json()
    assert summary == counts
    # the cap alone: what it hides among all
    alone = await _list(client, "candidates", max_responses=1000)
    assert alone["responses"] == {"max": 1000, "hidden": 4} and alone["fresh"]["hidden"] == 0
    # off: nothing hidden, the block says so
    assert (await _list(client, "candidates"))["responses"] == {"max": None, "hidden": 0}


async def test_max_responses_must_be_a_count(api) -> None:
    client, runtime = api
    await _seed(runtime)
    for value in ("-1", "1000000", "abc", "1.5"):
        for path in ("/api/vacancies", "/api/vacancies/summary"):
            r = await client.get(path, params={"max_responses": value})
            assert r.status_code == 422, (path, value, r.text)
    for value in ("0", "999999"):
        assert (await client.get("/api/vacancies", params={"max_responses": value})).status_code == 200
        assert (await client.get("/api/vacancies/summary", params={"max_responses": value})).status_code == 200
