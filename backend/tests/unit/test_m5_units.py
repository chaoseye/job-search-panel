"""M5 unit tests: negotiation/message normalization, notification texts (snapshots), job failure hook."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.hh.negotiation import normalize_chat_messages, normalize_negotiation, normalize_negotiation_messages
from app.services.notify import EVENT_TYPES, render_event, snippet, ui_summary
from app.services.telegram import TelegramError, _apply_failure

NEGOTIATION_ITEM = {
    "id": "123456789",
    "state": {"id": "invitation", "name": "Приглашение"},
    "hidden": False,
    "created_at": "2026-09-01T10:24:48+0300",
    "updated_at": "2026-09-03T12:00:00+0300",
    "url": "https://api.hh.ru/negotiations/123456789",
    "has_updates": True,
    "viewed_by_opponent": True,
    "messaging_status": "ok",
    "chat_id": 3490,
    "counters": {"messages": 3, "unread_messages": 1},
    "decline_allowed": True,
    "source": "NEGOTIATION",
    "resume": {"id": "0123456789abcdef", "title": "Python developer"},
    "vacancy": {
        "id": "555001",
        "name": "Senior Python Developer",
        "employer": {"id": "1455", "name": "Рога и копыта"},
        "alternate_url": "https://hh.ru/vacancy/555001",
    },
}


def test_normalize_negotiation() -> None:
    d = normalize_negotiation(NEGOTIATION_ITEM)
    assert d.hh_id == "123456789" and d.state_id == "invitation" and d.state_name == "Приглашение"
    assert d.viewed_by_opponent and d.has_updates and d.chat_id == "3490"
    assert d.unread_count == 1 and d.messages_count == 3
    assert d.created_at == datetime(2026, 9, 1, 7, 24, 48, tzinfo=UTC)
    assert d.resume_hh_id == "0123456789abcdef"
    assert d.vacancy_hh_id == "555001" and d.vacancy_name == "Senior Python Developer"
    assert d.employer_name == "Рога и копыта" and d.vacancy_raw is not None
    # tolerant: missing/odd fields
    d2 = normalize_negotiation({"id": 5, "state": {"id": "HIDDEN"}, "vacancy": None, "counters": "x"})
    assert d2.hh_id == "5" and d2.state_id == "hidden" and d2.hidden and d2.vacancy_hh_id is None
    assert d2.unread_count == 0 and d2.chat_id is None


def test_normalize_messages_both_formats() -> None:
    neg = normalize_negotiation_messages(
        {
            "items": [
                {
                    "id": "1",
                    "text": "Здравствуйте!",
                    "created_at": "2026-09-02T09:00:00+0300",
                    "author": {"participant_type": "employer"},
                    "viewed_by_me": False,
                    "viewed_by_opponent": True,
                },
                {"id": "2", "text": None, "author": {"participant_type": "applicant"}},
                {"text": "no id"},
            ]
        }
    )
    assert [m.hh_message_id for m in neg] == ["1", "2"]
    assert neg[0].author_type == "employer" and neg[0].text == "Здравствуйте!" and neg[0].viewed_by_me is False
    assert neg[1].author_type == "applicant" and neg[1].text is None and neg[0].source == "negotiation"

    chat = normalize_chat_messages(
        {
            "messages": [
                {
                    "id": "10",
                    "type": "SIMPLE",
                    "creation_time": "2026-09-02T09:00:00+0300",
                    "payload": {"text": "Приходите на интервью", "attachments": None},
                    "sender_display_info": {"role": "EMPLOYER", "name": "HR Анна", "is_current_participant": False},
                    "viewed_by_opponent": False,
                },
                {
                    "id": "11",
                    "type": "SIMPLE",
                    "payload": {"text": None, "attachments": [{"title": "offer.pdf"}]},
                    "sender_display_info": {"role": "APPLICANT", "is_current_participant": True},
                },
                {"id": "12", "type": "PARTICIPANT_JOINED", "payload": {}},
            ]
        }
    )
    assert [m.hh_message_id for m in chat] == ["10", "11"]
    assert chat[0].author_type == "employer" and chat[0].author_name == "HR Анна" and chat[0].source == "chat"
    assert chat[1].author_type == "applicant" and chat[1].text == "[вложение] offer.pdf"


# ---- notification texts ------------------------------------------

VAC = {
    "name": "Senior Python <Dev>",
    "employer": "Рога & копыта",
    "url": "https://hh.ru/vacancy/1",
    "negotiation_id": "777",
}


def test_render_every_event_type_has_a_text() -> None:
    payloads = {
        "digest": {"suitable": 2, "maybe": 1, "top": [{**VAC, "score": 88, "salary": 250000}]},
        "batch_ready": {"batch_id": "20260911_2000_ab12", "tasks": 12, "batches": 1},
        "batch_imported": {"evaluations": 12, "letters": 11, "suitable": 4, "errors": 1},
        "applied": {**VAC, "mode": "api", "mode_label": "API"},
        "invitation": {**VAC, "text": "Добрый день! Приглашаем на собеседование " + "х" * 300},
        "discard": VAC,
        "message": {**VAC, "author": "HR Анна", "text": "Когда вам удобно созвониться?"},
        "captcha": {"url": "https://hh.ru/captcha?x=1"},
        "auth": {"reason": "token_revoked"},
        "limit": {"until": "2026-09-12T00:05:00+03:00"},
        "job_error": {"job": "scan", "error": "HHTransportError: timeout", "failures": 3},
        "studio_reply": {"name": "Пиксель", "subject": "Re: разработка сайтов", "site": "https://pixel.example"},
        "ws_tender": {"title": "Сайт для отеля", "url": "https://workspace.ru/tenders/sayt-1/", "budget": "до 200 000"},
        "login": {"at": "2026-10-23T11:05:00+00:00", "ip": "203.0.113.5", "browser": "Chrome 141, Windows"},
        "daily": {
            "date": "2026-09-11",
            "found": 40,
            "suitable": 6,
            "sent": 5,
            "viewed": 3,
            "invitations": 1,
            "awaiting": 2,
            "no_answer": 7,
        },
        "capability": {
            "changes": [{"key": "negotiations_list", "old": "unavailable", "new": "available"}],
            "apply_mode": "api",
            "prev_mode": "assist",
        },
        "resume_bump": {"url": "https://hh.ru/resume/abc", "slot": "08:45", "next": "сегодня в 13:00"},
        "channel_post": {
            "channel": "IT Jobs",
            "username": "it_jobs_ru",
            "title": "Senior Python Developer",
            "url": "https://t.me/it_jobs_ru/100",
            "hh_url": "https://hh.ru/vacancy/1",
            "snippet": "Удалённо, 300k",
        },
        "search_new": {
            "vacancy_id": 5,
            "hh_id": "9",
            "title": "Тестировщик ПО",
            "employer": "Рога и копыта",
            "area": "Москва",
            "salary_from": 90000,
            "salary_to": None,
            "currency": "RUR",
            "url": "https://hh.ru/vacancy/9",
            "search": "QA: тестировщик",
            "search_id": 3,
        },
        "letter": {
            **VAC,
            "apply_url": "https://hh.ru/applicant/vacancy_response?vacancyId=1",
            "score": 85,
            "status": "suitable",
            "salary": 250000,
            "work_format": "Удалённо",
            "area": "Москва",
            "summary": "Отличное совпадение",
            "letter": "Здравствуйте! Меня заинтересовала ваша вакансия <b>x</b>.",
            "version": 1,
            "needs_clarification": False,
            "revision": False,
        },
        "site_vacancy": {
            "site": "Авито",
            "company": "Авито",
            "title": "ML-инженер",
            "url": "https://career.avito.com/vacancies/x/103/",
            "location": "",
            "remote": True,
        },
        "hot": {
            "total": 1,
            "max_responses": 50,
            "max_age_days": 3,
            "min_score": 50,
            "items": [
                {
                    "name": "AI",
                    "employer": "E",
                    "url": "https://hh.ru/vacancy/1",
                    "score": 71,
                    "responses": 3,
                    "age_hours": 5,
                }
            ],
        },
    }
    for event in EVENT_TYPES:
        text = render_event(event, [payloads[event]], timezone="Europe/Moscow")
        assert text and "<b>" in text, event
        assert "bot" not in text.lower() or event == "captcha"  # no tokens or bot internals leak


def test_render_snapshots() -> None:
    assert (
        render_event("applied", [{**VAC, "mode": "api", "mode_label": "API"}])
        == '📨 <b>Отклик отправлен:</b> <a href="https://hh.ru/vacancy/1">Senior Python &lt;Dev&gt;</a> — Рога &amp; копыта (API)'
    )
    inv = render_event("invitation", [{**VAC, "text": "Добрый день! " + "х" * 300}])
    assert inv.startswith("🎉 <b>Приглашение:</b> <a href=")
    assert "…»" in inv and len(inv) < 600  # 200-char snippet
    assert '<a href="https://hh.ru/applicant/negotiations/item?id=777">Открыть чат</a>' in inv
    assert (
        render_event("discard", [VAC])
        == '❌ <b>Отказ:</b> <a href="https://hh.ru/vacancy/1">Senior Python &lt;Dev&gt;</a> — Рога &amp; копыта'
    )
    assert render_event("limit", [{"until": "2026-09-12T00:05:00+03:00"}]) == (
        "⛔ <b>Лимит откликов hh.ru исчерпан.</b> Отправка приостановлена до 12.09 00:05."
    )
    assert render_event("captcha", [{"url": "https://hh.ru/c?x=1&y=2"}]) == (
        '⚠️ <b>hh.ru требует капчу.</b> Фоновые задачи на паузе.\n<a href="https://hh.ru/c?x=1&amp;y=2">Пройти капчу</a>'
    )
    assert render_event(
        "daily",
        [
            {
                "date": "2026-09-11",
                "found": 40,
                "suitable": 6,
                "sent": 5,
                "viewed": 3,
                "invitations": 1,
                "awaiting": 2,
                "no_answer": 7,
            }
        ],
    ) == (
        "📊 <b>Сводка за 2026-09-11</b>\nнайдено 40 · подходящих 6 · отправлено 5 · просмотрено 3 · приглашений 1 · "
        "ждут разбора 2 · без ответа >7 дней 7"
    )
    assert render_event("batch_ready", [{"batch_id": "b1", "tasks": 12}]) == (
        "📦 <b>Ждут разбора:</b> 12 задач в 1 пакете (b1) — запустите <code>/hh-batch</code> в Claude Code"
    )
    assert render_event("batch_imported", [{"evaluations": 12, "letters": 11, "suitable": 4}]) == (
        "✅ <b>Разбор импортирован:</b> 12 оценок, 11 писем готовы; подходят: 4"
    )
    assert render_event("auth", [{"reason": "token_revoked"}]).startswith("🔑 <b>Токен hh.ru истёк или отозван.</b>")
    assert render_event("job_error", [{"job": "scan", "error": "boom"}]) == (
        "🛑 <b>Сбой задачи</b>\n<b>scan</b>: 3 ошибки подряд. Последняя: boom"
    )
    post = {
        "channel": "IT Jobs <RU>",
        "username": "it_jobs_ru",
        "title": "Senior Python Developer",
        "url": "https://t.me/it_jobs_ru/100",
        "hh_url": "https://hh.ru/vacancy/1",
        "snippet": "Удалённо, 300k",
    }
    assert render_event("channel_post", [post]) == (
        "📣 <b>Вакансия в Telegram-канале</b> IT Jobs &lt;RU&gt;\n"
        '<a href="https://t.me/it_jobs_ru/100">Senior Python Developer</a> — @it_jobs_ru · '
        '<a href="https://hh.ru/vacancy/1">hh.ru</a>\n«Удалённо, 300k»'
    )
    letter = render_event(
        "letter",
        [
            {
                **VAC,
                "apply_url": "https://hh.ru/applicant/vacancy_response?vacancyId=1",
                "score": 85,
                "status": "suitable",
                "salary": 250000,
                "work_format": "Удалённо",
                "area": "Москва",
                "metro": ["Тверская"],
                "summary": "Отличное совпадение",
                "letter": "Здравствуйте! Меня заинтересовала ваша вакансия <b>x</b>.",
                "version": 1,
                "needs_clarification": True,
            }
        ],
    )
    assert letter == (
        '✉️ <b>Подходит</b> — оценка 85: <a href="https://hh.ru/vacancy/1">Senior Python &lt;Dev&gt;</a> — Рога &amp; копыта\n'
        "250 000 ₽ · Удалённо · Москва, м. Тверская\n<i>Отличное совпадение</i>\n\n"
        "<b>Сопроводительное письмо</b> (версия 1) — нажмите на текст, чтобы скопировать:\n"
        "<pre>Здравствуйте! Меня заинтересовала ваша вакансия &lt;b&gt;x&lt;/b&gt;.</pre>\n\n"
        "⚠️ В письме есть пометки [[УТОЧНИТЬ]] — допишите их перед отправкой."
    )
    two = render_event(
        "channel_post", [post, {**post, "hh_url": None, "url": "https://t.me/it_jobs_ru/101", "title": "QA"}]
    )
    assert two.startswith("📣 <b>Новые вакансии в Telegram-каналах: 2</b>\n• <a href=")
    assert '• <a href="https://t.me/it_jobs_ru/101">QA</a> — @it_jobs_ru' in two
    # watch-only searches: links only, salary and city when known; chunk headers like channel posts
    watched = {
        "title": "Тестировщик ПО",
        "employer": "Рога и копыта",
        "area": "Москва",
        "salary_from": 90000,
        "salary_to": None,
        "currency": "RUR",
        "url": "https://hh.ru/vacancy/9",
        "search": "QA: тестировщик",
    }
    assert render_event("search_new", [watched]) == (
        "👀 <b>Новая вакансия по поиску «QA: тестировщик»</b>\n"
        '<a href="https://hh.ru/vacancy/9">Тестировщик ПО</a> — Рога и копыта · от 90 000 ₽ · Москва'
    )
    both = render_event(
        "search_new",
        [
            watched,
            {**watched, "title": "Инженер по тестированию", "salary_from": 83000, "salary_to": 100000, "area": None},
        ],
        part=(1, 2, 12),
    )
    assert both.startswith("👀 <b>Новые вакансии по поискам-наблюдениям: 12</b> (часть 1 из 2)\n• <a href=")
    assert "Инженер по тестированию</a> — Рога и копыта · 83 000 – 100 000 ₽" in both
    # the workplace: the nearest metro station when hh gives one, otherwise the street; nothing beyond the city
    # for remote vacancies
    placed = render_event("search_new", [{**watched, "metro": ["Китай-город", "Лубянка"], "address": "Маросейка, 2"}])
    assert placed.endswith(" — Рога и копыта · от 90 000 ₽ · Москва, м. Китай-город")
    street = render_event("search_new", [{**watched, "metro": [], "address": "улица Баумана, 10", "area": "Казань"}])
    assert street.endswith(" — Рога и копыта · от 90 000 ₽ · Казань, улица Баумана, 10")
    assert ui_summary("search_new", watched) == "Вакансия по поиску-наблюдению: Тестировщик ПО — Рога и копыта"


def test_render_merges_same_type_events() -> None:
    payloads = [{**VAC, "name": f"Vacancy {i}", "mode": "api", "mode_label": "API"} for i in range(12)]
    text = render_event("applied", payloads)
    assert text.startswith("📨 <b>Отправлено откликов: 12</b>")
    assert text.count("• ") == 10 and "… и ещё 2" in text
    digest = render_event(
        "digest",
        [
            {"suitable": 1, "maybe": 0, "top": [{"hh_id": "1", "name": "A", "score": 70}]},
            {
                "suitable": 2,
                "maybe": 1,
                "top": [{"hh_id": "2", "name": "B", "score": 95}, {"hh_id": "1", "name": "A", "score": 70}],
            },
        ],
    )
    assert digest.startswith("🆕 <b>Новые подходящие вакансии: 4</b> (подходят 3, возможно 1)")
    assert digest.index("1. B") < digest.index("2. A") and digest.count("A ") == 1  # sorted by score, deduplicated
    assert render_event("invitation", [VAC, {**VAC, "name": "Other"}]).startswith("🎉 <b>Приглашения: 2</b>")


def test_render_login_notice() -> None:
    """A sign-in through the public address: time in TIMEZONE, the browser and the address, escaped (the User-Agent
    is whatever the client said); several sign-ins in one message each get a line."""
    one = {"at": "2026-10-23T11:05:00+00:00", "ip": "203.0.113.5", "browser": "Chrome 141, Windows"}
    assert render_event("login", [one], timezone="Europe/Moscow") == (
        "🔐 <b>Вход в панель по ссылке</b>\n23.10 14:05 · Chrome 141, Windows · IP 203.0.113.5\n"
        "Не вы? Отправьте /logout: все входы закроются, ссылки отменятся."
    )
    sneaky = {**one, "browser": "<b>evil</b> & co"}
    two = render_event("login", [one, sneaky], timezone="Europe/Moscow")
    assert two.startswith("🔐 <b>Входы в панель по ссылке: 2</b>")
    assert "&lt;b&gt;evil&lt;/b&gt; &amp; co" in two and "<b>evil" not in two
    assert render_event("login", [{}]).startswith("🔐 <b>Вход в панель по ссылке</b>\nIP —")
    assert ui_summary("login", one) == "Вход в панель по ссылке: Chrome 141, Windows · IP 203.0.113.5"


def test_snippet_and_ui_summary() -> None:
    assert snippet("  a  b\n c ") == "a b c"
    assert snippet("x" * 250).endswith("…") and len(snippet("x" * 250)) == 200
    assert ui_summary("invitation", VAC) == "Приглашение: Senior Python <Dev> — Рога & копыта"
    assert ui_summary("digest", {"suitable": 2, "maybe": 1}) == "Дайджест новых подходящих: 3"


def test_telegram_failure_policy() -> None:
    from app.db.models import Notification

    now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    rows = [Notification(channel="telegram", event_type="applied", payload_json="{}", status="queued", attempts=0)]
    _apply_failure(
        rows, TelegramError("Telegram 429: Too Many Requests", status=429, retry_after=7, transient=True), now
    )
    assert rows[0].status == "queued" and rows[0].attempts == 1
    assert (rows[0].next_attempt_at - now).total_seconds() == 7
    _apply_failure(rows, TelegramError("Telegram 400: chat not found", status=400), now)
    assert rows[0].status == "failed" and "chat not found" in rows[0].error
    # a network failure never gives up by the attempt count: the delay grows to 10 min and repeats until the row is
    # older than STALE_AFTER (24 h, checked in deliver_pending) — a sleeping laptop does not lose its messages
    rows2 = [Notification(channel="telegram", event_type="x", payload_json="{}", status="queued", attempts=40)]
    _apply_failure(rows2, TelegramError("сеть: ConnectError", transient=True), now)
    assert rows2[0].status == "queued" and rows2[0].attempts == 41
    assert (rows2[0].next_attempt_at - now).total_seconds() == 600


def test_job_runner_notifies_on_third_consecutive_failure(settings) -> None:
    from app.runtime import Runtime

    async def _main() -> None:
        rt = await Runtime.create(settings)
        calls: list[tuple[str, str]] = []

        async def hook(job: str, error: str) -> None:
            calls.append((job, error))

        rt.jobs.on_repeated_failure = hook

        async def boom() -> dict:
            raise RuntimeError("boom")

        async def ok() -> dict:
            return {}

        try:
            for _ in range(3):
                await rt.jobs.run_inline("scan", boom)
            assert calls == [("scan", "RuntimeError: boom")]
            await rt.jobs.run_inline("scan", boom)  # 4th failure: no second notification
            assert len(calls) == 1
            await rt.jobs.run_inline("scan", ok)
            assert rt.jobs.failures["scan"] == 0
        finally:
            await rt.close()

    asyncio.run(_main())
