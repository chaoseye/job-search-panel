"""hh.ru e-mails → journal: classification, parsing, matching; the borderline report."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.config import Settings
from app.services.analytics import Base, E, V, borderline_report
from app.services.mail_sync import MailItem, classify, match_application, parse_message, snippet, vacancy_ids


def test_classify_and_links() -> None:
    assert classify("Вас пригласили на собеседование", "") == "invitation"
    assert classify("Работодатель просмотрел ваш отклик", "") == "viewed"
    assert classify("Отказ по вакансии Python developer", "…") == "discard"
    assert classify("Новое сообщение от работодателя", "") == "message"
    assert classify("Приглашение", "к сожалению, работодатель…") == "invitation"  # the subject wins over the body
    assert classify("HeadHunter", "Компания посмотрела ваше резюме") == "viewed"
    assert classify("Подборка вакансий для вас", "Новые вакансии по вашим запросам") is None
    assert vacancy_ids("https://hh.ru/vacancy/123456?from=mail", "текст https://nn.hh.ru/vacancy/777/ и 123456") == [
        "123456",
        "777",
    ]


def test_parse_message_and_snippet() -> None:
    msg = EmailMessage()
    msg["Subject"] = "Вас пригласили на собеседование"
    msg["From"] = "HeadHunter <no-reply@hh.ru>"
    msg["Message-ID"] = "<abc@hh.ru>"
    msg["Date"] = "Mon, 14 Sep 2026 10:15:00 +0300"
    msg.set_content("Компания Рога и копыта приглашает вас.\nhttps://hh.ru/vacancy/555\nОтписаться: https://hh.ru/x")
    msg.add_alternative(
        '<p>Компания <b>Рога и копыта</b> приглашает вас.</p><a href="https://hh.ru/vacancy/555?utm=1">Открыть</a>',
        subtype="html",
    )
    item = parse_message(bytes(msg), uid=42)
    assert item.uid == 42 and item.message_id == "<abc@hh.ru>" and item.sender.endswith("<no-reply@hh.ru>")
    assert item.date == datetime(2026, 9, 14, 7, 15, tzinfo=UTC)
    assert "приглашает вас" in item.text and any("vacancy/555" in link for link in item.links)
    assert vacancy_ids(*item.links, item.text) == ["555"]
    assert snippet(item.text) == "Компания Рога и копыта приглашает вас."
    # html only
    msg2 = EmailMessage()
    msg2["Subject"] = "x"
    msg2.set_content("<p>Только <i>html</i></p>", subtype="html")
    assert parse_message(bytes(msg2), uid=1).text == "Только html"


def _app(hh_id: str, name: str, employer: str) -> tuple[SimpleNamespace, SimpleNamespace]:
    return SimpleNamespace(id=int(hh_id), vacancy_id=int(hh_id)), SimpleNamespace(
        hh_id=hh_id, name=name, employer_name=employer
    )


def test_match_application() -> None:
    apps = [_app("1", "Python developer", "Рога"), _app("2", "QA-инженер", "Копыта"), _app("3", "QA-инженер", "Альфа")]
    by_link = MailItem(1, "m1", "Приглашение", "hh", None, "текст", ["https://hh.ru/vacancy/2?x=1"])
    assert match_application(by_link, apps).id == 2  # type: ignore[union-attr]
    by_name = MailItem(2, "m2", "Отклик на «Python developer» просмотрен", "hh", None, "", [])
    assert match_application(by_name, apps).id == 1  # type: ignore[union-attr]
    ambiguous = MailItem(3, "m3", "QA-инженер", "hh", None, "Компания Альфа пригласила вас", [])
    assert match_application(ambiguous, apps).id == 3  # type: ignore[union-attr]
    assert match_application(MailItem(4, "m4", "QA-инженер", "hh", None, "", []), apps) is None  # two employers
    assert match_application(MailItem(5, "m5", "Что-то ещё", "hh", None, "", []), apps) is None


NOW = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def _v(i: int, status: str, prescore: int | None, responses: int | None = None) -> V:
    return V(
        id=i,
        hh_id=str(i),
        name=f"V{i}",
        url=None,
        employer_id="e",
        employer_name="E",
        accredited=None,
        trusted=None,
        area=None,
        salary=None,
        experience=None,
        formats=[],
        published_at=NOW - timedelta(hours=1),
        first_seen_at=NOW,
        archived=False,
        direct=False,
        has_test=False,
        letter_required=False,
        key_skills=[],
        responses=responses,
        status=status,
        prescore=prescore,
        score=None,
        set_id=None,
        search_ids=[],
        hidden=False,
        tags={},
    )


def test_borderline_report() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]  # prescore ≥ 35, maybe ≥ 50
    vs = [
        _v(1, "not_suitable", 32, responses=10),  # 3 under the prescore threshold, never evaluated → borderline
        _v(2, "not_suitable", 34, responses=200),  # closer to the threshold but crowded
        _v(3, "not_suitable", 20),  # too far
        _v(4, "filtered_out", 33),  # dropped by the rules — not a candidate
        _v(5, "not_suitable", 40),  # evaluated 47 → borderline by score
        _v(6, "not_suitable", 40),  # evaluated 30 → no
        _v(7, "sent", 33),  # already applied
    ]
    evals = {
        5: E(5, 47, "skip", "почти", [], ["нет опыта"], [], None, None, {}),
        6: E(6, 30, "skip", "", [], [], [], None, None, {}),
    }
    base = Base(vs, evals, [], {}, {}, [], [], None, set(), ZoneInfo("Europe/Moscow"), NOW)
    r = borderline_report(base, settings)
    assert r["prescore_min"] == 35 and r["score_min"] == 50 and r["total"] == 3
    assert [(i["id"], i["kind"], i["value"]) for i in r["items"]] == [
        (2, "prescore", 34),
        (1, "prescore", 32),
        (5, "score", 47),
    ]
    assert r["items"][2]["summary"] == "почти" and r["items"][2]["gaps"] == ["нет опыта"]
    assert borderline_report(base, settings, prescore_margin=1, score_margin=1)["total"] == 1
