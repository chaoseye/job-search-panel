"""The freshness filter of «Вакансии»: outside the cities of the base questionnaire a vacancy is shown only while its
hh `published_at` is within the last N days — remote ones too, one without a city counts as another city; a vacancy
whose response is under way or done is never hidden; the counters of the tabs and of the menu count what the filter
shows. The API applies it only when asked (`fresh_days`): the page asks by default."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx

from app.db.base import utcnow
from app.db.models import CoverLetter, Vacancy
from app.runtime import Runtime
from app.services import vacancies as vs
from app.services.preferences import Preferences, save_preferences
from tests.integration.test_api_m2 import api  # noqa: F401

__all__ = ["api"]

DAY = timedelta(days=1)
MARKER = "Здравствуйте! [[УТОЧНИТЬ: дата выхода]]"

# hh_id: (city, published so long ago (None — no date), status, extra fields)
ROWS: dict[str, tuple[str | None, timedelta | None, str, dict[str, Any]]] = {
    "kzn-old": ("Казань", 20 * DAY, "suitable", {}),
    "kzn-new": ("Казань", 1 * DAY, "maybe", {}),
    "msk-old": ("Москва", 6 * DAY, "maybe", {"remote": True, "letter": MARKER}),
    "msk-new": ("Москва", 2 * DAY, "prescored", {"remote": True}),
    "nn-in": ("Нижний Новгород", 5 * DAY - timedelta(minutes=10), "suitable", {}),
    "nn-out": ("Нижний Новгород", 5 * DAY + timedelta(minutes=10), "suitable", {}),
    "nocity-old": (None, 10 * DAY, "prescored", {}),
    "smr-nodate": ("Самара", None, "maybe", {}),
    "oryol-old": ("Орёл", 9 * DAY, "awaiting_claude", {}),
    "msk-snoozed-old": ("Москва", 7 * DAY, "suitable", {"snoozed": True}),
    "kzn-snoozed-old": ("Казань", 7 * DAY, "suitable", {"snoozed": True}),
    "msk-hidden-old": ("Москва", 7 * DAY, "hidden", {"hidden": True}),
    "msk-filtered-old": ("Москва", 8 * DAY, "filtered_out", {}),
    "msk-watched-old": ("Москва", 12 * DAY, "watched", {}),
    "kzn-watched-old": ("Казань", 12 * DAY, "watched", {}),
    # the response is under way or done: never hidden
    "msk-manual-old": ("Москва", 30 * DAY, "needs_manual", {"letter": MARKER}),
    "msk-queued-old": ("Москва", 30 * DAY, "queued", {}),
    "msk-sent-old": ("Москва", 30 * DAY, "sent", {}),
    "msk-applied-old": ("Москва", 30 * DAY, "already_applied", {}),
}
# hidden by the 5-day filter when the questionnaire names Kazan only
STALE = {
    "msk-old",
    "nn-out",
    "nocity-old",
    "oryol-old",
    "msk-snoozed-old",
    "msk-hidden-old",
    "msk-filtered-old",
    "msk-watched-old",
}


async def _seed(runtime: Runtime, areas: list[str]) -> None:
    now = utcnow()
    async with runtime.session_factory() as session:
        await save_preferences(session, Preferences(areas=areas))
        for hh_id, (city, age, status, extra) in ROWS.items():
            row = Vacancy(
                hh_id=hh_id,
                name=f"Вакансия {hh_id}",
                area_name=city,
                area_id={"Казань": "88", "Москва": "1"}.get(city or "", None),
                published_at=now - age if age is not None else None,
                pipeline_status=status,
                hidden=bool(extra.get("hidden")),
                snoozed_until=now + 3 * DAY if extra.get("snoozed") else None,
                work_format_ids_json=json.dumps(["REMOTE"] if extra.get("remote") else ["HYBRID"]),
                prescore=50,
            )
            session.add(row)
            await session.flush()
            if extra.get("letter"):
                session.add(CoverLetter(vacancy_id=row.id, version=1, text=extra["letter"], origin="claude"))
        await session.commit()


async def _list(client: httpx.AsyncClient, group: str, **params: Any) -> dict[str, Any]:
    r = await client.get("/api/vacancies", params={"group": group, "per_page": 200, **params})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == len(body["items"])
    return body


async def _ids(client: httpx.AsyncClient, group: str, **params: Any) -> set[str]:
    return {v["hh_id"] for v in (await _list(client, group, **params))["items"]}


async def test_stale_vacancies_outside_the_questionnaire_cities_are_hidden(api) -> None:
    client, runtime = api
    # typed by hand in the questionnaire: the case and «ё» do not matter (the region rule compares the same way)
    await _seed(runtime, ["казань", "Орел"])
    everything = {"kzn-old", "kzn-new", "msk-old", "msk-new", "nn-in", "nn-out", "nocity-old", "smr-nodate"}
    everything |= {"oryol-old"}
    assert await _ids(client, "candidates") == everything  # the API without fresh_days: as before
    shown = await _ids(client, "candidates", fresh_days=5)
    # Kazan and Orel at any age; Moscow (remote too) and Nizhny Novgorod only within 5 days, the boundary to the
    # minute; no city — another city; no publication date — nothing to judge by, stays
    assert shown == {"kzn-old", "kzn-new", "msk-new", "nn-in", "smr-nodate", "oryol-old"}
    assert await _ids(client, "candidates", fresh_days=7) == shown | {"msk-old", "nn-out"}
    assert await _ids(client, "candidates", fresh_days=30) == everything
    # the other groups the same way
    assert await _ids(client, "watched", fresh_days=5) == {"kzn-watched-old"}
    assert await _ids(client, "filtered_out", fresh_days=5) == set()
    assert await _ids(client, "hidden", fresh_days=5) == set()
    assert await _ids(client, "candidates", fresh_days=5, include_snoozed="true") == shown | {"kzn-snoozed-old"}
    # with the other filters of the page
    assert await _ids(client, "candidates", fresh_days=5, q="msk") == {"msk-new"}
    assert await _ids(client, "candidates", fresh_days=5, work_format="REMOTE") == {"msk-new"}
    assert await _ids(client, "candidates", fresh_days=5, area="новгород") == {"nn-in"}


async def test_vacancies_with_a_response_under_way_are_never_hidden(api) -> None:
    client, runtime = api
    await _seed(runtime, ["Казань"])
    for group, hh_id in (
        ("needs_manual", "msk-manual-old"),
        ("queued", "msk-queued-old"),
        ("sent", "msk-sent-old"),
        ("already_applied", "msk-applied-old"),
    ):
        assert await _ids(client, group, fresh_days=5) == {hh_id}, group
    every = await _ids(client, "all", fresh_days=5)
    assert {"msk-manual-old", "msk-queued-old", "msk-sent-old", "msk-applied-old"} <= every
    assert not every & STALE
    # «Уточнить письмо»: the stale candidate's question goes away with it, the open assistant page's stays
    assert await _ids(client, "needs_clarification") == {"msk-old", "msk-manual-old"}
    assert await _ids(client, "needs_clarification", fresh_days=5) == {"msk-manual-old"}


async def test_the_counters_count_what_the_filter_shows(api) -> None:
    client, runtime = api
    await _seed(runtime, ["Казань"])
    body = await _list(client, "candidates", fresh_days=5)
    counts = body["counts"]
    for group, statuses in vs.GROUPS.items():
        if statuses is None or group == "hidden":  # «hidden» counts the hidden flag, not a list of visible rows
            continue
        assert counts[group] == (await _list(client, group, fresh_days=5))["total"], group
    assert counts["needs_clarification"] == 1
    assert counts["candidates"] == 5 and counts["unseen_candidates"] == 5
    assert counts["snoozed"] == 1  # Kazan's; the stale Moscow one is not counted
    assert counts["hidden"] == 0 and counts["total"] == len(ROWS) - len(STALE)
    # the menu asks the summary with the page's filter: the same numbers
    summary = (await client.get("/api/vacancies/summary", params={"fresh_days": 5})).json()
    assert summary == counts
    # without the filter, the full numbers
    full = (await client.get("/api/vacancies/summary")).json()
    assert full == (await _list(client, "candidates"))["counts"]
    assert full["candidates"] == 9 and full["unseen_candidates"] == 9 and full["total"] == len(ROWS)
    assert full["needs_clarification"] == 2 and full["snoozed"] == 2 and full["hidden"] == 1


async def test_the_list_says_what_the_filter_hid_and_the_exempt_cities(api) -> None:
    client, runtime = api
    await _seed(runtime, ["Казань", " "])
    body = await _list(client, "candidates", fresh_days=5)
    # msk-old, nn-out, nocity-old, oryol-old
    assert body["fresh"] == {"days": 5, "home_areas": ["Казань"], "hidden": 4}
    # within the other filters of the list
    assert (await _list(client, "candidates", fresh_days=5, q="msk"))["fresh"]["hidden"] == 1
    assert (await _list(client, "sent", fresh_days=5))["fresh"]["hidden"] == 0
    # off: the cities are still named (the page labels the switch), nothing hidden
    assert (await _list(client, "candidates"))["fresh"] == {"days": None, "home_areas": ["Казань"], "hidden": 0}


async def test_without_cities_in_the_questionnaire_every_city_is_subject(api) -> None:
    client, runtime = api
    await _seed(runtime, [])
    body = await _list(client, "candidates", fresh_days=5)
    assert {v["hh_id"] for v in body["items"]} == {"kzn-new", "msk-new", "nn-in", "smr-nodate"}
    assert body["fresh"]["home_areas"] == [] and body["fresh"]["hidden"] == 5


async def test_fresh_days_must_be_a_number_of_days(api) -> None:
    client, runtime = api
    await _seed(runtime, ["Казань"])
    for value in ("0", "-1", "366", "abc", "1.5"):
        for path in ("/api/vacancies", "/api/vacancies/summary"):
            r = await client.get(path, params={"fresh_days": value})
            assert r.status_code == 422, (path, value, r.text)
    assert (await client.get("/api/vacancies", params={"fresh_days": 365})).status_code == 200
    assert (await client.get("/api/vacancies/summary", params={"fresh_days": 1})).status_code == 200


def test_area_key_folds_case_and_yo() -> None:
    assert vs.area_key("  Орёл ") == vs.area_key("орел") == "орел"
    assert vs.area_key("Нижний  Новгород") == "нижний новгород"
    assert vs.area_key("КАЗАНЬ") == "казань"
