"""Hard selection rules. Pure functions: no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.hh.vacancy import EXPERIENCE_RANGES, VacancyData, to_rur
from app.services.preferences import Preferences
from app.services.textmatch import contains_term, norm

STATUS_FILTERED = "filtered_out"
STATUS_HIDDEN = "hidden"
STATUS_ALREADY = "already_applied"

AGENCY_MARKERS = ("кадров", "агентств", "рекрут", "recruit", "staffing", "подбор персонала", "hr-агент")


@dataclass
class RulesContext:
    prefs: Preferences
    hidden_employer_ids: set[str] = field(default_factory=set)
    hidden_employer_names: set[str] = field(default_factory=set)  # normalized
    direct_policy: str = "keep"  # skip | keep
    currency_rates: dict[str, float] = field(default_factory=dict)


@dataclass
class RuleResult:
    status: str | None  # None = passed
    reasons: list[str] = field(default_factory=list)
    # informational flags: letter_required, test, test_required, direct, no_salary
    flags: list[str] = field(default_factory=list)
    needs_manual: bool = False

    @property
    def passed(self) -> bool:
        return self.status is None


def looks_like_agency(employer_name: str | None) -> bool:
    n = norm(employer_name)
    return any(marker in n for marker in AGENCY_MARKERS)


def _fail(status: str, reason: str, flags: list[str]) -> RuleResult:
    return RuleResult(status=status, reasons=[reason], flags=flags)


def apply_rules(v: VacancyData, ctx: RulesContext) -> RuleResult:
    """Returns the first failing rule (status + reason). Description-based rules run only when the text is known."""
    prefs = ctx.prefs
    flags: list[str] = []
    if v.response_letter_required:
        flags.append("letter_required")
    if v.has_test:
        flags.append("test")
    if v.test_required:
        flags.append("test_required")
    if v.is_direct:
        flags.append("direct")
    if not (v.salary_from or v.salary_to):
        flags.append("no_salary")

    # 1. archived / relations / employer blacklists
    if v.archived:
        return _fail(STATUS_FILTERED, "вакансия в архиве", flags)
    if any(r in v.relations for r in ("got_response", "got_invitation", "got_rejection")):
        return _fail(STATUS_ALREADY, "по вакансии уже есть отклик/приглашение", flags)
    if "blacklisted" in v.relations or v.employer_blacklisted:
        return _fail(STATUS_HIDDEN, "скрыта на hh.ru", flags)
    if v.employer_id and v.employer_id in ctx.hidden_employer_ids:
        return _fail(STATUS_HIDDEN, "работодатель скрыт в панели", flags)
    if v.employer_name and norm(v.employer_name) in ctx.hidden_employer_names:
        return _fail(STATUS_HIDDEN, "работодатель скрыт в панели", flags)
    employer_norm = norm(v.employer_name)
    for name in prefs.excluded_employers:
        if norm(name) and norm(name) in employer_norm:
            return _fail(STATUS_FILTERED, f"исключённый работодатель: {name}", flags)
    if prefs.accredited_it_only and v.employer_accredited_it is False:
        return _fail(STATUS_FILTERED, "работодатель не аккредитованная IT-компания", flags)
    if prefs.no_agencies and looks_like_agency(v.employer_name):
        return _fail(STATUS_FILTERED, "похоже на кадровое агентство", flags)

    # 2. stop words
    name_norm = norm(v.name)
    for word in prefs.stop_words_title:
        if contains_term(name_norm, word):
            return _fail(STATUS_FILTERED, f"стоп-слово в названии: {word}", flags)
    if v.description_text:
        desc_norm = norm(v.description_text)
        for word in prefs.stop_words_description:
            if contains_term(desc_norm, word):
                return _fail(STATUS_FILTERED, f"стоп-слово в описании: {word}", flags)

    # 3. region / format / employment
    if prefs.areas and v.area_name and not v.is_remote:
        wanted = {norm(a) for a in prefs.areas}
        if norm(v.area_name) not in wanted:
            return _fail(STATUS_FILTERED, f"регион {v.area_name} не в списке", flags)
    if prefs.work_formats and v.work_format_ids and not set(prefs.work_formats) & set(v.work_format_ids):
        shown = ", ".join(v.work_format_names) or ", ".join(v.work_format_ids)
        return _fail(STATUS_FILTERED, f"формат работы: {shown}", flags)
    if prefs.employment_forms and v.employment_form_id and v.employment_form_id not in prefs.employment_forms:
        return _fail(STATUS_FILTERED, f"занятость: {v.employment_form_name or v.employment_form_id}", flags)

    # 4. experience range
    rng = EXPERIENCE_RANGES.get(v.experience_id or "")
    if rng and (prefs.experience_min_years is not None or prefs.experience_max_years is not None):
        lo, hi = rng
        want_lo = prefs.experience_min_years if prefs.experience_min_years is not None else 0.0
        want_hi = prefs.experience_max_years if prefs.experience_max_years is not None else 99.0
        # required range [lo, hi) must fall inside the wanted range: "до 3 лет" excludes "от 3 до 6"
        if hi <= want_lo or lo >= want_hi:
            return _fail(STATUS_FILTERED, f"требуемый опыт: {v.experience_name or v.experience_id}", flags)

    # 5. salary
    if prefs.salary_min:
        net = v.salary_net_estimate()
        if net is None:
            if prefs.salary_missing == "skip":
                return _fail(STATUS_FILTERED, "зарплата не указана", flags)
        else:
            net_rur = to_rur(net, v.salary_currency, ctx.currency_rates)
            min_rur = to_rur(prefs.salary_min, prefs.salary_currency, ctx.currency_rates)
            if net_rur is not None and min_rur is not None and net_rur < min_rur:
                return _fail(STATUS_FILTERED, f"зарплата ниже минимума ({net_rur:,} ₽ нетто)".replace(",", " "), flags)

    # 6. direct / tests
    needs_manual = False
    if v.is_direct:
        if ctx.direct_policy == "skip":
            return _fail(STATUS_FILTERED, "отклик на внешнем сайте (direct)", flags)
        needs_manual = True
    if v.test_required:
        needs_manual = True

    return RuleResult(status=None, reasons=[], flags=flags, needs_manual=needs_manual)
