"""Input validation and tolerant parsing: impossible .env values are refused with a
readable message, bad dates / huge ids / damaged files give 4xx instead of 500, links of other sites are not taken for
hh ids, CSV cells cannot run formulas, the weekly probe survives restarts, and so on."""

from __future__ import annotations

import csv
import io
from datetime import time, timedelta

import pytest
import respx
from pydantic import ValidationError
from sqlalchemy import select
from typer.testing import CliRunner

from app.config import ROOT_DIR, Settings, describe_settings_error
from app.db.base import utcnow
from app.db.models import Application, CoverLetter, HHAuth, Vacancy
from app.hh.negotiation import normalize_chat_messages, normalize_negotiation_messages
from app.hh.resume import normalize_resume
from app.runtime import Runtime
from app.services import applier
from app.services import channels as ch
from app.services.company_sites import location_matches
from app.services.mail_sync import vacancy_ids
from app.services.manual_add import parse_vacancy_ids
from app.services.probe_runner import probe_due
from app.services.questions import parse_questions
from app.services.resume_parse import total_months
from app.services.resume_text import UnreadableFile, extract_text
from app.services.scanner import find_duplicate
from app.services.state import get_system_state
from app.services.stats import _csv_cell, _CsvWriter, clear_cache, export_applications_csv
from tests.integration.test_api_m4 import _consent, _seed, api  # noqa: F401

__all__ = ["api"]  # fixture shared with test_api_m4


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


# ---- .env values -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "values",
    [
        {"apply_window": "25:00-21:00"},
        {"apply_window": "09:00"},
        {"apply_window": "9-21"},
        {"backup_at": "3:61"},
        {"telegram_daily_summary_at": "вечером"},
        {"hot_digest_at": "24:00"},
        {"freshness_check_at": "4.10"},
        {"score_threshold_maybe": 80, "score_threshold_suitable": 70},
        {"score_threshold_suitable": 120},
        {"score_threshold_maybe": -1},
        {"apply_mode": "turbo"},
        {"direct_policy": "maybe"},
        {"hh_public_auth": "both"},
        {"apply_max_per_day": -5},
        {"backup_keep": -1},
        {"hh_rps": 0},
        {"batch_size": 0},
    ],
)
def test_settings_refuse_impossible_values(values: dict) -> None:
    with pytest.raises(ValidationError):
        _settings(**values)


def test_settings_accept_what_the_code_understands() -> None:
    s = _settings(
        apply_window=" 9:00 - 21:30 ",
        backup_at="03:30",
        hot_digest_at="",  # empty = the job is off
        freshness_check_at="",
        telegram_daily_summary_at="7:05",
        score_threshold_maybe=70,
        score_threshold_suitable=70,  # equal thresholds are fine
        apply_mode="API",  # case used to fall back to auto silently
        direct_policy=" Keep ",
        hh_public_auth="USER",
        probe_interval_days=0,
        channel_sync_min=0,
    )
    assert (s.apply_mode, s.direct_policy, s.hh_public_auth) == ("api", "keep", "user")
    assert applier.parse_window(s.apply_window) == (time(9, 0), time(21, 30))
    Settings(_env_file=str(ROOT_DIR / ".env.example"))  # type: ignore[call-arg]  # the template itself passes


def test_settings_error_names_every_bad_key() -> None:
    with pytest.raises(ValidationError) as info:
        _settings(apply_window="bogus", backup_at="99:99", score_threshold_maybe=90)
    text = describe_settings_error(info.value)
    assert text.startswith("Ошибка в .env")
    assert "APPLY_WINDOW:" in text and "«bogus»" in text and "BACKUP_AT:" in text
    assert "Value error" not in text
    with pytest.raises(ValidationError) as info:
        _settings(score_threshold_maybe=90)
    assert "SCORE_THRESHOLD_MAYBE ≤ SCORE_THRESHOLD_SUITABLE" in describe_settings_error(info.value)


def test_cli_explains_a_bad_env_instead_of_a_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import cli

    def bad() -> Settings:
        return _settings(apply_window="25:00-21:00")

    monkeypatch.setattr(cli, "get_settings", bad)
    for args in (["doctor"], ["doctor", "--json"], ["status"]):
        res = CliRunner().invoke(cli.app, args)
        assert res.exit_code == 2, (args, res.output)
        assert "APPLY_WINDOW" in res.output and "Traceback" not in res.output


