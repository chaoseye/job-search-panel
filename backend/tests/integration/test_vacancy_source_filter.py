"""The filter «Сайт» of «Вакансии» (after hirehi.ru became the second source, to tell the vacancies of hh.ru from
those of HireHi): `source` of the list keeps the vacancies of one site. A HireHi vacancy
taken from hh.ru is the vacancy of hh.ru, its HireHi row a skipped duplicate (services/hirehi.route_to_hh). It narrows
like the search that found the vacancy: the counters of the tabs stay as they are; the standing filters say what they
hid within the site. An unknown site is a 422. Migration 0033 puts the site into the covering index."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

import httpx
from alembic import command

from app.config import Settings
from app.db.base import utcnow
from app.db.models import Vacancy
from app.db.session import alembic_config
from app.runtime import Runtime
from app.services import vacancies as vs
from app.services.preferences import Preferences, save_preferences
from tests.integration.test_api_m2 import api  # noqa: F401

__all__ = ["api"]

DAY = timedelta(days=1)
HH_SEARCH, HIREHI_SEARCH = 3, 7
# key: (site, status, published so long ago, responses on hh.ru, the searches that found it)
ROWS: dict[str, tuple[str, str, timedelta, int | None, tuple[int, ...]]] = {
    "hh-suitable": ("hh", "suitable", DAY, 10, (HH_SEARCH,)),
    "hh-prescored": ("hh", "prescored", DAY, None, (HH_SEARCH,)),
    "hh-many": ("hh", "suitable", DAY, 5000, (HH_SEARCH,)),
    "hh-stale": ("hh", "maybe", 9 * DAY, 10, (HH_SEARCH,)),
    "hh-sent": ("hh", "sent", 9 * DAY, 10, (HH_SEARCH,)),
    # the vacancy of hh.ru a HireHi vacancy led to: it remembers the HireHi search (hirehi._tag_all)
    "hh-twin": ("hh", "maybe", DAY, 30, (HH_SEARCH, HIREHI_SEARCH)),
    "hi-suitable": ("hirehi", "suitable", DAY, None, (HIREHI_SEARCH,)),
    "hi-prescored": ("hirehi", "prescored", DAY, None, (HIREHI_SEARCH,)),
    "hi-stale": ("hirehi", "maybe", 9 * DAY, None, (HIREHI_SEARCH,)),
    "hi-sent": ("hirehi", "sent", DAY, None, (HIREHI_SEARCH,)),
    # the HireHi row of hh-twin: a skipped duplicate
    "hi-dup": ("hirehi", "skipped", DAY, None, (HIREHI_SEARCH,)),
}
CANDIDATES = {k for k, (_s, status, *_rest) in ROWS.items() if status in vs.CANDIDATE_STATUSES}


def site_of(key: str) -> set[str]:
    return {k for k in ROWS if k.startswith(key)}


async def _seed(runtime: Runtime) -> dict[str, int]:
    now = utcnow()
    ids: dict[str, int] = {}
    async with runtime.session_factory() as session:
        await save_preferences(session, Preferences(areas=["Казань"]))
        for key, (site, status, age, responses, searches) in ROWS.items():
            row = Vacancy(
                hh_id=key if site == "hh" else None,
                source=site,
                source_id=key if site == "hirehi" else None,
                name=key,
                area_name="Москва",
                published_at=now - age,
                responses_count=responses,
                pipeline_status=status,
                prescore=50,
                source_search_ids="," + "".join(f"{s}," for s in searches),
            )
            session.add(row)
            await session.flush()
            ids[key] = row.id
        dup = await session.get(Vacancy, ids["hi-dup"])
        assert dup is not None
        dup.duplicate_of_id = ids["hh-twin"]
        await session.commit()
    return ids


async def _list(client: httpx.AsyncClient, group: str, **params: Any) -> dict[str, Any]:
    r = await client.get("/api/vacancies", params={"group": group, "per_page": 200, **params})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == len(body["items"])
    return body


async def _names(client: httpx.AsyncClient, group: str, **params: Any) -> set[str]:
    return {v["name"] for v in (await _list(client, group, **params))["items"]}


async def test_the_site_filter_keeps_the_vacancies_of_one_site(api) -> None:
    client, runtime = api
    ids = await _seed(runtime)
    assert await _names(client, "candidates") == CANDIDATES  # off: both sites, as before
    assert await _names(client, "candidates", source="hh") == CANDIDATES & site_of("hh")
    assert await _names(client, "candidates", source="hirehi") == CANDIDATES & site_of("hi")
    # every group: the two sites split it, nothing is lost or shown twice
    for group in ("all", "sent", "skipped", "maybe"):
        both = await _names(client, group)
        hh, hirehi = await _names(client, group, source="hh"), await _names(client, group, source="hirehi")
        assert hh | hirehi == both and not hh & hirehi, group
        assert hh <= site_of("hh") and hirehi <= site_of("hi"), group
    # a HireHi vacancy taken from hh.ru: the vacancy of hh.ru works under «hh.ru», its HireHi row is a duplicate
    assert await _names(client, "skipped", source="hirehi") == {"hi-dup"}
    assert await _names(client, "skipped", source="hh") == set()
    twin = next(v for v in (await _list(client, "maybe", source="hh"))["items"] if v["name"] == "hh-twin")
    assert twin["source"] == "hh" and twin["id"] == ids["hh-twin"]
    dup = (await _list(client, "skipped", source="hirehi"))["items"][0]
    assert dup["source"] == "hirehi" and dup["duplicate_of_id"] == ids["hh-twin"]


async def test_the_counters_of_the_tabs_do_not_follow_the_site(api) -> None:
    """Like the search that found the vacancy: the tabs keep the numbers of every site, the list says «Показано N из
    M»."""
    client, runtime = api
    await _seed(runtime)
    for params in ({}, {"fresh_days": 5}, {"fresh_days": 5, "max_responses": 1000}):
        everything = (await _list(client, "candidates", **params))["counts"]
        for source in ("hh", "hirehi"):
            assert (await _list(client, "candidates", source=source, **params))["counts"] == everything, params
        summary = (await client.get("/api/vacancies/summary", params=params)).json()
        assert summary == everything


async def test_the_site_and_the_search_together(api) -> None:
    """The HireHi search also found the vacancy of hh.ru its HireHi row led to: the search shows both, the site parts
    them."""
    client, runtime = api
    await _seed(runtime)
    found = {"hh-twin", "hi-suitable", "hi-prescored", "hi-stale"}
    assert await _names(client, "candidates", search_id=HIREHI_SEARCH) == found
    assert await _names(client, "candidates", search_id=HIREHI_SEARCH, source="hirehi") == found - {"hh-twin"}
    assert await _names(client, "candidates", search_id=HIREHI_SEARCH, source="hh") == {"hh-twin"}
    assert await _names(client, "candidates", search_id=HH_SEARCH, source="hirehi") == set()


async def test_the_standing_filters_say_what_they_hid_within_the_site(api) -> None:
    client, runtime = api
    await _seed(runtime)
    hirehi = await _list(client, "candidates", source="hirehi", fresh_days=5, max_responses=1000)
    # the old vacancy of hh.ru is not counted among those the freshness filter hid from the list of HireHi
    assert {v["name"] for v in hirehi["items"]} == {"hi-suitable", "hi-prescored"}
    assert hirehi["fresh"]["hidden"] == 1 and hirehi["responses"]["hidden"] == 0  # hi-stale; HireHi has no counter
    hh = await _list(client, "candidates", source="hh", fresh_days=5, max_responses=1000)
    assert {v["name"] for v in hh["items"]} == {"hh-suitable", "hh-prescored", "hh-twin"}
    assert hh["fresh"]["hidden"] == 1 and hh["responses"]["hidden"] == 1  # hh-stale; hh-many
    # the response stages are never hidden, on either site
    assert await _names(client, "sent", source="hh", fresh_days=5) == {"hh-sent"}
    assert await _names(client, "sent", source="hirehi", fresh_days=5) == {"hi-sent"}


async def test_an_unknown_site_is_a_422(api) -> None:
    client, runtime = api
    await _seed(runtime)
    for value in ("hh.ru", "HireHi", "HH", "", "all", "hh,hirehi"):
        r = await client.get("/api/vacancies", params={"source": value})
        assert r.status_code == 422, (value, r.text)
    for value in ("hh", "hirehi"):
        assert (await client.get("/api/vacancies", params={"source": value})).status_code == 200


def _listing_columns(db: str) -> list[str]:
    con = sqlite3.connect(db)
    try:
        return [r[2] for r in con.execute("pragma index_info(ix_vacancies_listing)")]
    finally:
        con.close()


def test_the_migration_puts_the_site_into_the_listing_index_and_takes_it_out(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(_env_file=None, data_dir=tmp_path, logs_dir=tmp_path / "logs")  # type: ignore[call-arg]
    cfg = alembic_config(settings)
    db = str(settings.db_path)
    command.upgrade(cfg, "0032_vacancy_source")
    before = _listing_columns(db)
    assert before[-2:] == ["area_name", "responses_count"]
    con = sqlite3.connect(db)
    con.execute(
        "insert into vacancies (hh_id, source, source_id, name, source_search_ids, pipeline_status, first_seen_at, "
        "created_at, updated_at, archived, response_letter_required, has_test, full_fetch_errors, hidden, "
        "user_override, claude_attempts) values (NULL, 'hirehi', '218432', 'Backend engineer', ',7,', 'suitable', "
        "'2026-11-08 10:00:00', '2026-11-08 10:00:00', '2026-11-08 10:00:00', 0, 0, 0, 0, 0, 0, 0)"
    )
    con.commit()
    rows = con.execute("select * from vacancies").fetchall()
    con.close()
    command.upgrade(cfg, "head")
    assert _listing_columns(db) == [*before, "source"]
    command.downgrade(cfg, "0032_vacancy_source")  # the index of 0031/0032 again, the rows stay
    assert _listing_columns(db) == before
    command.upgrade(cfg, "head")
    con = sqlite3.connect(db)
    assert con.execute("select * from vacancies").fetchall() == rows
    assert con.execute("pragma integrity_check").fetchone() == ("ok",)
    con.close()
