from __future__ import annotations

from datetime import date

from app.hh.vacancy import normalize_vacancy
from app.services.preferences import Preferences, SkillWeight
from app.services.prescore import prescore
from app.services.rules import RulesContext, apply_rules, looks_like_agency
from app.services.textmatch import contains_term, norm, terms_equal
from tests.unit.test_vacancy_norm import load_full, short_item

RATES = {"RUR": 1.0, "USD": 0.0125, "EUR": 0.0111}


def ctx(**prefs) -> RulesContext:
    return RulesContext(prefs=Preferences(**prefs), currency_rates=RATES, direct_policy="keep")


def full():
    return normalize_vacancy(load_full(), full=True)


# ---- text matching ----------------------------------------------------------


def test_contains_term_word_forms() -> None:
    text = norm("Ищем стажёра-разработчика на Python и C++, знание PostgreSQL обязательно")
    assert contains_term(text, "стажер")  # ё/е + suffix
    assert contains_term(text, "Python")
    assert contains_term(text, "c++")
    assert contains_term(text, "postgres")  # latin terms of 5+ chars allow a short suffix (postgresql)
    assert not contains_term(norm("javascript"), "java")
    assert not contains_term(text, "java")
    assert not contains_term(text, "go")  # not present as a word
    assert contains_term(norm("Go developer"), "go")
    assert not contains_term(norm("golang developer"), "go")
    assert (
        terms_equal("PostgreSQL", "postgres")
        and terms_equal("Python 3", "python")
        and not terms_equal("Java", "JavaScript")
    )


def test_agency_heuristic() -> None:
    assert looks_like_agency("Кадровое агентство Х")
    assert looks_like_agency("Smart Recruiting")
    assert not looks_like_agency("Рога и копыта")


# ---- rules ------------------------------------------------------------------


def test_rules_pass_and_flags() -> None:
    r = apply_rules(full(), ctx())
    assert r.passed and "letter_required" in r.flags and not r.needs_manual


def test_rules_archived_relations_hidden() -> None:
    assert apply_rules(normalize_vacancy(short_item(archived=True), full=False), ctx()).status == "filtered_out"
    r = apply_rules(normalize_vacancy(short_item(relations=["got_response"]), full=False), ctx())
    assert r.status == "already_applied"
    r = apply_rules(normalize_vacancy(short_item(relations=["blacklisted"]), full=False), ctx())
    assert r.status == "hidden"
    c = ctx()
    c.hidden_employer_ids = {"1455"}
    assert apply_rules(full(), c).status == "hidden"
    c = ctx()
    c.hidden_employer_names = {norm("Рога и копыта")}
    assert apply_rules(full(), c).status == "hidden"


def test_rules_employer_preferences() -> None:
    assert apply_rules(full(), ctx(excluded_employers=["рога"])).status == "filtered_out"
    v = normalize_vacancy(
        short_item(employer={"id": "9", "name": "ООО Ромашка", "accredited_it_employer": False}), full=False
    )
    assert apply_rules(v, ctx(accredited_it_only=True)).status == "filtered_out"
    assert apply_rules(full(), ctx(accredited_it_only=True)).passed
    v = normalize_vacancy(short_item(employer={"id": "9", "name": "Кадровое агентство Плюс"}), full=False)
    assert apply_rules(v, ctx(no_agencies=True)).status == "filtered_out"


def test_rules_stop_words() -> None:
    r = apply_rules(full(), ctx(stop_words_title=["senior"]))
    assert r.status == "filtered_out" and "стоп-слово в названии" in r.reasons[0]
    r = apply_rules(full(), ctx(stop_words_description=["платеж"]))
    assert r.status == "filtered_out" and "описании" in r.reasons[0]
    # description rule does not fire on the short representation
    s = normalize_vacancy(short_item(), full=False)
    assert apply_rules(s, ctx(stop_words_description=["платеж"])).passed


def test_rules_region_format_employment() -> None:
    assert apply_rules(full(), ctx(areas=["Казань"])).passed  # remote vacancy ignores region
    office = normalize_vacancy(short_item(work_format=[{"id": "ON_SITE", "name": "В офисе"}]), full=False)
    assert apply_rules(office, ctx(areas=["Казань"])).status == "filtered_out"
    assert apply_rules(office, ctx(areas=["москва"])).passed
    assert apply_rules(office, ctx(work_formats=["REMOTE"])).status == "filtered_out"
    assert apply_rules(full(), ctx(work_formats=["REMOTE", "HYBRID"])).passed
    assert apply_rules(full(), ctx(employment_forms=["PART"])).status == "filtered_out"
    no_format = normalize_vacancy(short_item(work_format=[]), full=False)
    assert apply_rules(no_format, ctx(work_formats=["REMOTE"])).passed  # unknown format is not a reason


