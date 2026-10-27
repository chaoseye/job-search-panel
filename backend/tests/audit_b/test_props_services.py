"""Property-тесты сервисных помощников: окно откликов, напоминания, вопросы, каналы, почта, метки, статистика,
уведомления, письма, ссылки, анкета, профиль, правила/prescore. Инварианты, а не конкретные примеры."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import assume, given
from hypothesis import settings as h_settings
from hypothesis import strategies as st
from pydantic import ValidationError

from app.config import Settings
from app.exchange.profile_md import render_profile_md
from app.exchange.schemas import Letter
from app.hh.auth import pkce_challenge
from app.hh.probe import AVAILABLE, UNAVAILABLE, UNKNOWN, _classify_default
from app.hh.resume import normalize_resume
from app.hh.vacancy import normalize_vacancy
from app.services import channels as ch
from app.services import mail_sync
from app.services.analytics import LEVEL_WORDS, _quartiles, name_terms
from app.services.applier import (
    in_window,
    local_day_bounds,
    negotiation_id_from_location,
    next_day_resume_at,
    parse_window,
)
from app.services.bot_commands import _cmd
from app.services.company_sites import SiteVacancyItem, is_match, location_matches, mass_title, slugify
from app.services.counters import local_date
from app.services.dictionaries import currency_rates
from app.services.doctor import _major
from app.services.humanizer import is_russian
from app.services.letters import check_before_send, letter_from_result, letter_warnings
from app.services.manual_add import parse_vacancy_ids
from app.services.notify import (
    EVENT_TYPES,
    digest_bucket,
    enabled_events,
    render_event,
    ui_summary,
)
from app.services.preferences import Preferences, apply_criteria, criteria_of
from app.services.prescore import prescore
from app.services.profile import safe_filename
from app.services.questions import MAX_QUESTIONS, parse_questions
from app.services.resume_bump import cron_day_of_week, describe_when, next_slot, parse_days, parse_slots, schedule_label
from app.services.resume_parse import parse_resume_text, total_months
from app.services.resume_text import normalize_whitespace
from app.services.rules import RulesContext, apply_rules
from app.services.searches import SearchParams, build_query
from app.services.state import captcha_url_with_backurl
from app.services.stats import PERIODS, SALARY_BUCKETS, SCORE_BUCKETS, resolve_period, salary_bucket, score_bucket
from app.services.tagging import GROUPS, MAX_PER_GROUP, canonical, clean_tags, load_tags, merge_tags, rule_tags
from app.services.telegram import TelegramError, _apply_failure
from app.services.textmatch import _EMOJI
from app.services.tg_letters import ACTIONS, keyboard, parse_callback

SETTINGS = Settings(_env_file=None, timezone="Europe/Moscow")  # type: ignore[call-arg]
MSK = ZoneInfo("Europe/Moscow")
any_text = st.text(max_size=300)
json_scalars = st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(max_size=20)
json_values = st.recursive(
    json_scalars,
    lambda c: st.lists(c, max_size=4) | st.dictionaries(st.text(max_size=10), c, max_size=5),
    max_leaves=20,
)


# ---- applier: окно, границы дня, Location ---------------------------------------------------------


@given(
    st.integers(0, 23), st.integers(0, 59), st.integers(0, 23), st.integers(0, 59), st.datetimes(timezones=st.just(MSK))
)
def test_in_window_matches_manual_rule(h1: int, m1: int, h2: int, m2: int, now: datetime) -> None:
    window = f"{h1:02d}:{m1:02d}-{h2:02d}:{m2:02d}"
    start, end = parse_window(window)
    assert (start.hour, start.minute, end.hour, end.minute) == (h1, m1, h2, m2)
    t = now.time().replace(second=0, microsecond=0)
    expected = start <= t <= end if start <= end else (t >= start or t <= end)
    assert in_window(now, window) == expected


@given(st.text(max_size=20))
def test_parse_window_garbage_means_whole_day(value: str) -> None:
    assume(not re.fullmatch(r"\s*\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}\s*", value))
    assert parse_window(value) == parse_window("")


@given(st.from_regex(r"[A-Za-z0-9_-]{1,20}", fullmatch=True), st.text(max_size=10))
def test_negotiation_id_from_location(nid: str, junk: str) -> None:
    assume("/" not in junk and "?" not in junk and "#" not in junk)
    assert negotiation_id_from_location(f"/negotiations/{nid}") == nid
    assert negotiation_id_from_location(f"https://api.hh.ru/negotiations/{nid}?x=1") == nid
    assert negotiation_id_from_location(None) is None
    assert negotiation_id_from_location("/vacancies/1") is None


@given(st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2035, 1, 1), timezones=st.just(UTC)))
def test_local_day_bounds_contain_now_and_span_a_day(now: datetime) -> None:
    start, end = local_day_bounds(SETTINGS, now)
    assert start <= now < end
    assert end - start == timedelta(days=1)  # Europe/Moscow без перевода часов
    assert start.astimezone(MSK).time() == datetime.min.time()
    resume_at = next_day_resume_at(SETTINGS, now)
    assert resume_at > now and resume_at.astimezone(MSK).strftime("%H:%M") == "00:05"
    assert local_date(SETTINGS, now) == now.astimezone(MSK).strftime("%Y-%m-%d")


# ---- напоминание о резюме --------------------------------------------------------------------------


@given(any_text)
def test_parse_slots_and_days_are_total(value: str) -> None:
    slots = parse_slots(value)
    assert slots == sorted(set(slots))
    assert all(0 <= h <= 23 and 0 <= m <= 59 for h, m in slots)
    days = parse_days(value)
    assert days == sorted(set(days)) and all(0 <= d <= 6 for d in days)
    assert cron_day_of_week(days) is None or set(cron_day_of_week(days).split(",")) <= set(  # type: ignore[union-attr]
        ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    )
    label = schedule_label(slots, days)
    assert (label is None) == (not slots)


@given(
    st.lists(st.tuples(st.integers(0, 23), st.integers(0, 59)), min_size=1, max_size=4, unique=True),
    st.lists(st.integers(0, 6), min_size=1, max_size=7, unique=True),
    st.datetimes(min_value=datetime(2024, 1, 1), max_value=datetime(2030, 1, 1), timezones=st.just(MSK)),
)
def test_next_slot_is_the_first_future_slot(slots: list[tuple[int, int]], days: list[int], now: datetime) -> None:
    slots, days = sorted(slots), sorted(days)
    nxt = next_slot(now, slots, days)
    assert nxt is not None
    assert nxt > now and nxt - now <= timedelta(days=8)
    assert nxt.weekday() in days and (nxt.hour, nxt.minute) in slots
    # ничего раньше между now и nxt
    for d in range(0, 8):
        day = now.date() + timedelta(days=d)
        if day.weekday() not in days:
            continue
        for h, m in slots:
            cand = datetime(day.year, day.month, day.day, h, m, tzinfo=MSK)
            if now < cand < nxt:
                pytest.fail(f"пропущен более ранний слот {cand}")
    assert describe_when(nxt, now).endswith(nxt.strftime("%H:%M"))


def test_parse_days_examples() -> None:
    assert parse_days("mon-fri") == [0, 1, 2, 3, 4]
    assert parse_days("sat-mon") == [0, 5, 6]
    assert parse_days("*") == list(range(7)) and parse_days("") == list(range(7))
    assert parse_days("mon,wed,fri") == [0, 2, 4] and parse_days("xyz") == []
    assert schedule_label([(8, 45), (13, 0)], [0, 1, 2, 3, 4]) == "08:45, 13:00 (пн–пт)"


# ---- вопросы работодателя ---------------------------------------------------------------------------


@given(any_text)
def test_parse_questions_is_total_and_bounded(text: str) -> None:
    out = parse_questions(text)
    assert len(out) <= MAX_QUESTIONS
    assert all(len(q) >= 5 for q in out)


# исправлено сессией «_a» 2026-09-23 (fix/input-validation-and-parsing): нумерованные вопросы делятся, варианты остаются при вопросе
def test_parse_questions_numbered_and_options() -> None:
    text = "1. Какой у вас опыт с Python?\n\n2. Что использовали?\n\n- Docker\n- Kafka\n\n3) Почему мы?"
    out = parse_questions(text)
    assert len(out) == 3, out
    assert out[1].startswith("Что использовали?") and "Docker" in out[1] and "Kafka" in out[1]
    assert out[2] == "Почему мы?"


def test_parse_questions_unnumbered_paragraphs() -> None:
    text = "Какой у вас опыт с Python?\n\nЧто использовали?\n\n- Docker\n- Kafka\n\nПочему мы?"
    out = parse_questions(text)
    assert len(out) == 3 and out[1] == "Что использовали?\nDocker\nKafka" and out[2] == "Почему мы?"


# ---- Telegram-каналы ---------------------------------------------------------------------------------


@given(any_text)
def test_channel_heuristics_are_total(text: str) -> None:
    assert ch.detect_language(text) in ("ru", "en", "uk", "other")
    assert isinstance(ch.looks_like_vacancy(text), bool)
    title = ch.post_title(text)
    assert len(title) <= ch.TITLE_CHARS and _EMOJI.search(title) is None
    for url in ch.extract_links([], text):
        assert url.startswith(("http://", "https://"))
    for url in ch.hh_vacancy_urls([], text):
        assert re.fullmatch(r"https://hh\.ru/vacancy/\d{1,12}", url)
    page = ch.parse_preview(text)
    assert isinstance(page.posts, list)


@given(st.text(max_size=60))
def test_parse_channel_input_returns_a_valid_username_or_refuses(text: str) -> None:
    try:
        name = ch.parse_channel_input(text)
    except ch.ChannelError:
        return
    assert ch.USERNAME_RE.match(name) and name == name.lower()


def test_parse_channel_input_forms() -> None:
    for raw in (
        "@Job_Python",
        "job_python",
        "https://t.me/Job_Python",
        "t.me/s/job_python",
        "https://t.me/job_python/123",
    ):
        assert ch.parse_channel_input(raw) == "job_python"
    with pytest.raises(ch.ChannelError):
        ch.parse_channel_input("https://t.me/addlist/AbCdEf")
    with pytest.raises(ch.ChannelError):
        ch.parse_channel_input("ab")


def test_language_detection_examples() -> None:
    assert ch.detect_language("Ищем Python-разработчика в команду, удалённо") == "ru"
    assert ch.detect_language("Шукаємо розробника, віддалено, досвід від 2 років") == "uk"
    assert ch.detect_language("We are hiring a senior engineer, remote, apply now") == "en"
    assert ch.detect_language("12345 !!!") == "other"


# ---- письма hh.ru → журнал ----------------------------------------------------------------------------


@given(any_text, any_text)
def test_mail_classify_and_snippet_are_total(subject: str, text: str) -> None:
    kind = mail_sync.classify(subject, text)
    assert kind in (None, "invitation", "discard", "viewed", "message")
    out = mail_sync.snippet(text)
    assert len(out) <= mail_sync.SNIPPET_CHARS and "http" not in out
    for vid in mail_sync.vacancy_ids(subject, text):
        assert vid.isdigit()


@given(st.binary(max_size=2000))
def test_parse_message_never_raises_on_garbage_mail(raw: bytes) -> None:
    item = mail_sync.parse_message(raw, uid=7)
    assert item.uid == 7 and isinstance(item.subject, str) and isinstance(item.text, str)


def test_classify_priority_invitation_over_viewed() -> None:
    assert mail_sync.classify("Приглашение на собеседование", "Работодатель просмотрел ваш отклик") == "invitation"
    assert mail_sync.classify("Отклик просмотрен", "") == "viewed"
    assert mail_sync.classify("Рассылка недели", "лучшие вакансии") is None


# ---- метки -----------------------------------------------------------------------------------------------


@given(any_text, st.lists(st.text(max_size=20), max_size=6), any_text | st.none())
def test_rule_tags_bounded(name: str, skills: list[str], desc: str | None) -> None:
    tags = rule_tags(name, skills, desc)
    assert set(tags) == set(GROUPS)
    for g in GROUPS:
        assert len(tags[g]) <= MAX_PER_GROUP[g] and len(set(tags[g])) == len(tags[g])


@given(json_values)
def test_clean_and_merge_tags_are_total(raw: object) -> None:
    tags = clean_tags(raw)
    assert set(tags) == set(GROUPS)
    merged = merge_tags(tags, tags)
    assert merged == tags  # объединение с собой ничего не добавляет
    assert load_tags("not json") == clean_tags(None)


def test_canonical_maps_free_labels() -> None:
    assert canonical("stack", "RAG-пайплайны") == "RAG"
    assert canonical("stack", "python") == "Python"
    assert canonical("tasks", "чат-боты") == "чат-боты и ассистенты"
    assert canonical("domain", "совсем новая метка") == "совсем новая метка"


# ---- сайты компаний ------------------------------------------------------------------------------------


@given(st.text(min_size=1, max_size=80))
def test_mass_title_and_slugify(title: str) -> None:
    assume(title.strip())
    assert mass_title(title).strip()
    assert re.fullmatch(r"[a-z0-9\-]*", slugify(title))


@given(any_text | st.none(), st.booleans())
def test_location_rules(location: str | None, remote: bool) -> None:
    cities = ["Санкт-Петербург"]
    if remote:
        assert location_matches(location, True, cities)
    item = SiteVacancyItem("1", "Менеджер по продажам", "https://x", location, None, remote)
    if is_match(item, city_list=cities, stop_words=["продаж"], keywords=[]):
        pytest.fail("стоп-слово в названии должно отсеивать")


def test_location_matches_examples() -> None:
    assert location_matches("г. Санкт-Петербург, Невский", False, ["Санкт-Петербург"])
    assert location_matches("СПб", False, ["Санкт-Петербург"])
    assert not location_matches("Москва", False, ["Санкт-Петербург"])
    assert not location_matches(None, False, ["Санкт-Петербург"])


# ---- статистика и аналитика -----------------------------------------------------------------------------


@given(st.integers(-10, 110) | st.none())
def test_score_bucket_covers_every_score(score: int | None) -> None:
    label = score_bucket(score)
    labels = {lbl for _, _, lbl in SCORE_BUCKETS} | {"нет оценки"}
    assert label in labels
    if score is not None and 0 <= score <= 100:
        assert label != "нет оценки"


@given(st.integers(0, 2_000_000) | st.none())
def test_salary_bucket_is_monotone(value: int | None) -> None:
    order = [lbl for _, _, lbl in SALARY_BUCKETS]
    label = salary_bucket(value)
    if value is None:
        assert label == "не указана"
    else:
        assert label in order
        if value >= 1:
            assert order.index(salary_bucket(value)) >= order.index(salary_bucket(value - 1))


@given(st.sampled_from(PERIODS + ("bogus",)), st.dates(), st.dates())
def test_resolve_period_bounds(key: str, a, b) -> None:  # type: ignore[no-untyped-def]
    if key == "custom" and not all(2000 <= d.year <= 2100 for d in (a, b)):
        # 0001-01-01 / 9999-12-31 overflowed Period.start/end (500 in /api/applications): refused with a message now
        with pytest.raises(ValueError, match="вне диапазона"):
            resolve_period(SETTINGS, key, date_from=a.isoformat(), date_to=b.isoformat())
        return
    period = resolve_period(SETTINGS, key, date_from=a.isoformat(), date_to=b.isoformat())
    assert period.start < period.end
    assert period.date_from <= period.date_to
    assert period.start.tzinfo is UTC and period.end.tzinfo is UTC
    if key == "custom":
        assert {period.date_from, period.date_to} == {min(a, b), max(a, b)}


@given(any_text)
def test_name_terms_skip_level_words(name: str) -> None:
    terms = name_terms(name)
    for t in terms:
        for w in t.split(" "):
            assert w not in LEVEL_WORDS and not w.isdigit()
    assert len(terms) == len(set(terms))


@given(st.lists(st.integers(0, 10**6), max_size=30))
def test_quartiles_are_ordered(values: list[int]) -> None:
    p25, med, p75 = _quartiles(values)
    if values:
        assert p25 is not None and med is not None and p75 is not None
        assert min(values) <= p25 <= med <= p75 <= max(values)
    else:
        assert (p25, med, p75) == (None, None, None)


# ---- уведомления -------------------------------------------------------------------------------------------

NUMERIC_KEYS = (
    "score",
    "salary",
    "salary_from",
    "salary_to",
    "failures",
    "batches",
    "tasks",
    "evaluations",
    "letters",
    "suitable",
    "maybe",
    "errors",
    "missing",
    "total",
    "found",
    "sent",
    "viewed",
    "invitations",
    "awaiting",
    "no_answer",
    "journal_days",
    "journal_total",
    "version",
    "max_age_days",
    "max_responses",
    "min_score",
    "vacancy_id",
    "post_id",
    "channel_id",
    "application_id",
    "age_hours",
    "responses",
    "prescore",
)
TEXT_KEYS = (
    "name",
    "employer",
    "url",
    "text",
    "negotiation_id",
    "mode",
    "mode_label",
    "until",
    "reason",
    "job",
    "error",
    "batch_id",
    "apply_mode",
    "prev_mode",
    "slot",
    "next",
    "title",
    "username",
    "channel",
    "hh_url",
    "snippet",
    "company",
    "site",
    "location",
    "search",
    "currency",
    "area",
    "address",
    "date",
    "letter",
    "summary",
    "work_format",
    "status",
    "verdict",
    "author",
)
DICT_LIST_KEYS = ("top", "changes", "items", "journal")
BOOL_KEYS = ("remote", "revision", "needs_clarification")
small_dict = st.dictionaries(
    st.sampled_from(
        (
            "name",
            "score",
            "url",
            "employer",
            "key",
            "old",
            "new",
            "days",
            "responses",
            "age_hours",
            "salary",
            "prescore",
            "hh_id",
        )
    ),
    st.none() | st.integers(0, 1000) | st.text(max_size=15),
    max_size=6,
)
payloads = st.fixed_dictionaries(
    {},
    optional={
        **{k: st.none() | st.integers(0, 10**6) for k in NUMERIC_KEYS},
        **{k: st.none() | st.text(max_size=40) for k in TEXT_KEYS},
        **{k: st.lists(small_dict, max_size=4) for k in DICT_LIST_KEYS},
        "metro": st.lists(st.text(max_size=10), max_size=3),
        **{k: st.booleans() for k in BOOL_KEYS},
    },
)


@h_settings(max_examples=60)
@given(st.sampled_from(EVENT_TYPES), st.lists(payloads, min_size=1, max_size=3), st.booleans())
def test_render_and_summary_never_raise_for_realistic_payloads(event: str, items: list[dict], chunked: bool) -> None:
    part = (1, 2, len(items) + 5) if chunked else None
    text = render_event(event, items, timezone="Europe/Moscow", part=part)
    assert isinstance(text, str) and text
    assert isinstance(ui_summary(event, items[0]), str)


@given(st.text(max_size=100))
def test_enabled_events_parsing(raw: str) -> None:
    events = enabled_events(Settings(_env_file=None, telegram_events=raw))  # type: ignore[call-arg]
    assert all(e and e == e.strip() for e in events)


@given(st.datetimes(timezones=st.just(UTC)), st.integers(0, 299))
def test_digest_bucket_groups_five_minutes(moment: datetime, delta: int) -> None:
    key = digest_bucket("digest", moment)
    assert key.startswith("digest:")
    # моменты внутри одного 5-минутного окна имеют один ключ, через 5 минут — другой
    same_window = int(moment.timestamp() // 300) == int((moment + timedelta(seconds=delta)).timestamp() // 300)
    assert (digest_bucket("digest", moment + timedelta(seconds=delta)) == key) == same_window
    assert digest_bucket("digest", moment + timedelta(seconds=300)) != key


@given(st.booleans(), st.floats(min_value=1, max_value=3600) | st.none(), st.integers(1, 8))
def test_telegram_failure_policy(transient: bool, retry_after: float | None, rounds: int) -> None:
    from app.db.models import Notification

    row = Notification(channel="telegram", event_type="digest", payload_json="{}", status="queued", attempts=0)
    now = datetime.now(UTC)
    for _ in range(rounds):
        _apply_failure([row], TelegramError("x", transient=transient, retry_after=retry_after), now)
    if not transient:
        assert row.status == "failed" and row.attempts == rounds
    else:
        # исправлено сессией «_a» (d77959b): временная ошибка не сдаётся по числу попыток — строка ждёт до STALE_AFTER
        assert row.status == "queued" and row.next_attempt_at is not None and row.next_attempt_at > now
        if retry_after:
            assert row.next_attempt_at == now + timedelta(seconds=retry_after)


# ---- кнопки писем в боте ---------------------------------------------------------------------------------------


@given(st.integers(1, 2**31), st.text(max_size=60) | st.none(), st.sampled_from(ACTIONS))
def test_keyboard_callback_roundtrip_and_size(vacancy_id: int, url: str | None, action: str) -> None:
    kb = keyboard(vacancy_id, url)
    buttons = kb["inline_keyboard"][0]
    for b in buttons:
        assert len(b["callback_data"].encode()) <= 64  # лимит Telegram
        parsed = parse_callback(b["callback_data"])
        assert parsed is not None and parsed[0] == vacancy_id and parsed[1] in ACTIONS
    assert parse_callback(f"vac:{vacancy_id}:{action}") == (vacancy_id, action)
    assert parse_callback("vac:x:sent") is None and parse_callback(None) is None
    assert (len(kb["inline_keyboard"]) == 2) == bool(url)


# ---- письма ----------------------------------------------------------------------------------------------------


@given(any_text | st.none(), st.booleans())
def test_check_before_send_is_total(text: str | None, required: bool) -> None:
    problems = check_before_send(text, required=required)
    assert isinstance(problems, list)
    if not (text or "").strip():
        assert problems == (["сопроводительное письмо обязательно для этой вакансии"] if required else [])
    else:
        assert isinstance(letter_warnings(text or ""), list)


@given(st.text(min_size=1, max_size=3000), st.integers(100, 1000), st.integers(1000, 3000))
def test_letter_from_result_length_warnings(text: str, lo: int, hi: int) -> None:
    assume(text.strip())
    letter = Letter.model_validate({"text": text})
    warnings, clar = letter_from_result(letter, min_chars=lo, max_chars=hi)
    n = len(letter.text)
    short = any("короче" in w for w in warnings)
    long = any("длиннее" in w for w in warnings)
    assert short == (n < int(lo * 0.8))
    assert long == (not short and n > int(hi * 1.2))
    for m in letter.markers():
        assert m in clar


# ---- ссылки, файлы, url -----------------------------------------------------------------------------------------


@given(any_text)
def test_parse_vacancy_ids_shape(text: str) -> None:
    ids = parse_vacancy_ids(text)
    assert all(i.isdigit() and 1 <= len(i) <= 12 for i in ids)
    assert len(ids) == len(set(ids))


def test_parse_vacancy_ids_examples() -> None:
    text = "https://hh.ru/vacancy/123456?from=x, https://nn.hh.ru/vacancy/7890/ 123456 55555 1234 abc"
    assert parse_vacancy_ids(text) == ["123456", "7890", "55555"]


@given(st.text(max_size=200))
def test_safe_filename_has_no_separators(name: str) -> None:
    out = safe_filename(name)
    assert out and len(out) <= 120
    assert "/" not in out and "\\" not in out and not out.startswith(".")


@given(st.from_regex(r"https://hh\.ru/[a-z/]{1,20}(\?[a-z]=[0-9]{1,3})?", fullmatch=True))
def test_captcha_backurl_is_appended_once(url: str) -> None:
    out = captcha_url_with_backurl(url, "http://localhost:8000/api/auth/captcha-done")
    assert out.count("backurl=") == 1 and out.startswith(url)
    assert ("&backurl=" in out) == ("?" in url)


@given(st.text(min_size=43, max_size=96, alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_"))
def test_pkce_challenge_shape(verifier: str) -> None:
    challenge = pkce_challenge(verifier)
    assert len(challenge) == 43 and re.fullmatch(r"[A-Za-z0-9_-]+", challenge)


# ---- анкета и профиль -------------------------------------------------------------------------------------------

prefs_st = st.builds(
    Preferences,
    target_titles=st.lists(st.text(max_size=15), max_size=3),
    stop_words_title=st.lists(st.text(max_size=10), max_size=3),
    stop_words_description=st.lists(st.text(max_size=10), max_size=2),
    salary_min=st.none() | st.integers(0, 500_000),
    salary_missing=st.sampled_from(["keep", "skip"]),
    work_formats=st.lists(st.sampled_from(["REMOTE", "HYBRID", "ON_SITE", "junk"]), max_size=3),
    areas=st.lists(st.text(max_size=12), max_size=2),
    employment_forms=st.lists(st.sampled_from(["FULL", "PART", "x"]), max_size=2),
    experience_min_years=st.none() | st.floats(0, 20),
    experience_max_years=st.none() | st.floats(0, 20),
    own_experience_years=st.none() | st.floats(0, 60),
    no_agencies=st.booleans(),
    accredited_it_only=st.booleans(),
)


@given(prefs_st)
def test_criteria_roundtrip_is_stable(prefs: Preferences) -> None:
    base = Preferences()
    applied = apply_criteria(base, criteria_of(prefs))
    assert criteria_of(applied) == criteria_of(prefs)
    assert applied.letter_style == base.letter_style  # не критерий — остаётся от анкеты


@given(st.dictionaries(st.text(max_size=12), json_values, max_size=8))
def test_preferences_validation_is_total(raw: dict) -> None:
    try:
        prefs = Preferences.model_validate(raw)
    except ValidationError:
        return
    assert all(f in ("REMOTE", "HYBRID", "ON_SITE", "FIELD_WORK") for f in prefs.work_formats)
    names = [s.name.lower() for s in prefs.skills_weights]
    assert len(names) == len(set(names))


@given(st.dictionaries(st.text(max_size=12), json_values, max_size=8), prefs_st, st.text(max_size=100) | st.none())
def test_profile_md_renders_for_any_resume(raw: dict, prefs: Preferences, file_text: str | None) -> None:
    md = render_profile_md(
        version=3,
        generated_at="2026-09-23T00:00:00",
        resume=normalize_resume(raw),
        file_text=file_text,
        file_name="cv.pdf" if file_text else None,
        prefs=prefs,
    )
    assert md.startswith("---\nprofile_version: 3\n") and "## Стиль писем" in md


# ---- правила и prescore на случайных вакансиях ---------------------------------------------------------------

VAC_KEYS = (
    "id",
    "name",
    "employer",
    "salary",
    "snippet",
    "test",
    "counters",
    "work_format",
    "relations",
    "description",
    "key_skills",
    "area",
    "experience",
    "employment_form",
    "schedule",
    "published_at",
    "archived",
    "type",
    "response_letter_required",
    "has_test",
    "response_url",
)
vacancy_raw = st.dictionaries(st.sampled_from(VAC_KEYS), json_values, max_size=12)  # скаляры в полях-списках тоже


@h_settings(max_examples=50)
@given(vacancy_raw, prefs_st, st.booleans(), st.none() | st.integers(0, 600))
def test_rules_and_prescore_are_total_and_bounded(
    raw: dict, prefs: Preferences, full: bool, months: int | None
) -> None:
    v = normalize_vacancy(raw, full=full)
    result = apply_rules(v, RulesContext(prefs=prefs, currency_rates={"RUR": 1.0}))
    assert result.passed == (result.status is None)
    if not result.passed:
        assert result.status in ("filtered_out", "hidden", "already_applied") and len(result.reasons) == 1
    ps = prescore(
        v, prefs, weights=prefs.merged_skills(["Python"]), candidate_months=months, currency_rates={"RUR": 1.0}
    )
    assert 0 <= ps.score <= 100 and ps.notes and ps.notes[0].startswith(f"итого {ps.score}:")


# ---- разное ----------------------------------------------------------------------------------------------------


@given(st.dictionaries(st.text(max_size=8), json_values, max_size=5))
def test_search_params_validation_and_query(raw: dict) -> None:
    try:
        params = SearchParams.model_validate(raw)
    except ValidationError:
        return
    q = build_query(params, page=0, period=99)
    assert q["period"] <= 30 and q["order_by"] == "publication_time"
    q2 = build_query(params, page=1, date_from=datetime(2026, 9, 1, tzinfo=UTC))
    assert "period" not in q2 and q2["date_from"].startswith("2026-09-01")
    assert isinstance(params.summary(), str)


@given(st.text(max_size=30))
def test_bot_command_parsing(text: str) -> None:
    cmd = _cmd(text)
    if cmd is not None:
        assert cmd.startswith("/") and "@" not in cmd and cmd == cmd.lower() and " " not in cmd
    assert _cmd("/Status@ExamplePanelBot now") == "/status" and _cmd("hello") is None


@given(any_text)
def test_normalize_whitespace_is_idempotent(text: str) -> None:
    out = normalize_whitespace(text)
    assert normalize_whitespace(out) == out
    assert "\r" not in out and "\x00" not in out and "\n\n\n" not in out and out == out.strip()


@given(any_text, any_text | st.none())
def test_resume_parser_is_total(text: str, layout: str | None) -> None:
    parsed = parse_resume_text(text, layout)
    if parsed is not None:
        assert parsed.origin == "file" and all(1 < len(s) <= 60 for s in parsed.skills)


def test_total_months_examples() -> None:
    assert total_months("Опыт работы — 3 года 8 месяцев") == 44
    assert total_months("Опыт работы — 8 месяцев") == 8
    assert total_months("Опыт работы — 1 год") == 12
    assert total_months("нет такого") is None


@given(st.dictionaries(st.text(max_size=8), json_values, max_size=5))
def test_currency_rates_always_has_rubles(dicts: dict) -> None:
    rates = currency_rates(dicts)
    assert rates["RUR"] == 1.0 and rates["RUB"] == 1.0 and all(isinstance(v, float) for v in rates.values())


def test_probe_default_classification() -> None:
    from app.hh.client import HHResponse
    from app.hh.errors import HHError

    assert _classify_default(HHResponse(200, {}, {}, ""))[0] == AVAILABLE
    assert _classify_default(HHError(status=403))[0] == UNAVAILABLE
    assert _classify_default(HHError(status=404))[0] == UNAVAILABLE
    assert _classify_default(HHError(status=400))[0] == AVAILABLE
    assert _classify_default(HHError(status=None))[0] == UNKNOWN


@given(st.text(alphabet=st.characters(max_codepoint=127), max_size=20) | st.none())
def test_major_version(version: str | None) -> None:
    out = _major(version)
    assert out is None or out >= 0
    assert _major("v24.18.0") == 24 and _major("3.11.9") == 3 and _major(None) is None


# исправлено сессией «_a» 2026-09-23 (fix/input-validation-and-parsing): _major берёт только ASCII-цифры
def test_major_version_unicode_digit() -> None:
    assert _major("¹") is None


@given(any_text)
def test_is_russian_is_total(text: str) -> None:
    assert isinstance(is_russian(text), bool)
    assert is_russian("Здравствуйте, меня зовут Иван") and not is_russian("Hello there") and not is_russian("123")
