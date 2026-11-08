"""hirehi.ru pages → the panel's data (app/hirehi/parse.py). The pages are synthetic but keep the markup of the live
site (checked 2026-11-08): the jobs JSON of a listing, the card links and the JSON-LD ItemList, the JobPosting, the
breadcrumbs, the page's vacancy data and the apply button of a card."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from app.hirehi import parse
from app.hirehi.parse import (
    CATEGORIES,
    CITIES,
    FORMATS,
    LEVELS,
    SUBCATEGORIES,
    Card,
    HireHiParams,
    LayoutChanged,
    ListingItem,
    card_path,
    hh_employer_id,
    item_reason,
    parse_card,
    parse_format,
    parse_listing,
    parse_salary,
    title_similarity,
    vacancy_data,
)

NOW = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)


def job(
    jid: int,
    title: str,
    *,
    company: str = "Рога и копыта",
    icon: str | None = "/static/uploads/company_hh777_20261024101304_38e881.webp",
    created: str = "2026-11-08T07:26:58Z",
    fmt: str = "удалённо по РФ",
    level: str = "middle",
    category: str = "development",
    salary: str = "зп не указана",
) -> dict[str, Any]:
    return {
        "category": category,
        "company": company,
        "company_icon": icon,
        "created_at": created,
        "format": fmt,
        "id": jid,
        "industry": "other",
        "is_from_recruiter": False,
        "is_premium": False,
        "level": level,
        "salary": salary,
        "salary_display": salary,
        "title": title,
    }


def slug(title: str) -> str:
    return "-".join("".join(c if c.isalnum() and c.isascii() else " " for c in title.lower()).split()) or "vacancy"


def listing_html(
    jobs: list[dict[str, Any]], *, total: int | None = None, has_more: bool = False, links: bool = True
) -> str:
    ssr = {
        "category": "all",
        "exclude_hidden_applied": False,
        "filter_counts": {"hh": 1, "hirehi": 1},
        "has_more": has_more,
        "jobs": jobs,
        "limit": 51,
        "page": 1,
        "total_count": len(jobs) if total is None else total,
    }
    cards = ""
    if links:
        cards = "".join(
            f'<div class="job-card-shell" data-id="{j["id"]}">\n<a class="job-card"\n'
            f'   href="/{j["category"]}/{slug(j["title"])}-{j["id"]}"\n'
            f'   data-id="{j["id"]}" data-category="{j["category"]}">\n'
            f'<div class="job-title">{j["title"].lower()}</div></a></div>'
            for j in jobs
        )
    item_list = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": n,
                "url": f"https://hirehi.ru/{j['category']}/{slug(j['title'])}-{j['id']}",
                "name": j["title"],
            }
            for n, j in enumerate(jobs, 1)
        ],
    }
    graph = {"@context": "https://schema.org", "@graph": [{"@type": "Organization", "name": "HireHi"}]}
    return (
        "<!doctype html><html><head>"
        f'<script type="application/ld+json">{json.dumps(graph)}</script>'
        f'<script type="application/ld+json">{json.dumps(item_list, ensure_ascii=False)}</script>'
        f"</head><body>{cards}"
        f'<script nonce="n" type="application/json" id="__SSR_JOBS__">\n{json.dumps(ssr, ensure_ascii=False)}\n</script>'
        "</body></html>"
    )


DESCRIPTION = (
    "Описание: Компания делает сервисы для логистики.\n\n"
    "Задачи: Разработка сервисов на Python и FastAPI. Интеграции с Kafka.\n\n"
    "Требования: Опыт с Python от 2 лет. Знание PostgreSQL. Будет плюсом: Docker\n\n"
    "Условия: Удалённая работа по России"
)


def card_html(
    jid: int,
    title: str,
    *,
    company: str = "Рога и копыта",
    label: str = "Отклик",
    description: str = DESCRIPTION,
    posted: str = "2026-11-08T06:39:39.838407",
    valid: str = "2027-02-06T06:39:39.838407",
    months: int | None = 24,
    locality: str = "Москва",
    fmt: str = "удалённо по РФ",
    category: str = "development",
    crumbs: tuple[str, ...] = ("Разработка", "Backend"),
    salary: str = "зп не указана",
    employment: str = "FULL_TIME",
) -> str:
    posting: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "description": description,
        "datePosted": posted,
        "validThrough": valid,
        "employmentType": employment,
        "workHours": "standard",
        "hiringOrganization": {"@type": "Organization", "name": company},
        "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress", "addressLocality": locality}},
        "qualifications": "middle level position",
        "directApply": True,
        "url": f"https://hirehi.ru/{category}/{slug(title)}-{jid}",
        "identifier": {"@type": "PropertyValue", "name": "Job ID", "value": str(jid)},
    }
    if months is not None:
        posting["experienceRequirements"] = {
            "@type": "OccupationalExperienceRequirements",
            "monthsOfExperience": months,
        }
    names = ["Все вакансии", *crumbs, title]
    breadcrumbs = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [{"@type": "ListItem", "position": n, "name": name} for n, name in enumerate(names, 1)],
    }
    vdata = {
        "category": category,
        "company": company,
        "format": fmt,
        "id": jid,
        "level": "middle",
        "location": locality,
        "salary": salary,
        "title": title,
        "contact_opened": False,
        "is_authenticated": False,
    }
    return (
        "<!doctype html><html><head>"
        f'<script type="application/ld+json">{json.dumps(posting, ensure_ascii=False)}</script>'
        f'<script type="application/ld+json">{json.dumps(breadcrumbs, ensure_ascii=False)}</script>'
        "</head><body>"
        '<a href="#" class="btn btn-gray vacancy-tracker-btn" id="addApplicationBtnDesktop">трекер</a>\n'
        '<a href="#"\n   class="btn btn-primary sidebar-apply-btn sidebar-apply-btn--desktop" data-apply-link="true">\n'
        f"        {label}\n    </a>\n"
        f'<script nonce="n" type="application/json" id="vacancy-data-json">{json.dumps(vdata, ensure_ascii=False)}'
        "</script></body></html>"
    )


# ---- the listing -------------------------------------------------------------------------------------------------


def test_listing_gives_the_jobs_newest_first_with_their_card_links() -> None:
    page = listing_html(
        [
            job(1002, "Backend engineer", salary="от 300 000 до 400 000 ₽"),
            job(1001, "Python developer", icon="data:image/svg+xml;base64,AAAA"),
        ],
        total=195,
        has_more=True,
    )
    listing = parse_listing(page)
    assert listing.total == 195 and listing.has_more
    first, second = listing.items
    assert (first.id, first.title, first.path) == ("1002", "Backend engineer", "/development/backend-engineer-1002")
    assert first.company_icon and first.salary == "от 300 000 до 400 000 ₽"
    assert second.company_icon is None  # a placeholder picture is not kept
    assert second.published_at == datetime(2026, 11, 8, 7, 26, 58, tzinfo=UTC)
    # the item stored in raw_short_json comes back the same
    assert ListingItem.from_raw(json.loads(json.dumps(first.raw()))) == first


def test_listing_links_come_from_the_item_list_when_the_cards_have_none() -> None:
    listing = parse_listing(listing_html([job(1003, "Data engineer")], links=False))
    assert listing.items[0].path == "/development/data-engineer-1003"


def test_a_page_without_the_jobs_is_a_changed_layout() -> None:
    with pytest.raises(LayoutChanged):
        parse_listing("<html><body>Проверка браузера</body></html>")
    with pytest.raises(LayoutChanged):
        parse_listing('<script id="__SSR_JOBS__" type="application/json">{"jobs": "нет"}</script>')


def test_card_paths_are_only_cards_of_the_site() -> None:
    assert card_path("https://hirehi.ru/devops/devops-engineer-218472") == "/devops/devops-engineer-218472"
    assert card_path("/qa/qa-auto-218466/") == "/qa/qa-auto-218466"
    assert card_path("https://example.com/devops/devops-engineer-218472") is None
    assert card_path("/vacancies/backend") is None and card_path("/vacancies/1c") is None and card_path(None) is None


def test_the_hh_employer_number_comes_from_the_logo() -> None:
    assert hh_employer_id("/static/uploads/company_hh5674346_20261024101304_38e881.webp") == "5674346"
    assert hh_employer_id("/static/uploads/Alstom_hh1129364_20261108054305_11d052.webp") == "1129364"
    assert hh_employer_id("/static/uploads/PAYNET_url_20261016110557_4b49ec.webp") is None
    assert hh_employer_id("/static/uploads/_20250425083218.webp") is None and hh_employer_id(None) is None


# ---- the card ----------------------------------------------------------------------------------------------------


def test_card_gives_the_posting_the_button_and_the_breadcrumbs() -> None:
    card = parse_card(card_html(1001, "Backend engineer", label="Отклик на hh"))
    assert card.posting["title"] == "Backend engineer" and "@context" not in card.posting
    assert card.apply_label == "Отклик на hh" and card.from_hh
    assert card.breadcrumbs == ["Разработка", "Backend"]
    assert card.data["format"] == "удалённо по РФ" and card.path == "/development/backend-engineer-1001"
    assert not parse_card(card_html(1002, "Data engineer", label="Отклик")).from_hh
    assert not Card({}, apply_label="Отклик в Telegram").from_hh
    assert Card.from_raw(json.loads(json.dumps(card.raw()))) == card
    with pytest.raises(LayoutChanged):
        parse_card("<html><body>нет данных</body></html>")


def test_vacancy_data_from_the_listing_and_the_card() -> None:
    item = parse_listing(listing_html([job(1001, "Backend engineer", fmt="гибрид Казань")])).items[0]
    short = vacancy_data(item.raw(), None, now=NOW)
    assert short.name == "Backend engineer" and short.employer_name == "Рога и копыта"
    assert short.employer_id == "777"  # the hh.ru number of the logo
    assert (short.work_format_ids, short.area_name) == (["HYBRID"], "Казань")
    assert short.hh_id == "" and not short.is_full and short.description_text is None
    assert short.alternate_url == short.apply_alternate_url == "https://hirehi.ru/development/backend-engineer-1001"

    card = parse_card(card_html(1001, "Backend engineer", fmt="гибрид Казань", salary="от 200 000 до 300 000 ₽"))
    full = vacancy_data(item.raw(), card.raw(), now=NOW)
    assert full.is_full and full.description_text == DESCRIPTION
    assert full.description_html and "<strong>Задачи:</strong>" in full.description_html
    assert (full.experience_id, full.experience_name) == ("between1And3", "От 1 года до 3 лет")
    assert (full.employment_form_id, full.salary_from, full.salary_to, full.salary_currency) == (
        "FULL",
        200_000,
        300_000,
        "RUR",
    )
    assert full.professional_roles == ["Backend"] and not full.archived
    assert full.key_skills == []  # «skills» of HireHi is a cut requirement text, not a list of skills


def test_a_card_past_its_term_or_removed_is_archived() -> None:
    old = parse_card(card_html(1001, "Backend engineer", valid="2026-11-01T00:00:00")).raw()
    assert vacancy_data(None, old, now=NOW).archived
    live = parse_card(card_html(1001, "Backend engineer")).raw()
    assert not vacancy_data(None, live, now=NOW).archived
    assert vacancy_data(None, {**live, "gone_at": "2026-11-08T08:00:00+00:00"}, now=NOW).archived


def test_formats_salaries_and_experience() -> None:
    assert parse_format("удалённо по РФ") == ("REMOTE", None) and parse_format("удалённо") == ("REMOTE", None)
    assert parse_format("гибрид Москва") == ("HYBRID", "Москва") and parse_format("офис Минск") == ("ON_SITE", "Минск")
    assert parse_format("офис") == ("ON_SITE", None) and parse_format(None) == (None, None)
    assert parse_salary("от 300 000 до 1 000 000 ₽") == (300_000, 1_000_000, "RUR")
    assert parse_salary("до 646 000 ₽") == (None, 646_000, "RUR")
    assert parse_salary("от 82 000 ₽") == (82_000, None, "RUR")
    assert parse_salary("~ 441 756 ₽") == (441_756, 441_756, "RUR")
    assert parse_salary("287 000 ₽") == (287_000, 287_000, "RUR")
    assert parse_salary("от 3 000 $") == (3_000, None, "USD")
    assert parse_salary("зп не указана") == (None, None, None) and parse_salary(None) == (None, None, None)
    assert [parse.experience_of(m) for m in (0, 11, 12, 35, 36, 71, 72, None, True)] == [
        "noExperience",
        "noExperience",
        "between1And3",
        "between1And3",
        "between3And6",
        "between3And6",
        "moreThan6",
        None,
        None,
    ]


# ---- searches ------------------------------------------------------------------------------------------------------


def test_search_params_build_only_allowed_addresses() -> None:
    p = HireHiParams(section="backend", formats=["remote_russia"], levels=["middle", "senior"])
    assert p.path() == "/vacancies/backend?format=remote_russia&level=middle&level=senior"
    assert p.path(2).endswith("&page=2")
    assert "search=" not in p.path(3) and "sort=" not in p.path(3)
    city = HireHiParams(city="kazan", formats=["гибрид", "удалённо"])
    assert (
        city.path()
        == "/kazan-jobs?format=%D0%B3%D0%B8%D0%B1%D1%80%D0%B8%D0%B4&format=%D1%83%D0%B4%D0%B0%D0%BB%D1%91%D0%BD%D0%BD%D0%BE"
    )
    assert HireHiParams().path() == "/" and HireHiParams(section="management").path() == "/vacancies/management"
    assert "Backend" in p.summary() and HireHiParams().summary() == "все вакансии"
    assert p.summary() == "Backend · удалённо по РФ · Middle/Senior"  # «РФ» keeps its capitals
    # a city hub knows categories, not subcategories: the API answers it with a phrase (422)
    assert HireHiParams(city="kazan", section="backend").problem()
    assert HireHiParams(city="kazan", section="development").problem() is None
    with pytest.raises(ValidationError):
        HireHiParams(section="python developer")
    with pytest.raises(ValidationError):
        HireHiParams(formats=["remote"])
    with pytest.raises(ValidationError):
        HireHiParams(keywords=["x" * 61])
    assert HireHiParams(keywords=[" SQL ", "SQL", ""], levels=["head", "head"]).model_dump()["keywords"] == ["SQL"]


def test_the_literal_types_name_every_value_of_the_catalogue() -> None:
    assert set(get_args(parse.SectionSlug)) == {"", *CATEGORIES, *SUBCATEGORIES}
    assert set(get_args(parse.CitySlug)) == {"", *CITIES}
    assert set(get_args(parse.FormatValue)) == set(FORMATS) and set(get_args(parse.LevelValue)) == set(LEVELS)


def test_the_panel_side_of_the_filters() -> None:
    item = ListingItem.from_raw(job(1, "Python developer (Kafka)", fmt="гибрид Казань", category="development"))
    assert item_reason(item, HireHiParams()) is None
    assert item_reason(item, HireHiParams(keywords=["Kafka", "Docker"])) is None
    assert item_reason(item, HireHiParams(keywords=["Go"])) == "в названии нет слов поиска"
    assert item_reason(item, HireHiParams(formats=["remote_russia"])) == "формат гибрид Казань"
    assert item_reason(item, HireHiParams(formats=["гибрид"])) is None
    assert item_reason(item, HireHiParams(levels=["senior"])) == "уровень middle"
    assert item_reason(item, HireHiParams(city="kazan", section="management")) == "раздел development"
    remote_world = ListingItem.from_raw(job(2, "Backend engineer", fmt="удалённо"))
    assert item_reason(remote_world, HireHiParams(formats=["remote_russia"])) is not None
    assert item_reason(remote_world, HireHiParams(formats=["удалённо"])) is None


def test_titles_of_the_two_sites_compare_by_their_words() -> None:
    assert title_similarity("DevOps engineer for high-load applications", "DevOps-инженер") == 1.0
    assert title_similarity("бизнес-аналитик в логистике", "Бизнес-аналитик") == 1.0
    assert title_similarity("Data engineer in payment systems", "Data Engineer") == 1.0
    assert title_similarity("Senior Python developer", "Python-разработчик") == 1.0
    assert title_similarity("Коммерческий директор маркетплейсов", "Бухгалтер") == 0.0
    assert title_similarity("anything", "") == 0.0


def test_listing_dates_far_in_the_past_parse_too() -> None:
    old = (NOW - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert ListingItem.from_raw(job(1, "x", created=old)).published_at == NOW - timedelta(days=30)