def test_server_start_logs_a_bad_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app import main

    monkeypatch.setattr(main, "ROOT_DIR", tmp_path)
    with pytest.raises(ValidationError) as info:
        _settings(score_threshold_suitable=150)
    main._report_bad_settings(info.value)
    logged = (tmp_path / "logs" / "app.log").read_text(encoding="utf-8")
    assert "settings_invalid" in logged and "SCORE_THRESHOLD" in logged


def test_parse_window_survives_a_stale_value() -> None:
    assert applier.parse_window("25:00-21:00") == (time(0, 0), time(23, 59))
    assert applier.parse_window("22:00-06:00") == (time(22, 0), time(6, 0))


# ---- API input -------------------------------------------------------------------------------------------------


async def test_huge_ids_and_bad_dates_are_client_errors(api) -> None:
    client, _ = api
    huge = "99999999999999999999"
    for method, url in (
        ("GET", f"/api/vacancies/{huge}"),
        ("POST", f"/api/vacancies/{huge}/skip"),
        ("DELETE", f"/api/searches/{huge}"),
    ):
        r = await client.request(method, url, json={})
        assert r.status_code == 422, (url, r.status_code, r.text[:200])
    for params in ({"date_from": "2026-13-45"}, {"date_from": "0001-01-01"}, {"date_to": "9999-12-31"}):
        r = await client.get("/api/applications", params=params)
        assert r.status_code == 400 and "неверная дата" in r.json()["detail"], (params, r.text[:200])
    r = await client.get("/api/stats", params={"period": "custom", "date_from": "9999-12-31"})
    assert r.status_code == 400
    r = await client.get("/api/stats/export", params={"kind": "applications", "period": "custom", "date_from": "1-1"})
    assert r.status_code == 400


@pytest.mark.parametrize(
    ("name", "data", "kind"),
    [
        ("cv.pdf", b"%PDF-1.4\n\x00\x01 not really a pdf", "PDF"),
        ("cv.docx", b"PK\x03\x04 broken zip", "DOCX"),
        ("cv.docx", "Иван, Python".encode(), "DOCX"),  # a text file renamed to .docx
    ],
)
async def test_damaged_resume_file_is_a_400(api, name: str, data: bytes, kind: str) -> None:
    client, _ = api
    r = await client.post("/api/profile/resumes/upload", files={"file": (name, data, "application/octet-stream")})
    assert r.status_code == 400, r.text[:300]
    assert f"Не удалось прочитать {kind}" in r.json()["detail"]
    assert (await client.get("/api/profile")).json()["resumes"] == []
    with pytest.raises(UnreadableFile):
        extract_text(data, name)


# ---- applying ---------------------------------------------------------------------------------------------------


