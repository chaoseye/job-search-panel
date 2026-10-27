"""Analytics of vacancy segments: rule tags at scan time, segment comparison, hot list, skills/gaps, salary,
funnel by dimension, employers, trends, retag; Claude tags in the evaluation schema and the vacancy detail."""

from __future__ import annotations

import json
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import respx
from sqlalchemy import delete, func, select

from app.db.base import as_utc, utcnow
from app.db.models import Application, Notification, Vacancy, VacancyEvaluation
from app.exchange.schemas import Evaluation
from app.scheduler import build_scheduler, hot_digest_catch_up
from app.services.bot_commands import handle_command
from app.services.freshness import check_closed
from app.services.hot_digest import LAST_RUN_SETTING, hot_digest_tick, last_run
from app.services.notify import emit_event, render_event
from app.services.state import set_setting
from tests.integration.test_api_m2 import BASE, DICTS, _full, _items, _login
from tests.integration.test_api_selection import api  # noqa: F401

__all__ = ["api"]


def _stamp(hours_ago: float) -> str:
    return (utcnow() - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")


VACANCIES = {
    # fresh, few responses, remote, well paid: Python/FastAPI (the fixture) — later gets an application
    "1": dict(published_at=_stamp(5), counters={"responses": 42}),
    # hybrid RAG job in a bank, crowded
    "2": dict(
        name="ML-инженер (RAG, LLM)",
        key_skills=[{"name": "Python"}, {"name": "LangChain"}, {"name": "Qdrant"}],
        description="<p>Строим RAG и чат-ботов для банка. Нужен опыт с LangChain и векторными базами.</p>",
        salary={"from": 200000, "to": None, "currency": "RUR", "gross": False},
        work_format=[{"id": "HYBRID", "name": "Гибрид"}],
        experience={"id": "noExperience", "name": "Нет опыта"},
        counters={"responses": 500},
        published_at=_stamp(30),
        has_test=True,
    ),
    # an MVP shop, no salary, brand new, almost no responses
    "3": dict(
        name="Fullstack-разработчик MVP",
        key_skills=[{"name": "Cursor"}, {"name": "Claude Code"}, {"name": "Python"}],
        description="<p>Собираем MVP на Cursor и Claude Code для стартапа в e-commerce; n8n для автоматизации.</p>",
        salary=None,
        salary_range=None,
        counters={"responses": 10},
        published_at=_stamp(1),
    ),
}


async def _seed(client, runtime, m: respx.MockRouter) -> tuple[dict[str, int], int]:
    await _login(client, m)
    m.get(f"{BASE}/dictionaries").mock(return_value=httpx.Response(200, json=DICTS))
    m.get(f"{BASE}/industries").mock(return_value=httpx.Response(200, json=[]))
    r = await client.post(
        "/api/profile/resumes/upload",
        files={"file": ("cv.txt", "Иван\nКлючевые навыки: Python, FastAPI, Docker".encode(), "text/plain")},
    )
    assert r.status_code == 200
    prefs = (await client.get("/api/profile/preferences")).json()
    prefs.update(
        {
            "target_titles": ["Python developer", "ML-инженер", "Fullstack-разработчик"],
            "skills_weights": [{"name": "Python", "weight": 5}, {"name": "Cursor", "weight": 4}],
            "salary_min": 150000,
        }
    )
    assert (await client.put("/api/profile/preferences", json=prefs)).status_code == 200
    sid = (
        await client.post(
            "/api/searches", json={"name": "Разработка", "params": {"text": "developer"}, "interval_min": 30}
        )
    ).json()["id"]
    m.get(f"{BASE}/vacancies").mock(
        return_value=httpx.Response(200, json={"found": 3, "pages": 1, "items": _items(list(VACANCIES))})
    )
    for i, over in VACANCIES.items():
        m.get(f"{BASE}/vacancies/{i}").mock(return_value=httpx.Response(200, json=_full(i, **over)))
    await client.post(f"/api/searches/{sid}/run")
    await runtime.jobs.wait("scan")
    rows = (await client.get("/api/vacancies", params={"group": "all"})).json()["items"]
    ids = {v["hh_id"]: v["id"] for v in rows}
    assert set(ids) == {"1", "2", "3"}
    return ids, sid


@respx.mock(assert_all_called=False)
async def test_analytics_reports(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    ids, sid = await _seed(client, runtime, respx_mock)
    now = utcnow()

    # rule tags were computed at scan time
    detail = (await client.get(f"/api/vacancies/{ids['2']}")).json()
    assert "финтех и банки" in detail["tags"]["domain"] and "LangChain" in detail["tags"]["stack"]
    assert "RAG / поиск по документам" in detail["tags"]["tasks"]

    # evaluations (as the importer would store them) + two sent applications, one with a reply
    evals = {
        "1": (75, "apply", ["нет опыта с Kafka"], [], "within", {"stack": ["Kafka", "python"], "domain": ["финтех"]}),
        "2": (55, "maybe", ["нужен LangGraph", "нет опыта с Kafka"], ["агентство"], "below", None),
        "3": (60, "maybe", [], [], "unknown", {"tasks": ["vibe-coding MVP"]}),
    }
    async with runtime.session_factory() as session:
        for hh_id, (score, verdict, gaps, flags, fit, tags) in evals.items():
            session.add(
                VacancyEvaluation(
                    vacancy_id=ids[hh_id],
                    batch_id="t",
                    score=score,
                    verdict=verdict,
                    summary="",
                    matched_json=json.dumps(["Python"]),
                    gaps_json=json.dumps(gaps, ensure_ascii=False),
                    red_flags_json=json.dumps(flags, ensure_ascii=False),
                    salary_fit=fit,
                    seniority_fit="match",
                    tags_json=json.dumps(tags, ensure_ascii=False) if tags else None,
                )
            )
            row = (await session.execute(select(Vacancy).where(Vacancy.id == ids[hh_id]))).scalar_one()
            row.score = score
            row.pipeline_status = "sent" if hh_id == "1" else "maybe"
        session.add(
            Application(
                vacancy_id=ids["1"], mode="assist", status="sent", sent_at=now - timedelta(days=8), letter_text="Письмо"
            )
        )
        session.add(Application(vacancy_id=ids["2"], mode="assist", status="sent", sent_at=now - timedelta(days=1)))
        await session.commit()
    apps = {a["vacancy_id"]: a["id"] for a in (await client.get("/api/applications")).json()["items"]}
    r = await client.post(f"/api/applications/{apps[ids['1']]}/manual-update", json={"state": "invitation"})
    assert r.status_code == 200, r.text

    # segments by search: everything found by the one search
    seg = (await client.get("/api/analytics/segments", params={"by": "search"})).json()
    assert seg["label"] == "по поискам" and [i["label"] for i in seg["items"]] == ["Разработка"]
    s = seg["items"][0]
    assert s["found"] == 3 and s["evaluated"] == 3 and s["ge50"] == 3 and s["ge70"] == 1 and s["ge70_pct"] == 33.3
    assert s["salary_n"] == 2 and s["no_salary_pct"] == 33.3 and s["remote_pct"] == 66.7 and s["hybrid_pct"] == 33.3
    assert s["test_pct"] == 33.3 and s["responses_median"] == 42 and s["low_competition_pct"] == 66.7
    assert s["sent"] == 2 and s["invited"] == 1 and s["invited_pct"] == 50.0
    assert s["experience"] == {"3–6 лет": 2, "без опыта": 1}
    assert seg["total"]["found"] == 3
    # by target title: «Python developer» matches the fixture name, «ML-инженер» and «Fullstack-разработчик» one each
    by_title = {i["label"]: i for i in (await client.get("/api/analytics/segments?by=title")).json()["items"]}
    assert by_title["Python developer"]["found"] == 1 and by_title["ML-инженер"]["found"] == 1
    assert by_title["Fullstack-разработчик"]["found"] == 1
    # by stack tag: Python is in all three (key skills) and Claude's «python» merged into the same label
    by_stack = {i["label"]: i for i in (await client.get("/api/analytics/segments?by=stack")).json()["items"]}
    assert by_stack["Python"]["found"] == 3 and by_stack["Claude Code"]["found"] == 1
    assert by_stack["Airflow / Kafka / Spark"]["found"] == 1  # Claude's «Kafka» mapped onto the panel's label
    by_tasks = {i["label"]: i for i in (await client.get("/api/analytics/segments?by=tasks")).json()["items"]}
    assert by_tasks["разработка продукта / vibe-coding"]["found"] == 1  # Claude's free label mapped onto ours
    assert (await client.get("/api/analytics/segments?by=nope")).status_code == 400
    assert (await client.get("/api/analytics/segments?scope=nope")).status_code == 400
    # scope: everything here passed the rules and is evaluated, so all three scopes agree; a hidden one drops out
    for scope in ("all", "passed", "evaluated"):
        assert (await client.get(f"/api/analytics/segments?scope={scope}")).json()["total"]["found"] == 3
    assert (await client.post(f"/api/vacancies/{ids['3']}/hide")).status_code == 200
    assert (await client.get("/api/analytics/segments?scope=passed")).json()["total"]["found"] == 2
    assert (await client.get("/api/analytics/segments?scope=all")).json()["total"]["found"] == 3
    assert (await client.post(f"/api/vacancies/{ids['3']}/restore")).status_code == 200
    kinds = (await client.get("/api/analytics/kinds")).json()
    assert {k["id"] for k in kinds["segments"]} >= {"search", "title", "stack"} and len(kinds["funnel"]) > 8

    # hot: fresh + few responses + score ≥ 50 and not handled yet → only «3» («1» is applied to, «2» crowded)
    hot = (await client.get("/api/analytics/hot", params={"max_responses": 50, "max_age_days": 3})).json()
    assert [i["hh_id"] for i in hot["items"]] == ["3"] and hot["items"][0]["age_hours"] < 2
    hot = (
        await client.get("/api/analytics/hot", params={"max_responses": 1000, "max_age_days": 3, "min_score": 60})
    ).json()
    assert [i["hh_id"] for i in hot["items"]] == ["3"]  # «2» is 30 h old but scores 55

    # skills and gaps of the search segment
    sk = (await client.get("/api/analytics/skills", params={"by": "search", "key": f"s{sid}"})).json()
    top = {s["name"]: s for s in sk["skills"]}
    assert top["Python"]["count"] == 3 and top["Python"]["in_resume"] is True and top["Python"]["share"] == 100.0
    assert top["Kafka"]["in_resume"] is False and "Kafka" in [m["name"] for m in sk["missing"]]
    assert "Cursor" not in [m["name"] for m in sk["missing"]]  # weighted in the questionnaire → counts as mine
    assert sk["gaps"][0] == {"text": "нет опыта с Kafka", "count": 2}
    assert {"name": "Kafka", "count": 2} in sk["gap_skills"]
    assert sk["red_flags"] == [{"text": "агентство", "count": 1}] and sk["evaluated"] == 3

    # salary: two vacancies with money; mine = 150 000 → both at or above; fits from evaluations
    sal = (await client.get("/api/analytics/salary")).json()
    assert sal["overall"]["n"] == 2 and sal["overall"]["of"] == 3 and sal["overall"]["median"] >= 200000
    assert sal["mine"] == {"expected": 150000, "at_or_above": 2, "at_or_above_pct": 100.0}
    assert sal["salary_fit"] == {"within": 1, "below": 1, "unknown": 1}
    assert [x["label"] for x in sal["by_format"]] == ["удалённо", "гибрид"]
    assert sal["top_paid"][0]["name"] == "Python developer 1" and sal["buckets"]["не указана"] == 1

    # funnel by letter: the application with a letter got the invitation
    fn = (await client.get("/api/analytics/funnel", params={"by": "letter"})).json()
    rows = {r["label"]: r for r in fn["rows"]}
    assert rows["с письмом"]["sent"] == 1 and rows["с письмом"]["invited"] == 1
    assert rows["без письма"]["sent"] == 1 and rows["без письма"]["pending"] == 1
    assert fn["overall"]["sent"] == 2 and fn["overall"]["invited_pct"] == 50.0 and fn["enough_data"] is False
    fn = (await client.get("/api/analytics/funnel", params={"by": "score"})).json()
    assert {r["label"] for r in fn["rows"]} == {"70–79", "50–69"}
    assert (await client.get("/api/analytics/funnel?by=weekday")).status_code == 400

    # employers: the fixture employer holds all three, sorted by score
    emp = (await client.get("/api/analytics/employers")).json()
    e = emp["items"][0]
    assert e["name"] == "Рога и копыта" and e["vacancies"] == 3 and e["sent"] == 2 and e["invited"] == 1
    assert e["url"] == "https://hh.ru/employer/1455" and e["best_score"] == 75 and e["accredited"] is True
    assert [i["score"] for i in e["items"]] == [75, 60, 55]

    # trends: everything landed this week
    tr = (await client.get("/api/analytics/trends", params={"weeks": 4})).json()
    assert tr["weeks"][-1]["new"] == 3 and tr["weeks"][-1]["ge50"] == 3
    assert sum(w["sent"] for w in tr["weeks"]) == 2
    assert "Python" in tr["tools"] and tr["weeks"][-1]["tools"]["Python"] == 3
    assert tr["tags"]["stack"][0]["name"] == "Python"

    # retag recomputes the rule tags for every vacancy with a description
    assert (await client.post("/api/analytics/retag")).json() == {"retagged": 3}

    # recommendations: three vacancies are too few for term statistics, but the actions work
    rec = (await client.get("/api/analytics/recommendations")).json()
    assert rec["evaluated"] == 3 and rec["good"] == 3 and rec["enough_data"] is False
    assert set(rec) >= {"searches", "stop_words", "weights", "resume", "gaps", "focus"}
    assert rec["resume"] == [] and rec["searches"] == []  # Kafka is asked by one vacancy only; no term has 3 uses
    r = await client.post(
        "/api/analytics/recommendations/search", json={"term": "data инженер", "query": '"data инженер"'}
    )
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["name"] == "data инженер (по названию)" and created["enabled"] is False
    assert created["params"]["text"] == '"data инженер"' and created["params"]["search_field"] == ["name"]
    assert (
        await client.post("/api/analytics/recommendations/search", json={"term": "data инженер"})
    ).status_code == 409
    r = await client.post("/api/analytics/recommendations/weights", json={"names": ["Kafka", "Python"]})
    assert r.json() == {"changed": ["Kafka"]}  # Kafka added with weight 5; Python already has 5
    prefs = (await client.get("/api/profile/preferences")).json()
    weights = {w["name"]: w["weight"] for w in prefs["skills_weights"]}
    assert weights["Kafka"] == 5 and weights["Python"] == 5 and weights["Cursor"] == 4
    assert (await client.post("/api/analytics/recommendations/weights", json={"names": ["Kafka"]})).json() == {
        "changed": []
    }
    r = await client.post("/api/analytics/recommendations/stop-words", json={"names": ["менеджер", "Менеджер", ""]})
    assert r.json() == {"added": ["менеджер"]}
    assert "менеджер" in (await client.get("/api/profile/preferences")).json()["stop_words_title"]


@respx.mock(assert_all_called=False)
async def test_hot_digest_and_bot_command(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    ids, _sid = await _seed(client, runtime, respx_mock)
    async with runtime.session_factory() as session:
        for hh_id, score in (("1", 75), ("2", 55), ("3", 60)):
            row = (await session.execute(select(Vacancy).where(Vacancy.id == ids[hh_id]))).scalar_one()
            session.add(VacancyEvaluation(vacancy_id=row.id, batch_id="t", score=score, verdict="maybe", summary=""))
            row.score = score
            row.pipeline_status = "maybe"
        await session.commit()
    # «1» (5 h, 42 responses) and «3» (1 h, 10 responses) are hot; «2» is crowded
    result = await hot_digest_tick(runtime)
    assert result["ok"] and result["items"] == 2 and result["telegram"] is False  # no bot token in this fixture
    feed = (await client.get("/api/notifications")).json()
    assert feed[0]["event_type"] == "hot" and feed[0]["summary"] == "Горячие вакансии: 2"
    assert [i["name"] for i in feed[0]["payload"]["items"]] == ["Python developer 1", "Fullstack-разработчик MVP"]
    # the same vacancies are not reported again; force ignores the memory
    assert (await hot_digest_tick(runtime))["skipped"].startswith("нет новых")
    assert (await hot_digest_tick(runtime, force=True))["items"] == 2
    assert (await client.post("/api/analytics/hot-digest")).json()["items"] == 2
    # the bot command renders the current list
    async with runtime.session_factory() as session:
        text = await handle_command(runtime, session, "/hot")
        assert text.startswith("🔥 <b>Горячие вакансии: 2</b>") and "оценка 75" in text and "42 откл." in text
        assert "/hot" in await handle_command(runtime, session, "/help")
    # scheduler wiring: the digest cron only when HOT_DIGEST_AT is set, the freshness cron by default
    jobs = {j.id for j in build_scheduler(runtime).get_jobs()}
    assert "freshness_daily" in jobs and "hot_digest" not in jobs and "hot_digest_catch_up" not in jobs
    runtime.settings = runtime.settings.model_copy(update={"hot_digest_at": "09:00", "freshness_stale_days": 0})
    jobs = {j.id for j in build_scheduler(runtime).get_jobs()}
    assert {"hot_digest", "hot_digest_catch_up"} <= jobs and "freshness_daily" not in jobs


@respx.mock(assert_all_called=False)
async def test_missed_morning_digest_comes_late_once(api, respx_mock: respx.MockRouter) -> None:
    """The computer was off at HOT_DIGEST_AT (switched on after noon, the 09:00 digest never came): the
    digest runs once the panel is back, marked as late; a run that found nothing new counts as done."""
    client, runtime = api
    ids, _sid = await _seed(client, runtime, respx_mock)
    async with runtime.session_factory() as session:
        for hh_id, score in (("1", 75), ("2", 55), ("3", 60)):
            row = (await session.execute(select(Vacancy).where(Vacancy.id == ids[hh_id]))).scalar_one()
            session.add(VacancyEvaluation(vacancy_id=row.id, batch_id="t", score=score, verdict="maybe", summary=""))
            row.score = score
            row.pipeline_status = "maybe"
        await session.commit()
    # the slot is «an hour ago» in Moscow time: the vacancies of the fixture are hot relative to the real clock
    now = utcnow()
    local = now.astimezone(ZoneInfo("Europe/Moscow"))

    def slot_minutes_ago(minutes: int) -> str:
        return (local - timedelta(minutes=minutes)).strftime("%H:%M")

    runtime.settings = runtime.settings.model_copy(
        update={"timezone": "Europe/Moscow", "hot_digest_at": slot_minutes_ago(60)}
    )
    # a panel where the digest never ran waits for its first regular one
    assert await hot_digest_catch_up(runtime) is False
    # the last digest came yesterday (a base from before the run mark: the feed row tells when)
    async with runtime.session_factory() as session:
        payload = {"total": 1, "items": [{"id": 0, "name": "вчерашняя"}]}
        await emit_event(session, runtime.settings, "hot", payload, now=now - timedelta(days=1, hours=2))
        await session.commit()
    # up to 20 minutes after the slot the regular job may still run (its grace is 15): no catch-up
    runtime.settings = runtime.settings.model_copy(update={"hot_digest_at": slot_minutes_ago(10)})
    assert await hot_digest_catch_up(runtime) is False
    runtime.settings = runtime.settings.model_copy(update={"hot_digest_at": slot_minutes_ago(60)})
    assert await hot_digest_catch_up(runtime) is True
    feed = (await client.get("/api/notifications")).json()
    late = feed[0]["payload"]["late"]
    assert feed[0]["event_type"] == "hot" and late.endswith(slot_minutes_ago(60))
    assert feed[0]["summary"] == "Горячие вакансии: 2 (с опозданием)"
    text = render_event("hot", [feed[0]["payload"]])
    assert f"С опозданием: в {late} панель не работала" in text and "Python developer 1" in text
    # it ran → the ticks until the next morning do nothing
    assert await hot_digest_catch_up(runtime) is False

    # the next missed morning finds nothing new: skipped, but its run is marked — no retry every 15 minutes
    async with runtime.session_factory() as session:
        await set_setting(session, LAST_RUN_SETTING, (now - timedelta(days=1)).isoformat())
        await session.execute(delete(Notification).where(Notification.event_type == "hot"))
        await session.commit()
    assert await hot_digest_catch_up(runtime) is True  # ran: both vacancies were reported a moment ago
    async with runtime.session_factory() as session:
        count = select(func.count()).select_from(Notification).where(Notification.event_type == "hot")
        assert (await session.execute(count)).scalar() == 0  # nothing sent
        mark = await last_run(session)
        assert mark is not None and mark >= now - timedelta(minutes=1)
    assert await hot_digest_catch_up(runtime) is False
    # the bot command /hot is not a digest run and changes nothing
    async with runtime.session_factory() as session:
        before = await last_run(session)
        await handle_command(runtime, session, "/hot")
        assert await last_run(session) == before


@respx.mock(assert_all_called=False)
async def test_freshness_check_closes_stale_candidates(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    ids, _sid = await _seed(client, runtime, respx_mock)
    stale = utcnow() - timedelta(days=3)
    async with runtime.session_factory() as session:
        for hh_id, status in (("1", "sent"), ("2", "maybe"), ("3", "prescored")):
            row = (await session.execute(select(Vacancy).where(Vacancy.id == ids[hh_id]))).scalar_one()
            row.pipeline_status = status
            row.last_seen_at = stale
        await session.commit()
    # «1» is sent → not checked; «2» is gone; «3» is alive with a new responses counter
    respx_mock.get(f"{BASE}/vacancies/2").mock(return_value=httpx.Response(404, json={"errors": []}))
    respx_mock.get(f"{BASE}/vacancies/3").mock(
        return_value=httpx.Response(200, json=_full("3", **{**VACANCIES["3"], "counters": {"responses": 99}}))
    )
    summary = await check_closed(runtime, stale_days=2)
    assert summary == {"checked": 2, "closed": 1, "refreshed": 1, "errors": 0, "candidates": 2}
    rows = {v["hh_id"]: v for v in (await client.get("/api/vacancies", params={"group": "all"})).json()["items"]}
    assert rows["2"]["pipeline_status"] == "filtered_out" and "удалена" in rows["2"]["pipeline_reason"]
    assert (
        rows["2"]["archived"] is True and rows["3"]["responses_count"] == 99 and rows["1"]["pipeline_status"] == "sent"
    )
    async with runtime.session_factory() as session:
        row = (await session.execute(select(Vacancy).where(Vacancy.id == ids["3"]))).scalar_one()
        assert as_utc(row.last_seen_at) > stale
    # nothing stale left → nothing checked; archived flag from the card closes too
    assert (await check_closed(runtime, stale_days=2))["candidates"] == 0
    respx_mock.get(f"{BASE}/vacancies/3").mock(
        return_value=httpx.Response(200, json=_full("3", **{**VACANCIES["3"], "archived": True}))
    )
    assert (await check_closed(runtime, stale_days=0))["closed"] == 1
    rows = {v["hh_id"]: v for v in (await client.get("/api/vacancies", params={"group": "all"})).json()["items"]}
    assert "архив" in rows["3"]["pipeline_reason"]
    # the manual trigger runs the same job
    r = await client.post("/api/jobs/freshness")
    assert r.status_code == 200 and r.json()["job_run_id"]
    await runtime.jobs.wait("freshness")


def test_evaluation_schema_accepts_tags() -> None:
    ev = Evaluation.model_validate(
        {"score": 70, "verdict": "apply", "tags": {"stack": ["n8n", " Cursor ", ""], "domain": "x", "extra": 1}}
    )
    assert ev.tags is not None and ev.tags.stack == ["n8n", "Cursor"] and ev.tags.domain == []
    assert Evaluation.model_validate({"score": 1, "verdict": "skip", "tags": "junk"}).tags is None
    assert Evaluation.model_validate({"score": 1, "verdict": "skip"}).tags is None
