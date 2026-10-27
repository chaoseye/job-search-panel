"""Statistics acceptance: 40 applications with known outcomes — metrics must match the hand calculation.

Fixture design (i = 0..39):
  mode:      i % 5 == 0 → external (8), i % 5 in (1, 2) → api (16), else assist (16)
  viewed:    i even → 20 viewed (first_viewed_at = sent + (i+1) h)
  invited:   i % 4 == 0 → 10 (all even, hence viewed); invited_at = sent + (i+1) h
  discarded: i % 8 == 3 → i ∈ {3, 11, 19, 27, 35} → 5 (odd, not viewed); discarded_at = sent + 48 h
  score:     50 + i → buckets 50–69: i<20 (20 sent, invited 0,4,8,12,16 = 5), 70–79: 20..29 (10, invited 3),
             80–89: 30..39 (10, invited 2)
  sent_at:   D0 + 6h·i (D0 = 2026-08-01 09:00 MSK) → 10 days; "now" = D0 + 30 days, so every row is > 7 days old
  rules:     rules_passed = (i % 10 != 9) → 36 of 40; evaluations for all 40 (suitable when score ≥ 70 → 20)
Hand-computed: sent 40 (api 16 / assist 16 / external 8), viewed 20, invitations 10, discards 5,
no_answer 25 (everything without invitation/discard/employer message — a view is not an answer),
conversions 50.0 / 25.0 / 50.0, median reaction 25.0 h (reactions: viewed even rows → 1,3,…,39 h (20 values),
discards → 48 h (5 values); the 13th of 25 sorted values is 25).
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.db.models import Application, Negotiation, Vacancy, VacancyEvaluation
from app.runtime import Runtime
from app.services.stats import clear_cache, compute_stats, export_aggregates_csv, export_applications_csv

TZ = ZoneInfo("Europe/Moscow")
D0 = datetime(2026, 8, 1, 9, 0, tzinfo=TZ)
NOW = D0 + timedelta(days=30)


def _mode(i: int) -> str:
    if i % 5 == 0:
        return "external"
    return "api" if i % 5 in (1, 2) else "assist"


@pytest.fixture
async def runtime(settings: Settings):
    rt = await Runtime.create(settings)
    clear_cache()
    try:
        yield rt
    finally:
        await rt.close()


async def _seed(rt: Runtime) -> None:
    # the panel stores UTC; SQLite keeps the naive part of whatever is passed, so convert before storing
    async with rt.session_factory() as session:
        for i in range(40):
            sent = (D0 + timedelta(hours=6 * i)).astimezone(UTC)
            v = Vacancy(
                hh_id=f"v{i}",
                name=f"Vacancy {i}",
                employer_name=f"Employer {i % 4}",
                area_name="Москва" if i % 2 else "Казань",
                salary_net_rur=120_000 + i * 5_000,
                work_format_names_json='["Удалённо"]' if i % 3 else '["В офисе"]',
                first_seen_at=sent - timedelta(days=1),
                rules_passed=(i % 10 != 9),
                pipeline_status="sent",
                score=50 + i,
                source_search_ids=",1," if i % 2 else ",2,",
            )
            session.add(v)
            await session.flush()
            ev = VacancyEvaluation(
                vacancy_id=v.id,
                batch_id="b1",
                score=50 + i,
                verdict="apply" if 50 + i >= 70 else "maybe",
                summary="",
                created_at=sent - timedelta(hours=12),
            )
            session.add(ev)
            app_row = Application(
                vacancy_id=v.id,
                mode=_mode(i),
                status="sent",
                sent_at=sent,
                hh_negotiation_id=f"n{i}",
                letter_text="x" * (100 + i),
                dry_run=False,
            )
            session.add(app_row)
            neg = Negotiation(
                hh_id=f"n{i}",
                vacancy_hh_id=v.hh_id,
                vacancy_id=v.id,
                state_id="response",
                hh_created_at=sent,
                hh_updated_at=sent,
            )
            viewed = i % 2 == 0
            invited = i % 4 == 0
            discarded = i % 8 == 3
            reaction = None
            if viewed:
                neg.viewed_by_opponent = True
                neg.first_viewed_at = sent + timedelta(hours=i + 1)
                reaction = neg.first_viewed_at
            if invited:
                neg.state_id = "invitation"
                neg.invited_at = sent + timedelta(hours=i + 1)
            if discarded:
                neg.state_id = "discard"
                neg.discarded_at = sent + timedelta(hours=48)
                reaction = neg.discarded_at
            neg.first_reaction_at = reaction
            session.add(neg)
        # a dry-run application and a queued one must not count
        session.add(Application(vacancy_id=1, mode="api", status="sent", sent_at=D0.astimezone(UTC), dry_run=True))
        session.add(Application(vacancy_id=2, mode="api", status="queued", queued_at=D0.astimezone(UTC)))
        await session.commit()


async def test_metrics_match_hand_calculation(runtime: Runtime) -> None:
    await _seed(runtime)
    async with runtime.session_factory() as session:
        stats = await compute_stats(session, runtime.settings, period_key="all", now=NOW, use_cache=False)
    m = stats["metrics"]
    assert m["found"] == 40 and m["passed_rules"] == 36 and m["evaluated"] == 40 and m["suitable"] == 20
    assert m["sent"] == 40 and m["sent_api"] == 16 and m["sent_assist"] == 16 and m["sent_external"] == 8
    assert m["viewed"] == 20 and m["invitations"] == 10 and m["discards"] == 5
    assert m["no_answer"] == 25
    assert m["conv_sent_viewed"] == 50.0 and m["conv_sent_invited"] == 25.0 and m["conv_viewed_invited"] == 50.0
    # reaction hours: even i → i+1 (1,3,…,39); discards (i=3,11,19,27,35) → 48
    expected = sorted([i + 1 for i in range(0, 40, 2)] + [48] * 5)
    assert m["median_reaction_hours"] == round(statistics.median(expected), 1) == 25.0
    assert m["remaining_day"] == runtime.settings.apply_max_per_day  # nothing sent "today" (NOW is far later)

    funnel = {f["stage"]: f["value"] for f in stats["funnel"]}
    assert funnel == {
        "found": 40,
        "passed_rules": 36,
        "evaluated": 40,
        "suitable": 20,
        "letters": 0,
        "sent": 40,
        "viewed": 20,
        "invitations": 10,
    }
    buckets = {b["bucket"]: b for b in stats["score_buckets"]}
    assert (
        buckets["50–69"]["sent"] == 20 and buckets["50–69"]["invited"] == 5 and buckets["50–69"]["invited_pct"] == 25.0
    )
    assert (
        buckets["70–79"]["sent"] == 10 and buckets["70–79"]["invited"] == 3 and buckets["70–79"]["invited_pct"] == 30.0
    )
    assert (
        buckets["80–89"]["sent"] == 10 and buckets["80–89"]["invited"] == 2 and buckets["80–89"]["invited_pct"] == 20.0
    )
    assert buckets["90–100"]["sent"] == 0 and "нет оценки" not in buckets

    # series: 40 sends over 10 days (4 per day), outcomes stacked
    series = {d["date"]: d for d in stats["series"]}
    assert sum(d["sent"] for d in series.values()) == 40
    assert sum(d["invited"] for d in series.values()) == 10 and sum(d["discarded"] for d in series.values()) == 5
    assert sum(d["viewed"] for d in series.values()) == 10  # viewed but not invited
    assert series["2026-08-01"]["sent"] == 3  # 09:00, 15:00, 21:00 local; 03:00 next day belongs to Aug 2
    assert series["2026-08-01"]["found"] == 4 and series["2026-07-31"]["found"] == 3

    by_search = {b["label"]: b for b in stats["breakdowns"]["by_search"]}
    assert by_search["поиск #1"]["sent"] == 20 and by_search["поиск #2"]["sent"] == 20
    assert (
        by_search["поиск #2"]["invited"] == 10 and by_search["поиск #1"]["invited"] == 0
    )  # invited rows are even → search 2
    by_employer = stats["breakdowns"]["by_employer"]
    assert len(by_employer) == 4 and sum(b["sent"] for b in by_employer) == 40
    by_salary = {b["label"]: b["sent"] for b in stats["breakdowns"]["by_salary"]}
    # 120k + 5k·i: i 0..5 → <150k (6), 6..15 → 150–200k (10), 16..35 → 200–300k (20), 36..39 → 300k+ (4)
    assert by_salary == {"100–150 тыс. ₽": 6, "150–200 тыс. ₽": 10, "200–300 тыс. ₽": 20, "от 300 000 ₽": 4}
    wh = stats["breakdowns"]["by_weekday_hour"]
    assert sum(c["sent"] for c in wh["cells"]) == 40 and sum(h["sent"] for h in wh["by_hour"]) == 40
    assert {h["hour"] for h in wh["by_hour"] if h["sent"]} == {9, 15, 21, 3}


async def test_period_filters_and_cache(runtime: Runtime) -> None:
    await _seed(runtime)
    async with runtime.session_factory() as session:
        custom = await compute_stats(
            session,
            runtime.settings,
            period_key="custom",
            date_from="2026-08-01",
            date_to="2026-08-01",
            now=NOW,
            use_cache=False,
        )
        assert custom["metrics"]["sent"] == 3 and custom["period"]["date_from"] == "2026-08-01"
        today = await compute_stats(session, runtime.settings, period_key="today", now=D0 + timedelta(hours=1))
        assert today["metrics"]["sent"] == 3  # the whole local day, including sends later that day
        assert today["metrics"]["remaining_day"] == runtime.settings.apply_max_per_day - 3
        # cached copy is returned while the TTL lasts
        again = await compute_stats(session, runtime.settings, period_key="today", now=D0 + timedelta(hours=1))
        assert again is today
        seven = await compute_stats(
            session, runtime.settings, period_key="7d", now=D0 + timedelta(days=9), use_cache=False
        )
        assert seven["period"]["date_from"] == "2026-08-04" and seven["period"]["date_to"] == "2026-08-10"
        assert seven["metrics"]["sent"] == 28  # 4 per day × 7 days (Aug 4–10)


async def test_csv_exports(runtime: Runtime) -> None:
    await _seed(runtime)
    async with runtime.session_factory() as session:
        apps = await export_applications_csv(session, runtime.settings, period_key="all", date_from=None, date_to=None)
        agg = await export_aggregates_csv(session, runtime.settings, period_key="all", date_from=None, date_to=None)
    lines = apps.lstrip("﻿").splitlines()
    assert lines[0].startswith("sent_at;vacancy;employer;area;salary_net_rur;work_format;mode;status;negotiation_state")
    assert len(lines) == 41 and apps.startswith("﻿")
    row = lines[1].split(";")
    assert (
        row[1] == "Vacancy 0" and row[6] == "external" and row[7] == "invited" and row[13] == "50" and row[15] == "100"
    )
    assert "metric;value" in agg and "sent;40" in agg and "invitations;10" in agg and "score_bucket;sent" in agg
