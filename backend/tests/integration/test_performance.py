"""Speed of the panel on the target volume, checked without timing anything: the
large columns of `vacancies` sit last, the hot queries run from covering indexes (query plans), the fast paths give
the same answers as the plain ones, the analytics base is built once and rebuilt after writes, the statistics cache
never shows another database or stale data."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import timedelta
from typing import Any

import pytest
from alembic import command
from sqlalchemy import event, update

from app.config import Settings
from app.db import changes
from app.db.base import utcnow
from app.db.models import (
    Application,
    AuditLog,
    CoverLetter,
    Negotiation,
    Vacancy,
    VacancyEvaluation,
)
from app.db.session import alembic_config
from app.runtime import Runtime
from app.services import analytics as an
from app.services import stats as st
from app.services import vacancies as vs
from app.services.state import get_system_state

LARGE = ["description_html", "description_text", "raw_short_json", "raw_full_json"]


@pytest.fixture
async def runtime(settings: Settings):
    rt = await Runtime.create(settings)
    st.clear_cache()
    an.clear_base_cache()
    try:
        yield rt
    finally:
        await rt.close()


def _vacancy(i: int, **kw: Any) -> Vacancy:
    now = utcnow()
    values: dict[str, Any] = dict(
        hh_id=str(1000 + i),
        name=f"Python developer {i}",
        employer_id=str(i % 4),
        employer_name=f"Работодатель {i % 4}",
        published_at=now - timedelta(hours=i),
        first_seen_at=now - timedelta(hours=i),
        pipeline_status="prescored",
        prescore=40 + i % 30,
        raw_full_json=json.dumps({"id": i, "description": "x" * 5000}),
        description_text="Описание " * 50,
        key_skills_json=json.dumps(["Python", "FastAPI"]),
    )
    values.update(kw)
    return Vacancy(**values)


# ---- layout and plans --------------------------------------------------------------------------------------------


async def test_large_columns_are_last_and_indexes_exist(runtime: Runtime) -> None:
    db = str(runtime.settings.db_path)
    con = sqlite3.connect(db)
    cols = [r[1] for r in con.execute("pragma table_info(vacancies)")]
    indexes = {r[1] for r in con.execute("pragma index_list(vacancies)")}
    con.close()
    assert cols[-4:] == LARGE, cols[-8:]
    assert {"ix_vacancies_listing", "ix_vacancies_first_seen"} <= indexes


def test_layout_migration_keeps_every_value(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(_env_file=None, data_dir=tmp_path, logs_dir=tmp_path / "logs")  # type: ignore[call-arg]
    cfg = alembic_config(settings)
    command.upgrade(cfg, "0020_claude_attempts")
    con = sqlite3.connect(settings.db_path)  # type: ignore[arg-type]
    con.execute(
        "insert into vacancies (hh_id, name, source_search_ids, pipeline_status, first_seen_at, created_at, "
        "updated_at, archived, response_letter_required, has_test, full_fetch_errors, hidden, user_override, "
        "claude_attempts, raw_full_json, description_text, score, prescore, address, tags_json) values "
        "('77', 'Инженер', ',1,', 'suitable', '2026-09-01 10:00:00', '2026-09-01 10:00:00', '2026-09-02 10:00:00', "
        "0, 1, 0, 2, 0, 1, 2, '{\"big\": 1}', 'текст', 81, 60, 'Тверская, 1', '{\"stack\": [\"Python\"]}')"
    )
    con.commit()
    before = con.execute("select * from vacancies").fetchall()
    names_before = [r[1] for r in con.execute("pragma table_info(vacancies)")]
    con.close()
    command.upgrade(cfg, "head")
    con = sqlite3.connect(settings.db_path)  # type: ignore[arg-type]
    names_after = [r[1] for r in con.execute("pragma table_info(vacancies)")]
    after = con.execute(f"select {', '.join(names_before)} from vacancies").fetchall()
    unique = con.execute("select sql from sqlite_master where name = 'vacancies'").fetchone()[0]
    con.close()
    assert after == before and sorted(names_after) == sorted(names_before) and names_after[-4:] == LARGE
    assert "UNIQUE (hh_id)" in unique
    command.downgrade(cfg, "0020_claude_attempts")  # the indexes go, the rows stay
    command.upgrade(cfg, "head")


async def _plans(runtime: Runtime, run: Any) -> list[tuple[str, str]]:
    """(statement, query plan) of every SELECT the coroutine function `run(session)` executes."""
    seen: list[tuple[str, Any]] = []

    def capture(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]  # noqa: ANN001
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append((statement, parameters))

    event.listen(runtime.engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with runtime.session_factory() as session:
            await run(session)
    finally:
        event.remove(runtime.engine.sync_engine, "before_cursor_execute", capture)
    con = sqlite3.connect(str(runtime.settings.db_path))
    out = []
    for statement, params in seen:
        plan = " | ".join(r[3] for r in con.execute("EXPLAIN QUERY PLAN " + statement, params or ()))
        out.append((" ".join(statement.split()), plan))
    con.close()
    return out


async def test_hot_queries_run_from_covering_indexes(runtime: Runtime) -> None:
    async with runtime.session_factory() as session:
        session.add_all([_vacancy(i) for i in range(30)])
        await session.commit()

    counts = await _plans(runtime, vs.status_counts)
    grouped = [p for s, p in counts if "GROUP BY vacancies.pipeline_status" in s]
    assert grouped and "COVERING INDEX ix_vacancies_listing" in grouped[0], counts

    listing = await _plans(runtime, lambda s: vs.list_vacancies(s, group="all"))
    page = [p for s, p in listing if s.startswith("SELECT vacancies.id FROM vacancies") and "LIMIT" in s]
    assert page and "COVERING INDEX ix_vacancies_listing" in page[0], listing
    candidates = await _plans(runtime, lambda s: vs.list_vacancies(s, group="candidates"))
    page = [p for s, p in candidates if s.startswith("SELECT vacancies.id FROM vacancies") and "LIMIT" in s]
    assert page and "COVERING INDEX ix_vacancies_listing" in page[0], candidates

    statistics = await _plans(
        runtime, lambda s: st.compute_stats(s, runtime.settings, period_key="30d", use_cache=False)
    )
    found = [p for s, p in statistics if "vacancies.first_seen_at >=" in s and "rules_passed" in s]
    assert found and "COVERING INDEX ix_vacancies_first_seen" in found[0], statistics
    # no statement of the statistics reads the vacancy payload
    assert not [s for s, _ in statistics if "raw_full_json" in s or "description_html" in s]


# ---- the fast paths answer the same ------------------------------------------------------------------------------


async def test_status_counts_match_plain_counting(runtime: Runtime) -> None:
    now = utcnow()
    statuses = ["prescored", "suitable", "maybe", "not_suitable", "filtered_out", "sent", "watched", "hidden"]
    async with runtime.session_factory() as session:
        rows = [
            _vacancy(
                i,
                pipeline_status=statuses[i % len(statuses)],
                hidden=i % 7 == 0,
                snoozed_until=(now + timedelta(days=1))
                if i % 5 == 0
                else (now - timedelta(days=1))
                if i % 5 == 1
                else None,
                seen_by_user_at=now if i % 3 == 0 else None,
            )
            for i in range(60)
        ]
        session.add_all(rows)
        await session.flush()
        for r in rows[:6]:
            session.add(
                CoverLetter(vacancy_id=r.id, version=1, text="Здравствуйте [[УТОЧНИТЬ: стаж]]", origin="claude")
            )
        await session.commit()
        counts = await vs.status_counts(session)

    def visible(v: Vacancy) -> bool:
        return not v.hidden and (v.snoozed_until is None or v.snoozed_until <= now)

    by_status: dict[str, int] = {}
    for v in rows:
        if visible(v):
            by_status[v.pipeline_status] = by_status.get(v.pipeline_status, 0) + 1
    assert counts["by_status"] == by_status  # type: ignore[comparison-overlap]
    assert counts["total"] == 60 and counts["hidden"] == sum(1 for v in rows if v.hidden)
    assert counts["snoozed"] == sum(1 for v in rows if v.snoozed_until and v.snoozed_until > now)
    assert counts["unseen_candidates"] == sum(
        1 for v in rows if visible(v) and v.pipeline_status in vs.CANDIDATE_STATUSES and v.seen_by_user_at is None
    )
    # only the letters that can still go out: not the sent, rejected or filtered vacancies among the six
    assert counts["needs_clarification"] == sum(
        1 for v in rows[:6] if visible(v) and v.pipeline_status in vs.CLARIFY_STATUSES
    )
    async with runtime.session_factory() as session:
        listing = await vs.list_vacancies(session, group="needs_clarification")
    assert listing["total"] == counts["needs_clarification"] > 0
    assert {v["pipeline_status"] for v in listing["items"]} <= set(vs.CLARIFY_STATUSES)
    assert counts["candidates"] == sum(by_status.get(s, 0) for s in vs.CANDIDATE_STATUSES)


@pytest.mark.parametrize("sort", ["score", "published", "responses", "salary"])
async def test_pages_follow_the_sort_order(runtime: Runtime, sort: str) -> None:
    now = utcnow()
    async with runtime.session_factory() as session:
        rows = [
            _vacancy(
                i,
                score=(50 + i % 4) if i % 3 else None,
                prescore=40 + i % 5,
                responses_count=(i % 6) if i % 4 else None,
                salary_net_rur=(100_000 + 10_000 * (i % 3)) if i % 2 else None,
                published_at=now - timedelta(hours=i % 7),
            )
            for i in range(23)
        ]
        session.add_all(rows)
        await session.commit()
        got: list[int] = []
        for page in range(4):
            items = (await vs.list_vacancies(session, group="all", sort=sort, page=page, per_page=7))["items"]
            got += [i["id"] for i in items]
            assert all(i["has_full"] for i in items)  # the full rows came with the page

    def key(v: Vacancy) -> tuple[Any, ...]:
        pub = -v.published_at.timestamp()  # type: ignore[union-attr]
        if sort == "published":
            return (pub, -v.id)
        if sort == "responses":
            return (v.responses_count is None, v.responses_count or 0, pub, -v.id)
        if sort == "salary":
            return (v.salary_net_rur is None, -(v.salary_net_rur or 0), pub, -v.id)
        best = v.score if v.score is not None else v.prescore if v.prescore is not None else -1
        return (-best, pub, -v.id)

    assert got == [v.id for v in sorted(rows, key=key)]


async def test_sent_rows_are_projections_not_partial_rows(runtime: Runtime) -> None:
    async with runtime.session_factory() as session:
        v = _vacancy(1, source_search_ids=",1,")
        session.add(v)
        await session.flush()
        session.add(Application(vacancy_id=v.id, mode="api", status="sent", sent_at=utcnow(), letter_text="Письмо"))
        session.add(Application(vacancy_id=999_999, mode="external", status="sent", sent_at=utcnow()))
        await session.commit()
    async with runtime.session_factory() as session:
        period = st.resolve_period(runtime.settings, "7d")
        rows = await st._load_sent(session, period)
        assert {type(r.vacancy).__name__ for r in rows} == {"SentVacancy", "NoneType"}
        known = next(r for r in rows if r.vacancy is not None)
        assert known.vacancy.name == "Python developer 1" and known.app.letter_text == "Письмо" and known.neg is None
        # the same session then loads the whole vacancy: nothing half-loaded was left in its identity map
        full = await session.get(Vacancy, v.id)
        assert full is not None and full.description_text and full.raw_full_json


async def test_journal_reminder_reads_columns_only(runtime: Runtime) -> None:
    old = utcnow() - timedelta(days=10)
    async with runtime.session_factory() as session:
        state = await get_system_state(session)
        state.capabilities_json = json.dumps({"negotiations_list": "unavailable"})
        v = _vacancy(1)
        session.add(v)
        await session.flush()
        session.add(Application(vacancy_id=v.id, mode="assist", status="sent", sent_at=old))
        session.add(Application(vacancy_id=424242, mode="assist", status="sent", sent_at=old))
        session.add(Application(vacancy_id=v.id, mode="assist", status="sent", sent_at=old, hh_negotiation_id="n1"))
        session.add(Negotiation(hh_id="n1", state_id="invitation"))
        await session.commit()
        plans = await _plans(runtime, st.journal_reminder)
        reminder = await st.journal_reminder(session)
    assert reminder["total"] == 2
    assert {i["name"] for i in reminder["items"]} == {"Python developer 1", "#424242"}
    assert not [s for s, _ in plans if "raw_full_json" in s]


# ---- data generation ----------------------------------------------------------------------------------------------


async def test_committed_writes_bump_the_data_generation(runtime: Runtime) -> None:
    async with runtime.session_factory() as session:
        g = changes.generation()
        session.add(_vacancy(1))
        await session.flush()
        assert changes.generation() == g  # not committed yet
        await session.commit()
        assert changes.generation() == g + 1
        session.add(_vacancy(2))
        await session.flush()
        await session.rollback()
        assert changes.generation() == g + 1  # rolled back
        await session.execute(update(Vacancy).values(prescore=99))
        await session.commit()
        assert changes.generation() == g + 2  # a bulk update counts too
        session.add(AuditLog(area="t", message="журнал"))
        await session.commit()
        assert changes.generation() == g + 2  # a table the analytics do not read
        await session.commit()
        assert changes.generation() == g + 2  # an empty commit


# ---- the shared analytics base -------------------------------------------------------------------------------------


async def test_analytics_base_is_shared_and_rebuilt_after_a_write(runtime: Runtime, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async with runtime.session_factory() as session:
        session.add_all([_vacancy(i, pipeline_status="filtered_out" if i == 0 else "prescored") for i in range(5)])
        await session.commit()
    loads = 0
    real = an.load_base

    async def counting(*args: Any, **kwargs: Any) -> an.Base:
        nonlocal loads
        loads += 1
        await asyncio.sleep(0.05)  # long enough for the concurrent callers to arrive meanwhile
        return await real(*args, **kwargs)

    monkeypatch.setattr(an, "load_base", counting)
    sf, s = runtime.session_factory, runtime.settings
    passed, everything, evaluated, again = await asyncio.gather(
        an.cached_base(sf, s, scope="passed"),
        an.cached_base(sf, s, scope="all"),
        an.cached_base(sf, s, scope="evaluated"),
        an.cached_base(sf, s, scope="passed"),
    )
    assert loads == 1 and passed is again
    assert len(everything.vacancies) == 5 and len(passed.vacancies) == 4 and evaluated.vacancies == []
    assert passed.sent is everything.sent  # the scopes are cut from one base
    assert await an.cached_base(sf, s, scope="passed") is passed and loads == 1

    async with runtime.session_factory() as session:
        session.add(_vacancy(9))
        await session.commit()
    fresh = await an.cached_base(sf, s, scope="passed")
    assert loads == 2 and len(fresh.vacancies) == 5


async def test_a_request_after_a_write_waits_for_a_newer_build(runtime: Runtime, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    started, release = asyncio.Event(), asyncio.Event()
    real = an.load_base
    calls = 0

    async def gated(*args: Any, **kwargs: Any) -> an.Base:
        nonlocal calls
        calls += 1
        first = calls == 1
        base = await real(*args, **kwargs)
        if first:
            started.set()
            await release.wait()  # the first build is slow: its data predate the write below
        return base

    monkeypatch.setattr(an, "load_base", gated)
    sf, s = runtime.session_factory, runtime.settings
    first = asyncio.create_task(an.cached_base(sf, s, scope="all"))
    await started.wait()
    async with runtime.session_factory() as session:
        session.add(_vacancy(1))
        await session.commit()
    second = await an.cached_base(sf, s, scope="all")
    assert calls == 2 and len(second.vacancies) == 1
    release.set()
    assert (await first).vacancies == []
    # the older build finished last but does not replace the newer base
    assert await an.cached_base(sf, s, scope="all") is second


async def test_a_failed_build_is_not_cached(runtime: Runtime, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    real = an.load_base
    fail = True

    async def flaky(*args: Any, **kwargs: Any) -> an.Base:
        if fail:
            raise RuntimeError("база занята")
        return await real(*args, **kwargs)

    monkeypatch.setattr(an, "load_base", flaky)
    results = await asyncio.gather(
        an.cached_base(runtime.session_factory, runtime.settings),
        an.cached_base(runtime.session_factory, runtime.settings),
        return_exceptions=True,
    )
    assert all(isinstance(r, RuntimeError) for r in results)
    fail = False
    assert isinstance(await an.cached_base(runtime.session_factory, runtime.settings), an.Base)


async def test_cpu_work_runs_off_the_event_loop_thread() -> None:
    loop_thread = threading.get_ident()
    assert await an.run_cpu(threading.get_ident) != loop_thread


async def test_reports_are_kept_with_the_base(runtime: Runtime, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import httpx
    from httpx import ASGITransport

    from app.main import app

    app.state.runtime = runtime
    async with runtime.session_factory() as session:
        session.add_all([_vacancy(i) for i in range(3)])
        await session.commit()
    computed = 0
    real = an.segments_report

    def counting(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal computed
        computed += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(an, "segments_report", counting)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        a = (await client.get("/api/analytics/segments", params={"by": "search"})).json()
        b = (await client.get("/api/analytics/segments", params={"by": "search"})).json()
        assert a == b and computed == 1
        await client.get("/api/analytics/segments", params={"by": "format"})
        assert computed == 2  # another slice is another report
        async with runtime.session_factory() as session:
            await session.execute(update(Vacancy).values(pipeline_status="maybe"))
            await session.commit()
        c = (await client.get("/api/analytics/segments", params={"by": "search"})).json()
        assert computed == 3 and c["total"]["found"] == 3


# ---- the statistics cache -----------------------------------------------------------------------------------------


async def test_statistics_cache_shows_fresh_data_of_its_own_database(runtime: Runtime, tmp_path) -> None:  # type: ignore[no-untyped-def]
    s = runtime.settings
    async with runtime.session_factory() as session:
        first = await st.compute_stats(session, s, period_key="7d")
        assert first["metrics"]["sent"] == 0
        session.add(Application(vacancy_id=1, mode="api", status="sent", sent_at=utcnow()))
        await session.commit()
        # the write shows at once, not after the 60-second cache
        assert (await st.compute_stats(session, s, period_key="7d"))["metrics"]["sent"] == 1

    other = await Runtime.create(s.model_copy(update={"data_dir": tmp_path / "other", "database_url": ""}))
    try:
        async with other.session_factory() as session:
            assert (await st.compute_stats(session, other.settings, period_key="7d"))["metrics"]["sent"] == 0
    finally:
        await other.close()


async def test_evaluation_order_is_deterministic(runtime: Runtime) -> None:
    """Two «current» evaluations of one vacancy (never expected, but possible after a crash): the newest wins."""
    async with runtime.session_factory() as session:
        v = _vacancy(1)
        session.add(v)
        await session.flush()
        for score in (40, 90):
            session.add(
                VacancyEvaluation(vacancy_id=v.id, batch_id=f"b{score}", score=score, verdict="maybe", summary="")
            )
        await session.commit()
        base = await an.load_base(session, runtime.settings, scope="all")
    assert base.evals[v.id].score == 90