def test_rules_experience_and_salary() -> None:
    assert apply_rules(full(), ctx(experience_min_years=1, experience_max_years=3)).status == "filtered_out"
    assert apply_rules(full(), ctx(experience_min_years=3)).passed
    assert apply_rules(full(), ctx(experience_max_years=2.5)).status == "filtered_out"
    assert apply_rules(full(), ctx(salary_min=350000)).status == "filtered_out"  # net 348000 < 350000
    assert apply_rules(full(), ctx(salary_min=300000)).passed
    usd = normalize_vacancy(
        short_item(salary={"from": 3000, "to": None, "currency": "USD", "gross": False}), full=False
    )
    assert apply_rules(usd, ctx(salary_min=200000)).passed  # 3000 USD = 240 000 RUR
    assert apply_rules(usd, ctx(salary_min=300000)).status == "filtered_out"
    none = normalize_vacancy(short_item(salary=None), full=False)
    assert apply_rules(none, ctx(salary_min=100000)).passed
    assert apply_rules(none, ctx(salary_min=100000, salary_missing="skip")).status == "filtered_out"


def test_rules_direct_and_tests() -> None:
    direct = normalize_vacancy(short_item(type={"id": "direct"}, response_url="https://x"), full=False)
    c = ctx()
    c.direct_policy = "skip"
    assert apply_rules(direct, c).status == "filtered_out"
    c.direct_policy = "keep"
    r = apply_rules(direct, c)
    assert r.passed and r.needs_manual and "direct" in r.flags
    raw = load_full()
    raw["has_test"] = True
    raw["test"] = {"required": True}
    r = apply_rules(normalize_vacancy(raw, full=True), ctx())
    assert r.passed and r.needs_manual and "test_required" in r.flags


# ---- prescore ---------------------------------------------------------------


def weights() -> list[SkillWeight]:
    return [
        SkillWeight(name="Python", weight=5),
        SkillWeight(name="FastAPI", weight=4),
        SkillWeight(name="PostgreSQL", weight=4),
        SkillWeight(name="Django", weight=2),
    ]


def test_prescore_good_match() -> None:
    prefs = Preferences(
        target_titles=["Python developer"], work_formats=["REMOTE"], areas=["Москва"], salary_min=250000
    )
    r = prescore(full(), prefs, weights=weights(), candidate_months=60, currency_rates=RATES, today=date(2026, 9, 11))
    # skills: coverage 3/4 (Kafka missing) -> 0.6*0.75 + 0.4*(13/15) = 0.45+0.3467 = 0.7967*50 = 39.8
    # experience 15, title 15 (exact substring "python developer"), conditions 4+3+3 = 10, no penalties
    assert r.score == 80, r.notes
    assert r.covered_key_skills == ["Python", "FastAPI", "PostgreSQL"] and r.uncovered_key_skills == ["Kafka"]
    assert r.notes[0].startswith("итого 80:")


def test_prescore_penalties_and_partial() -> None:
    raw = load_full()
    raw["counters"] = {"responses": 350}
    raw["employer"]["name"] = "Кадровое агентство Плюс"
    raw["published_at"] = "2026-08-01T10:00:00+0300"
    raw["name"] = "Разработчик"
    v = normalize_vacancy(raw, full=True)
    prefs = Preferences(target_titles=["Python developer"])
    r = prescore(v, prefs, weights=weights(), candidate_months=30, currency_rates=RATES, today=date(2026, 9, 11))
    # skills 39.8, experience 6 (2.5y vs 3-6: within 1 year below), title 0 (no significant word "python"/"developer" in "Разработчик")
    # conditions 3+2+2 = 7, penalties 10+5+5 = 20 -> 32.8 -> 33
    assert r.score == 33, r.notes
    assert any("очень много откликов" in n for n in r.notes)
    assert any("кадровое агентство" in n for n in r.notes)
    assert any("опубликована" in n for n in r.notes)


def test_prescore_no_skills_neutral() -> None:
    v = normalize_vacancy(short_item(), full=False)  # no key_skills, no description
    r = prescore(v, Preferences(), weights=[], candidate_months=None, currency_rates=RATES, today=date(2026, 9, 11))
    # skills 25 neutral, experience 8, title 8, conditions 3+2+2 = 7 -> 48
    assert r.score == 48
    assert "навыки профиля не заданы" in r.notes[1]
    # an empty profile stays neutral even when the vacancy lists key skills
    r = prescore(
        full(), Preferences(), weights=[], candidate_months=None, currency_rates=RATES, today=date(2026, 9, 11)
    )
    assert r.score == 48
