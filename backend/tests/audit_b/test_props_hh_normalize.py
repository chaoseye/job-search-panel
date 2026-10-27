"""Фазз-тесты терпимых разборщиков ответов hh.ru и схем обмена: на любом JSON-подобном входе — без исключений."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from app.exchange.schemas import Evaluation, Letter, Packet, ResultFile
from app.hh.errors import HHError, HHOAuthTokenError, build_error
from app.hh.negotiation import normalize_chat_messages, normalize_negotiation, normalize_negotiation_messages
from app.hh.resume import months_between, months_to_text, normalize_resume, plural_ru, resume_to_text
from app.hh.textclean import html_to_text, sanitize_html, strip_highlight
from app.hh.vacancy import (
    EXPERIENCE_RANGES,
    VacancyData,
    merge_full,
    normalize_vacancy,
    parse_hh_datetime,
    to_rur,
)

json_scalars = st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=30)
json_values = st.recursive(
    json_scalars,
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=12), children, max_size=6),
    max_leaves=25,
)
json_dicts = st.dictionaries(st.text(max_size=16), json_values, max_size=10)
# ключи, которые разборщики реально читают: так фазз чаще попадает в ветки с «почти правильными» типами
VACANCY_KEYS = (
    "id",
    "name",
    "employer",
    "salary",
    "salary_range",
    "snippet",
    "test",
    "counters",
    "work_format",
    "relations",
    "description",
    "key_skills",
    "address",
    "area",
    "experience",
    "employment_form",
    "employment",
    "schedule",
    "published_at",
    "archived",
    "type",
    "response_letter_required",
    "has_test",
    "response_url",
    "alternate_url",
    "apply_alternate_url",
    "professional_roles",
)
# поля-списки hh: скаляр вместо списка разбор теперь пропускает (_list), генераторы подают и его
LIST_KEYS = ("work_format", "relations", "key_skills", "professional_roles")
list_like = (
    st.none()
    | st.lists(json_values, max_size=4)
    | st.text(max_size=10)
    | st.dictionaries(st.text(max_size=5), json_values, max_size=3)
)
keyed_vacancy = st.dictionaries(st.sampled_from(VACANCY_KEYS), json_values, max_size=12)


# исправлено сессией «_a» 2026-09-23 (fix/input-validation-and-parsing): скаляр вместо списка → пустой список
@pytest.mark.parametrize("key", LIST_KEYS)
def test_normalize_vacancy_tolerates_scalars_in_list_fields(key: str) -> None:
    normalize_vacancy({"id": "1", key: 1}, full=False)


@settings(max_examples=40)
@given(json_dicts | keyed_vacancy, st.booleans())
def test_normalize_vacancy_never_raises(raw: dict, full: bool) -> None:
    v = normalize_vacancy(raw, full=full)
    assert isinstance(v, VacancyData)
    assert isinstance(v.searchable_text(), str)
    assert v.salary_net_estimate() is None or v.salary_net_estimate() >= 0 or v.salary_net_estimate() < 0
    merged = merge_full(v, normalize_vacancy(raw, full=True))
    assert merged.hh_id == v.hh_id
    json.dumps(v.model_dump(), default=str)  # сериализуется


@given(st.integers(min_value=0, max_value=10_000_000), st.sampled_from(["RUR", "RUB", "rur", "USD", "XXX", None]))
def test_to_rur_rules(amount: int, currency: str | None) -> None:
    rates = {"RUR": 1.0, "RUB": 1.0, "USD": 0.0125}
    result = to_rur(amount, currency, rates)
    if currency is None or currency.upper() in ("RUR", "RUB"):
        assert result == amount
    elif currency == "USD":
        assert result == int(round(amount / 0.0125))
    else:
        assert result is None
    assert to_rur(None, currency, rates) is None
    assert to_rur(amount, "USD", {"USD": 0}) is None


@given(st.text(max_size=40) | st.none() | st.integers())
def test_parse_hh_datetime_never_raises(value: object) -> None:
    out = parse_hh_datetime(value)
    assert out is None or out.tzinfo is not None


def test_parse_hh_datetime_formats() -> None:
    a = parse_hh_datetime("2026-09-11T10:00:00+0300")
    assert a == datetime(2026, 9, 11, 7, 0, tzinfo=UTC)
    assert parse_hh_datetime("2026-09-11T10:00:00Z") == datetime(2026, 9, 11, 10, 0, tzinfo=UTC)
    assert parse_hh_datetime("not a date") is None


def test_experience_ranges_are_contiguous() -> None:
    bounds = sorted(EXPERIENCE_RANGES.values())
    assert bounds[0][0] == 0.0
    for (_, hi), (lo, _) in zip(bounds, bounds[1:], strict=False):
        assert hi == lo


@given(json_dicts)
def test_normalize_negotiation_never_raises(raw: dict) -> None:
    data = normalize_negotiation(raw)
    assert data.unread_count >= 0 and data.messages_count >= 0
    assert data.state_id is None or data.state_id == data.state_id.lower()


@given(json_values)
def test_normalize_messages_never_raise(payload: object) -> None:
    for item in normalize_negotiation_messages(payload):
        assert item.hh_message_id and item.source == "negotiation"
    for item in normalize_chat_messages(payload):
        assert item.hh_message_id and item.source == "chat"
        assert item.author_type in ("applicant", "employer", "bot", "unknown")


@given(json_dicts)
def test_normalize_resume_never_raises(raw: dict) -> None:
    resume = normalize_resume(raw)
    assert isinstance(resume_to_text(resume), str)
    assert all(isinstance(s, str) and s for s in resume.skills)


@given(st.integers(min_value=0, max_value=100_000), st.integers(min_value=0, max_value=100_000))
def test_months_between_and_text(months: int, other: int) -> None:
    text = months_to_text(months)
    assert text and ("мес" in text or "год" in text or "лет" in text)
    start = "2000-01-01"
    assert months_between(start, None, today=datetime(2000, 1, 1, tzinfo=UTC).date()) == 0
    assert months_between(start, "2001-03-15") == 14 and other >= 0
    assert months_between("garbage", None) is None


@given(st.integers(min_value=-1000, max_value=1000))
def test_plural_ru_consistent_with_russian_rules(n: int) -> None:
    form = plural_ru(n, "год", "года", "лет")
    a = abs(n) % 100
    if 10 < a < 20:
        assert form == "лет"
    elif a % 10 == 1:
        assert form == "год"
    elif 2 <= a % 10 <= 4:
        assert form == "года"
    else:
        assert form == "лет"


@given(st.integers(min_value=100, max_value=599), json_values, st.text(max_size=20) | st.none(), st.text(max_size=50))
def test_build_error_never_raises_and_summarizes(
    status: int, payload: object, request_id: str | None, text: str
) -> None:
    err = build_error(status, payload, request_id, text)
    assert isinstance(err, HHError)
    assert isinstance(err.summary(), str) and err.summary()
    assert isinstance(err.error_types, list)
    if isinstance(err, HHOAuthTokenError):
        assert isinstance(err.error, str)


def test_build_error_maps_status_families() -> None:
    assert type(build_error(400, None)).__name__ == "HHBadRequest"
    assert type(build_error(403, None)).__name__ == "HHForbidden"
    assert type(build_error(404, None)).__name__ == "HHNotFound"
    assert type(build_error(429, None)).__name__ == "HHRateLimited"
    assert type(build_error(503, None)).__name__ == "HHServerError"
    captcha = build_error(403, {"errors": [{"type": "captcha_required", "captcha_url": "https://hh.ru/c"}]})
    assert type(captcha).__name__ == "HHCaptchaRequired"
    oauth = build_error(403, {"oauth_error": "token-expired"})
    assert type(oauth).__name__ == "HHAuthError" and oauth.value == "token_expired"  # type: ignore[attr-defined]


@given(st.text(max_size=300))
def test_html_helpers_never_raise_and_sanitize(html: str) -> None:
    clean = sanitize_html(html)
    assert "<script" not in clean.lower()
    assert "onerror=" not in clean.lower()
    text = html_to_text(html)
    assert "\r" not in text and "\n\n\n" not in text
    assert strip_highlight(html).lower().count("highlighttext") == 0


def test_sanitize_strips_dangerous_markup_but_keeps_lists() -> None:
    src = '<p>Hi<script>alert(1)</script><a href="javascript:x" onclick="y">l</a></p><ul><li>a</li></ul>'
    out = sanitize_html(src)
    assert "script" not in out and "javascript:" not in out and "onclick" not in out
    assert "<li>a</li>" in out
    assert html_to_text("<p>a</p><ul><li>b</li></ul>") == "a\n\n• b"
    assert html_to_text("<b>l</b><i>hl</i><br>x") == "lhl\nx"


@given(json_dicts)
def test_result_file_schema_is_total(raw: dict) -> None:
    try:
        parsed = ResultFile.model_validate(raw)
    except ValidationError:
        return
    assert isinstance(parsed.batch_id, str)
    for item in parsed.results:
        assert item.vacancy_id == item.vacancy_id.strip()
        if item.evaluation is not None:
            assert 0 <= item.evaluation.score <= 100
            assert item.evaluation.verdict in ("apply", "maybe", "skip")
        if item.letter is not None:
            assert item.letter.text.strip() == item.letter.text and item.letter.text


@given(st.integers(min_value=-50, max_value=150), st.text(max_size=20))
def test_evaluation_bounds(score: int, verdict: str) -> None:
    try:
        ev = Evaluation.model_validate({"score": score, "verdict": verdict})
    except ValidationError:
        assert not (0 <= score <= 100 and verdict in ("apply", "maybe", "skip"))
        return
    assert 0 <= ev.score <= 100


@given(st.text(max_size=200))
def test_letter_helpers(text: str) -> None:
    try:
        letter = Letter.model_validate({"text": text})
    except ValidationError:
        assert not text.strip()
        return
    assert isinstance(letter.markers(), list) and isinstance(letter.cliches(), list)
    assert isinstance(letter.has_links(), bool)


def test_packet_roundtrip_through_json() -> None:
    packet = Packet(batch_id="20260923_1200_ab12", created_at="2026-09-23T12:00:00+00:00", profile_version=1)
    again = Packet.model_validate_json(packet.model_dump_json())
    assert again == packet
