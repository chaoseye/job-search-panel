"""Recommendations from the analytics base: search terms, stop words, weights, resume, focus."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.services.analytics import Base, E, V, name_terms, recommendations_report

NOW = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def _v(i: int, name: str, skills: list[str], *, search: int = 1) -> V:
    return V(
        id=i,
        hh_id=str(i),
        name=name,
        url=None,
        employer_id="e",
        employer_name="E",
        accredited=True,
        trusted=True,
        area=None,
        salary=100_000 + i * 1000,
        experience="between1And3",
        formats=["REMOTE"],
        published_at=NOW - timedelta(hours=i),
        first_seen_at=NOW - timedelta(hours=i),
        archived=False,
        direct=False,
        has_test=False,
        letter_required=False,
        key_skills=skills,
        responses=10 * i,
        status="evaluated",
        prescore=40,
        score=None,
        set_id=None,
        search_ids=[search],
        hidden=False,
        tags={"domain": [], "tasks": ["парсинг и данные"] if "пайплайн" in name.lower() else [], "stack": []},
    )


def _e(v: V, score: int) -> E:
    v.score = score
    verdict = "apply" if score >= 70 else "maybe" if score >= 50 else "skip"
    return E(v.id, score, verdict, "", [], [], [], None, None, {})


def test_name_terms() -> None:
    assert name_terms("Senior Data-инженер (Spark пайплайнов)") == [
        "data",
        "инженер",
        "spark",
        "пайплайнов",
        "data инженер",
        "инженер spark",
        "spark пайплайнов",
    ]
    assert name_terms("Junior 2024") == []


def test_recommendations_report() -> None:
    vs = [
        _v(1, "Data-инженер пайплайнов", ["Python", "Kafka"]),
        _v(2, "Инженер по внедрению BI", ["Python", "Kafka"]),
        _v(3, "Spark инженер", ["Python", "Docker"]),
        _v(4, "Инженер поддержки", ["Excel"]),
        _v(5, "Менеджер по продажам", ["Продажи"]),
        _v(6, "Менеджер проектов Data", ["Jira"]),
        _v(7, "Менеджер по работе с клиентами", ["CRM"]),
        _v(8, "Менеджер BI-продукта", ["Python"]),
        _v(9, "Аналитик", ["Tableau"]),  # never evaluated → only «potential»
    ]
    evals = {v.id: _e(v, s) for v, s in zip(vs[:8], (80, 70, 65, 20, 10, 15, 5, 12), strict=True)}
    base = Base(
        vacancies=vs,
        evals=evals,
        sent=[],
        search_names={1: "Data"},
        set_names={},
        titles=["Data-инженер"],
        my_skills=["Python", "Tableau"],
        my_salary=None,
        hidden_employers=set(),
        tz=ZoneInfo("Europe/Moscow"),
        now=NOW,
        my_weights={"python": 3},
        stop_words=["продаж"],
        search_texts=["data"],
    )
    r = recommendations_report(base)
    assert r["evaluated"] == 8 and r["good"] == 3 and r["enough_data"] is False
    # «инженер»: 4 evaluated, 3 good (75 %) and not covered by the searches/titles → suggested; «data» is covered
    terms = {s["term"]: s for s in r["searches"]}
    assert terms["инженер"] == {
        "term": "инженер",
        "query": "инженер",
        "evaluated": 4,
        "good": 3,
        "rate": 75.0,
        "in_base": 4,
    }
    assert "data" not in terms and "менеджер" not in terms
    # «менеджер»: 4 evaluated, none good → stop word; «продаж» is already a stop word
    assert [s["term"] for s in r["stop_words"]] == ["менеджер"]
    # Python is mine with weight 3 and demanded by all good vacancies → raise to 5; Kafka is not mine → resume
    assert r["weights"] == [{"name": "Python", "in_good": 3, "in_all": 4, "weight": 3, "suggested": 5}]
    assert r["resume"][0] == {"name": "Kafka", "in_good": 2, "in_all": 2}
    assert all(x["name"] != "Docker" for x in r["resume"])  # only one good vacancy asks for it
    # focus: the title segment «Data-инженер» (1 evaluated) is below the 3-evaluation floor; the search «Data» qualifies
    assert any(f["by"] == "search" and f["label"] == "Data" and f["ge50_pct"] == 37.5 for f in r["focus"])
    assert all(f["key"] not in ("other", "none") for f in r["focus"])
