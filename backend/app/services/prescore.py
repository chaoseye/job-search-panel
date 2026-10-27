"""Deterministic pre-score 0–100 used to rank vacancies before the Claude batch."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.db.base import utcnow
from app.hh.vacancy import EXPERIENCE_RANGES, VacancyData, to_rur
from app.services.preferences import Preferences, SkillWeight
from app.services.rules import looks_like_agency
from app.services.textmatch import contains_term, norm, significant_words, terms_equal


@dataclass
class PrescoreResult:
    score: int
    notes: list[str]
    matched_skills: list[str]
    covered_key_skills: list[str]
    uncovered_key_skills: list[str]


SkillsPart = tuple[float, list[str], list[str], list[str], list[str]]


def _skills_part(v: VacancyData, weights: list[SkillWeight]) -> SkillsPart:
    notes: list[str] = []
    text_norm = norm(v.searchable_text())
    matched = [w for w in weights if contains_term(text_norm, w.name)]
    total_w = sum(w.weight for w in weights)
    matched_w = sum(w.weight for w in matched)
    profile_hit = (matched_w / total_w) if total_w else None

    covered = [k for k in v.key_skills if any(terms_equal(k, w.name) for w in weights)]
    uncovered = [k for k in v.key_skills if k not in covered]
    coverage = (len(covered) / len(v.key_skills)) if v.key_skills else None

    if not weights:
        # nothing to compare against: neutral, regardless of what the vacancy lists
        score = 25.0
        notes.append("навыки профиля не заданы — нейтральная оценка")
    elif coverage is None:
        score = 50.0 * profile_hit  # type: ignore[operator]
    elif profile_hit is None:
        score = 50.0 * coverage
    else:
        score = 50.0 * (0.6 * coverage + 0.4 * profile_hit)

    if weights:
        top = ", ".join(w.name for w in sorted(matched, key=lambda x: -x.weight)[:6])
        notes.append(f"навыки профиля в тексте: {len(matched)}/{len(weights)}" + (f" ({top})" if top else ""))
    if v.key_skills:
        note = f"ключевые навыки вакансии покрыты: {len(covered)}/{len(v.key_skills)}"
        if uncovered:
            note += "; нет в профиле: " + ", ".join(uncovered[:6])
        notes.append(note)
    return score, notes, [w.name for w in matched], covered, uncovered


def _experience_part(v: VacancyData, candidate_months: int | None) -> tuple[float, str]:
    rng = EXPERIENCE_RANGES.get(v.experience_id or "")
    if rng is None or candidate_months is None:
        return 8.0, "опыт не сопоставлен"
    years = candidate_months / 12
    lo, hi = rng
    if lo <= years < hi:
        return 15.0, f"опыт подходит ({v.experience_name or v.experience_id})"
    if years >= hi:
        return 12.0, f"опыт выше требуемого ({v.experience_name or v.experience_id})"
    if years >= lo - 1:
        return 6.0, f"опыт чуть ниже требуемого ({v.experience_name or v.experience_id})"
    return 0.0, f"опыт ниже требуемого ({v.experience_name or v.experience_id})"


def _title_part(v: VacancyData, prefs: Preferences) -> tuple[float, str]:
    if not prefs.target_titles:
        return 8.0, "целевые должности не заданы"
    name_norm = norm(v.name)
    best = 0.0
    hit = ""
    for title in prefs.target_titles:
        t = norm(title)
        if t and t in name_norm:
            return 15.0, f"название совпадает с «{title}»"
        words = significant_words(title)
        if words and any(contains_term(name_norm, w) for w in words):
            if best < 8.0:
                best, hit = 8.0, title
    if best:
        return best, f"название частично совпадает с «{hit}»"
    return 0.0, "название не совпадает с целевыми должностями"


def _conditions_part(v: VacancyData, prefs: Preferences, rates: dict[str, float]) -> tuple[float, list[str]]:
    notes: list[str] = []
    score = 0.0
    if prefs.work_formats:
        if set(prefs.work_formats) & set(v.work_format_ids):
            score += 4
        elif not v.work_format_ids:
            score += 2
            notes.append("формат работы не указан")
        else:
            notes.append("формат работы не совпадает")
    else:
        score += 3
    if prefs.areas:
        if v.is_remote or (v.area_name and norm(v.area_name) in {norm(a) for a in prefs.areas}):
            score += 3
        else:
            notes.append("регион не совпадает")
    else:
        score += 2
    if prefs.salary_min:
        net_rur = to_rur(v.salary_net_estimate(), v.salary_currency, rates)
        min_rur = to_rur(prefs.salary_min, prefs.salary_currency, rates)
        if net_rur is None or min_rur is None:
            score += 1
            notes.append("зарплата не указана")
        elif net_rur >= min_rur:
            score += 3
        else:
            notes.append("зарплата ниже ожидаемой")
    else:
        score += 2
    return score, notes


def _penalties(v: VacancyData, today: date) -> tuple[float, list[str]]:
    penalty = 0.0
    notes: list[str] = []
    if v.responses_count is not None:
        if v.responses_count > 300:
            penalty += 10
            notes.append(f"очень много откликов ({v.responses_count})")
        elif v.responses_count > 100:
            penalty += 5
            notes.append(f"много откликов ({v.responses_count})")
    if looks_like_agency(v.employer_name):
        penalty += 5
        notes.append("похоже на кадровое агентство")
    if v.published_at is not None:
        age = (today - v.published_at.date()).days
        if age > 14:
            penalty += 5
            notes.append(f"опубликована {age} дн. назад")
    return penalty, notes


def prescore(
    v: VacancyData,
    prefs: Preferences,
    *,
    weights: list[SkillWeight],
    candidate_months: int | None,
    currency_rates: dict[str, float] | None = None,
    today: date | None = None,
) -> PrescoreResult:
    rates = currency_rates or {}
    today = today or utcnow().date()
    notes: list[str] = []

    skills_score, skill_notes, matched, covered, uncovered = _skills_part(v, weights)
    notes += skill_notes
    exp_score, exp_note = _experience_part(v, candidate_months)
    notes.append(exp_note)
    title_score, title_note = _title_part(v, prefs)
    notes.append(title_note)
    cond_score, cond_notes = _conditions_part(v, prefs, rates)
    notes += cond_notes
    penalty, penalty_notes = _penalties(v, today)
    notes += penalty_notes

    total = skills_score + exp_score + title_score + cond_score - penalty
    score = int(round(max(0.0, min(100.0, total))))
    notes.insert(
        0,
        f"итого {score}: навыки {skills_score:.0f}/50, опыт {exp_score:.0f}/15, название {title_score:.0f}/15, "
        f"условия {cond_score:.0f}/10, штрафы −{penalty:.0f}",
    )
    return PrescoreResult(
        score=score, notes=notes, matched_skills=matched, covered_key_skills=covered, uncovered_key_skills=uncovered
    )