@respx.mock(assert_all_called=False)
async def test_autopilot_takes_only_vacancies_with_a_ready_letter(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    runtime.settings = runtime.settings.model_copy(update={"apply_autopilot": True})
    ids = await _seed(client, runtime, respx_mock, ["1", "2", "3"], response_letter_required=False)
    await _consent(client)
    async with runtime.session_factory() as session:
        for vid in ids.values():
            v = await session.get(Vacancy, vid)
            v.pipeline_status, v.score = "suitable", 95
        # «2»: only a template letter; «3»: the edited letter still carries a clarification marker
        for letter in (await session.execute(select(CoverLetter).where(CoverLetter.vacancy_id == ids["2"]))).scalars():
            letter.origin = "template"
        for letter in (await session.execute(select(CoverLetter).where(CoverLetter.vacancy_id == ids["3"]))).scalars():
            letter.text += " [[УТОЧНИТЬ: стаж с Kafka]]"
        await session.commit()
    async with runtime.session_factory() as session:
        queued = await applier.autopilot_enqueue(session, runtime.settings)
        await session.commit()
    assert queued == [ids["1"]], queued


# ---- scanning -------------------------------------------------------------------------------------------------


async def test_watched_twins_do_not_make_duplicates(settings: Settings) -> None:
    rt = await Runtime.create(settings)
    try:
        async with rt.session_factory() as session:

            def vac(hh_id: str, status: str, **kw: object) -> Vacancy:
                return Vacancy(hh_id=hh_id, name="Python-разработчик", employer_id="7", pipeline_status=status, **kw)

            watched = vac("1", "watched")
            session.add(watched)
            await session.flush()
            # an earlier regular twin that was itself marked a duplicate of the watched one → the twin counts
            regular = vac("2", "skipped", duplicate_of_id=watched.id)
            session.add(regular)
            await session.flush()
            new = vac("3", "new")
            session.add(new)
            await session.flush()
            assert (await find_duplicate(session, new)).id == regular.id
            regular.pipeline_status = "watched"
            await session.flush()
            assert await find_duplicate(session, new) is None
    finally:
        await rt.close()


def test_hh_normalizers_tolerate_scalars_in_list_fields() -> None:
    resume = normalize_resume(
        {"id": "r1", "experience": 1, "education": {"primary": "x"}, "language": 3, "certificate": "c", "site": 5}
    )
    assert resume.experience == [] and resume.education == [] and resume.languages == [] and resume.certificates == []
    assert normalize_negotiation_messages({"items": 1}) == [] and normalize_chat_messages({"messages": "x"}) == []


# ---- links -------------------------------------------------------------------------------------------------------


def test_only_hh_sites_give_vacancy_ids() -> None:
    assert parse_vacancy_ids(
        "https://hh.ru/vacancy/123456?from=x https://nn.hh.ru/vacancy/7890/ https://hh.kz/vacancy/111111 "
        "https://rabota.by/vacancy/222222 headhunter.ge/vacancy/333333 HH.RU/vacancy/444444"
    ) == ["123456", "7890", "111111", "222222", "333333", "444444"]
    assert (
        parse_vacancy_ids(
            "https://job.merlion.com/vacancy/555555 https://rabota5ka.ru/vacancy/666666 "
            "https://hh.ru.example.com/vacancy/777777 myhh.ru/vacancy/888888 https://hh.ru/vacancy/12345abc"
        )
        == []
    )
    # a bare number: 5–10 digits; 11 digits is a phone number
    assert parse_vacancy_ids("79161234567 1234 123450001 1234567890") == ["123450001", "1234567890"]
    assert vacancy_ids('<a href="https://hh.ru/vacancy/555?utm=1">x</a> https://superjob.ru/vacancy/666') == ["555"]
    assert ch.hh_vacancy_urls(["https://nn.hh.ru/vacancy/5?from=share"], "https://job.merlion.com/vacancy/9") == [
        "https://hh.ru/vacancy/5"
    ]


def test_channel_input_refuses_invites_and_service_links() -> None:
    for raw in ("https://t.me/joinchat/AAAAAEkK", "https://t.me/+AbCdEf123", "t.me/+AbCdEf123"):
        with pytest.raises(ch.ChannelError, match="приглашение"):
            ch.parse_channel_input(raw)
    for raw in ("https://t.me/share/url?url=x", "t.me/addstickers/pack", "https://t.me/proxy?server=1"):
        with pytest.raises(ch.ChannelError, match="служебная"):
            ch.parse_channel_input(raw)
    assert ch.parse_channel_input("https://t.me/s/Job_Python") == "job_python"


def test_city_matches_whole_words() -> None:
    spb = ["Санкт-Петербург"]
    assert location_matches("в Санкт-Петербурге, Невский", False, spb)
    assert location_matches("Москва, СПб", False, spb)
    assert not location_matches("СПбГУ, Москва", False, spb)
    assert not location_matches("Петербургское шоссе, Москва", False, spb)
    assert location_matches("г. Орёл", False, ["Орёл"]) and not location_matches("Орелстрой", False, ["Орёл"])


# ---- text parsing ------------------------------------------------------------------------------------------------


def test_total_experience_without_a_dash() -> None:
    assert total_months("Опыт работы 10 лет 1 месяц") == 121
    assert total_months("Опыт работы: 2 года") == 24
    assert total_months("Опыт работы — 3 года 8 месяцев") == 44
    assert total_months("Опыт работы в продажах 5 лет\nОпыт работы — 1 год") == 12
    assert total_months("Опыт работы\nООО Ромашка, 2 года") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "1. Какой у вас опыт с Python?\n2. Что использовали?\n- Docker\n- Kafka\n3) Почему мы?",
            ["Какой у вас опыт с Python?", "Что использовали?\nDocker\nKafka", "Почему мы?"],
        ),
        ("1. Опыт с Python\n2. Зарплата", ["Опыт с Python", "Зарплата"]),
        (
            "Уровень английского?\n1) A1\n2) B2\n3) C1\n\nПочему мы?",
            ["Уровень английского?\nA1\nB2\nC1", "Почему мы?"],
        ),
        ("Выберите стек:\n\n- Python\n- Go", ["Выберите стек:\nPython\nGo"]),
        (
            "Опишите самый сложный кейс,\nкоторый вы реализовали.",
            ["Опишите самый сложный кейс, который вы реализовали."],
        ),
    ],
)
def test_parse_questions_numbering_and_options(text: str, expected: list[str]) -> None:
    assert parse_questions(text) == expected


# ---- CSV -----------------------------------------------------------------------------------------------------------


def test_csv_cells_cannot_start_a_formula() -> None:
    buf = io.StringIO()
    _CsvWriter(buf).writerow(['=HYPERLINK("http://x")', "+7 999", "-сдельно", "@cmd", "\tx", "обычный", -5, 3.5])
    row = next(csv.reader(io.StringIO(buf.getvalue()), delimiter=";"))
    assert row == ['\'=HYPERLINK("http://x")', "'+7 999", "'-сдельно", "'@cmd", "'\tx", "обычный", "-5", "3.5"]
    assert _csv_cell(None) is None and _csv_cell(0) == 0


async def test_applications_export_escapes_vacancy_titles(settings: Settings) -> None:
    rt = await Runtime.create(settings)
    clear_cache()
    try:
        async with rt.session_factory() as session:
            v = Vacancy(hh_id="1", name='=HYPERLINK("http://evil","жми")', employer_name="@Рога и копыта")
            session.add(v)
            await session.flush()
            session.add(Application(vacancy_id=v.id, mode="api", status="sent", sent_at=utcnow()))
            await session.commit()
        async with rt.session_factory() as session:
            text = await export_applications_csv(session, rt.settings, period_key="all", date_from=None, date_to=None)
        rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=";"))
        assert rows[1][1] == '\'=HYPERLINK("http://evil","жми")' and rows[1][2] == "'@Рога и копыта"
    finally:
        await rt.close()


# ---- weekly probe -------------------------------------------------------------------------------------------------


async def test_probe_is_due_by_the_stored_date_not_by_uptime(settings: Settings) -> None:
    rt = await Runtime.create(settings)
    try:
        async with rt.session_factory() as session:
            s = rt.settings
            assert await probe_due(session, s) is False  # not logged in
            session.add(HHAuth(id=1, access_token="t", state="ok"))
            await session.flush()
            state = await get_system_state(session)
            assert await probe_due(session, s) is True  # never probed
            state.probe_at = utcnow() - timedelta(days=2)
            assert await probe_due(session, s) is False
            state.probe_at = utcnow() - timedelta(days=s.probe_interval_days, minutes=1)
            assert await probe_due(session, s) is True
            state.paused_reason = "captcha"
            assert await probe_due(session, s) is False
            state.paused_reason = None
            assert await probe_due(session, s.model_copy(update={"probe_interval_days": 0})) is False
    finally:
        await rt.close()


async def test_probe_tick_starts_the_job_only_when_due(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from app import scheduler
    from app.services import probe_runner

    started: list[str] = []

    class Jobs:
        async def start(self, job: str, fn: object) -> int:
            started.append(job)
            return 1

    rt = await Runtime.create(settings)
    try:
        stub = SimpleNamespace(settings=rt.settings, session_factory=rt.session_factory, jobs=Jobs())
        due = {"value": False}

        async def fake_due(session: object, s: object, now: object = None) -> bool:
            return due["value"]

        monkeypatch.setattr(probe_runner, "probe_due", fake_due)
        await scheduler.probe_tick(stub)  # type: ignore[arg-type]
        assert started == []
        due["value"] = True
        await scheduler.probe_tick(stub)  # type: ignore[arg-type]
        assert started == ["probe"]
        job = next(j for j in scheduler.build_scheduler(stub).get_jobs() if j.id == "probe_tick")  # type: ignore[arg-type]
        assert job.trigger.interval == timedelta(hours=scheduler.PROBE_CHECK_HOURS)
    finally:
        await rt.close()


def test_major_version_takes_ascii_digits_only() -> None:
    from app.services.doctor import _major

    assert _major("v24.18.0") == 24 and _major("¹.2") is None and _major("٣.1") is None and _major("") is None
