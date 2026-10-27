"""Telegram channel preview parsing, channel input parsing and the vacancy heuristic (services/channels.py).

`page_html` builds a page in the markup t.me/s/<channel> really serves: message
wrappers with `data-post`, the text div `js-message_text`, `<time datetime>` and the `tme_messages_more` cursor.
"""

from __future__ import annotations

from app.services.channels import (
    ChannelError,
    detect_language,
    folder_slug,
    hh_vacancy_urls,
    is_vacancy_post,
    looks_like_vacancy,
    parse_channel_input,
    parse_keywords,
    parse_preview,
    post_title,
    split_inputs,
)

VACANCY_HTML = (
    "🔥 <b>Senior Python Developer</b> в Рога и Копыта<br/><br/>"
    "Обязанности: делать хорошо. Требования: Python, FastAPI. Удалённо, вилка 300–400k ₽.<br/>"
    'Откликнуться: <a href="https://hh.ru/vacancy/555?from=share" target="_blank" rel="noopener">hh.ru</a> '
    'или <a href="https://t.me/hr_anna">@hr_anna</a> '
    '<a href="?q=%23%D0%B2%D0%B0%D0%BA%D0%B0%D0%BD%D1%81%D0%B8%D1%8F">#вакансия</a> '
    '<a href="tg://resolve?domain=hr_anna">tg</a>'
)
RESUME_HTML = (
    "#резюме Python-разработчик, 5 лет опыта. Ищу работу, рассмотрю предложения удалённо.<br/>"
    "Стек: Python, Django, PostgreSQL. Писать в лс."
)
SHORT_HTML = "Всем привет! Канал снова работает."


def page_html(
    posts: list[tuple[int, str, str, str]],
    *,
    channel: str = "test_jobs",
    title: str = "Test Jobs",
    before: int | None = None,
) -> str:
    """posts: (message_id, datetime, inner html, kind) where kind is text | service | photo | reply."""
    blocks = []
    for message_id, dt, inner, kind in posts:
        classes = "tgme_widget_message text_not_supported_wrap js-widget_message"
        if kind == "service":
            classes += " service_message"
        body = ""
        if kind == "reply":
            body += (
                '<a class="tgme_widget_message_reply" href="https://t.me/x/1">'
                '<div class="tgme_widget_message_text js-message_reply_text" dir="auto">Цитата: вакансия Java</div></a>'
            )
        if kind == "photo":
            body += '<a class="tgme_widget_message_photo_wrap" href="https://t.me/x/2"></a>'
        elif kind == "service":
            body += f'<div class="tgme_widget_message_text js-message_text" dir="auto">{inner}</div>'
        else:
            body += f'<div class="tgme_widget_message_text js-message_text" dir="auto">{inner}</div>'
        blocks.append(
            f'<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="{classes}" '
            f'data-post="{channel}/{message_id}" data-view="x">'
            f'<div class="tgme_widget_message_bubble">{body}'
            '<div class="tgme_widget_message_footer compact js-message_footer"><div class="tgme_widget_message_info short">'
            f'<span class="tgme_widget_message_meta"><a class="tgme_widget_message_date" href="https://t.me/{channel}/{message_id}">'
            f'<time datetime="{dt}" class="time">12:00</time></a></span></div></div></div></div></div>'
        )
    more = (
        f'<a class="tme_messages_more js-messages_more" href="/s/{channel}?before={before}" data-before="{before}"></a>'
        if before
        else ""
    )
    return (
        "<!DOCTYPE html><html><head><title>Telegram</title></head><body>"
        '<div class="tgme_channel_info_header_title"><span dir="auto">' + title + "</span></div>"
        '<section class="tgme_channel_history js-message_history">'
        + more
        + "".join(blocks)
        + "</section></body></html>"
    )


def sample_page() -> str:
    return page_html(
        [
            (101, "2026-09-15T08:00:00+00:00", VACANCY_HTML, "text"),
            (102, "2026-09-15T08:05:00+00:00", "Test Jobs pinned «Правила канала»", "service"),
            (103, "2026-09-15T08:10:00+00:00", RESUME_HTML, "reply"),
            (104, "2026-09-15T08:15:00+00:00", "", "photo"),
            (105, "2026-09-15T08:20:00+00:00", SHORT_HTML, "text"),
        ],
        before=101,
    )


def test_parse_channel_input() -> None:
    for raw in (
        "@Test_Jobs",
        "test_jobs",
        "https://t.me/test_jobs",
        "t.me/s/test_jobs",
        "https://t.me/test_jobs/123?x=1",
    ):
        assert parse_channel_input(raw) == "test_jobs", raw
    for bad in (
        "",
        "@",
        "ab",
        "https://example.com/x",
        "имя",
        "a b",
        "+AbCdEf",
        "https://t.me/addlist/Ab_cD12EfGh3IjK4",
    ):
        try:
            parse_channel_input(bad)
        except ChannelError as exc:
            if "addlist" in bad:
                assert "папку" in str(exc) and "списком" in str(exc)
            continue
        raise AssertionError(bad)
    assert folder_slug("https://t.me/addlist/Ab_cD12EfGh3IjK4") == "Ab_cD12EfGh3IjK4"
    assert folder_slug("t.me/addlist/abc-1_2") == "abc-1_2" and folder_slug("https://t.me/test_jobs") is None
    assert split_inputs("@a_jobs, https://t.me/b_jobs;\n  c_jobs\td_jobs ") == [
        "@a_jobs",
        "https://t.me/b_jobs",
        "c_jobs",
        "d_jobs",
    ]


def test_parse_preview_posts_links_and_cursor() -> None:
    page = parse_preview(sample_page())
    assert page.title == "Test Jobs" and page.before == 101
    assert [p.message_id for p in page.posts] == [101, 103, 105]  # service and photo-only posts are skipped
    vac = page.posts[0]
    assert vac.posted_at is not None and vac.posted_at.isoformat() == "2026-09-15T08:00:00+00:00"
    assert vac.text.startswith("🔥 Senior Python Developer в Рога и Копыта")
    assert "Обязанности" in vac.text and "\n" in vac.text
    assert vac.links == ["https://hh.ru/vacancy/555?from=share", "https://t.me/hr_anna"]  # no hashtag/tg:// links
    assert '<a href="https://hh.ru/vacancy/555?from=share" rel="noopener noreferrer nofollow">' in vac.html
    assert "?q=" not in vac.html and "tg://" not in vac.html and "#вакансия" in vac.html
    resume = page.posts[1]
    assert resume.text.startswith("#резюме") and "Цитата" not in resume.text  # the quoted reply is not the post
    assert hh_vacancy_urls(vac.links, vac.text) == ["https://hh.ru/vacancy/555"]
    assert post_title(vac.text) == "Senior Python Developer в Рога и Копыта"  # no emoji in titles
    assert post_title("\n\n— \nВакансия: DevOps\nтекст") == "Вакансия: DevOps"
    # a hashtag line is skipped, a short company line is joined with the position
    assert post_title("#senior #удаленка\n\nАльфа-Банк\nSenior python developer\nОпыт работы: 3–6 лет") == (
        "Альфа-Банк — Senior python developer"
    )
    assert post_title("Вакансия:\nPython dev\nусловия") == "Вакансия: Python dev"
    assert post_title("👤 Finance Expert\n📍 Abu Dhabi, UAE\n💼 Bybit") == "Finance Expert — Abu Dhabi, UAE"
    assert post_title("🔗 RD-ОБРАБОТЧИК (LATAM) 🔗\n🌎 ГЕО: Мексика") == "RD-ОБРАБОТЧИК (LATAM)"
    assert post_title("▶️ Media Buyer Nutra | FB 🇺🇦 #buyer\n⭐️ Работодатель Prime") == "Media Buyer Nutra | FB #buyer"
    assert post_title("1️⃣ Senior Go — ⚡️ Remote ✅") == "1 Senior Go — Remote"
    # job boards: «Company: … / Title: …» → the position first, the company after it
    assert post_title("Company: SoFi\nTitle: Senior Manager, Capital Markets\nLocation: CA - Remote\nTags: Senior") == (
        "Senior Manager, Capital Markets — SoFi"
    )
    assert post_title("Компания: Рога и копыта\nДолжность: Тестировщик\nГород: Москва") == "Тестировщик — Рога и копыта"
    assert post_title("Company: Binance\nLocation: Remote\nDate: 2026") == "Company: Binance"  # no role line: as before
    assert post_title("Вакансия: DevOps\nКомпания: X\nусловия") == "Вакансия: DevOps"  # the first line is the role


def test_vacancy_heuristic() -> None:
    page = parse_preview(sample_page())
    assert looks_like_vacancy(page.posts[0].text) is True
    assert looks_like_vacancy(page.posts[1].text) is False  # #резюме
    assert looks_like_vacancy(page.posts[2].text) is False  # too short
    assert looks_like_vacancy("Ищем middle Go-разработчика в продуктовую команду, удалённо, зп от 250к") is True
    assert looks_like_vacancy("Требуется аналитик данных. Обязанности: отчёты. Условия: офис в Москве.") is True
    assert looks_like_vacancy("Junior QA, зарплата 80 000 руб, офис, полная занятость, опыт от года") is True  # weak ×2
    assert looks_like_vacancy("#вакансия Python dev, open to work кандидатов не рассматриваем, пишите в лс") is True
    assert (
        looks_like_vacancy("Сегодня в 19:00 стрим про карьеру в IT. Приходите, будет интересно, ссылка ниже!") is False
    )
    assert looks_like_vacancy("Open to work: Senior Java, 8 лет, рассмотрю предложения, remote only") is False
    # affiliate/iGaming channels: employer boilerplate «если ты в поисках работы» must not
    # turn a vacancy into a résumé; Ukrainian posts; «WANTED»; an HR community announcement is not a vacancy
    assert looks_like_vacancy(
        "▶️ Media Buyer Nutra | FB #buyer #удаленка ⭐️ Работодатель Prime Team ‼ Требования • Маєш досвід у FB "
        "• Вмієш працювати зі спендом від $1к+ ✍️ Обязанности • Запускати кампанії. Если ты в поисках работы — пиши"
    )
    assert looks_like_vacancy(
        "⚡️ Financial Manager | Crypto ⚡️ Impulse Team — команда з 6+ роками досвіду. Зараз шукаємо Finance Manager, "
        "який візьме під контроль фінанси 💰 Ставка 🔥 Повний ремоут і гнучкий графік"
    )
    assert looks_like_vacancy("🔫WANTED: Media Buyer FB+Gambling. Заробляй без стелі, ставка + %, пиши в лс @hr")
    assert not looks_like_vacancy(
        "ArbiHunter x Easy Hunt 🎙 21 сентября 16:00 — открытый войс-чат для HR, рекрутеров, работодателей и "
        "руководителей команд. Сколько реально стоит ошибка найма? Обсудим зарплата ли решает."
    )
    assert not looks_like_vacancy(
        "MoreLogin — выгодный антидетект для арбитража 🔥 Надежная подмена отпечатков и защита от банов. Промокод "
        "CPAJOB15 — скидка 15% на первое пополнение, 50 профилей на 14 дней в подарок"
    )
    # «Seeking or Hiring: SEEKING» templates are résumés although they carry «hiring»; an interview with a CEO
    # about hiring («обсудил», «автор», «статье») is a column, while «интервью»/«кейс» inside a vacancy are fine
    assert not looks_like_vacancy(
        "Seeking or Hiring: 🔥SEEKING🔥\nMy Website + LinkedIn profile: x\nMy Experience (in bullets): 4 years of "
        "Web3 communications, content creation, community management; offering my services as a senior manager"
    )
    assert not looks_like_vacancy(
        "👨‍💻 Поговорим про найм. Автор обсудил с CEO партнерской сети ситуацию с наймом в iGaming и на примере BDM "
        "расписал, почему кандидаты на высокие должности получают отказы: оклад, офис, мы предлагаем senior специалист"
    )
    assert looks_like_vacancy(
        "SEO Team Lead / Senior в продуктовую iGaming-компанию. Обязанности: стратегия. Условия: удалённо, зп от 300к. "
        "Этапы: интервью с HR, кейс, оффер"
    )
    # crypto/web3 job boards post «title / location / company» or «Company: … Location: … Apply here» only
    assert looks_like_vacancy("👤 Finance Expert\n📍 Abu Dhabi, UAE\n💼 Bybit")
    assert looks_like_vacancy("👤 Senior Data Analyst - Growth/Marketing\n📍 Kuala Lumpur, Malaysia\n💼 Bybit")
    assert looks_like_vacancy(
        "Company: Binance Title: Product Manager Location: Asia, Hong Kong Date Posted: 2026-09-15 Tags: Product, Crypto "
        "View here: https://jobs.example.com/jobs/binance-looking-for-a-product-manager"
    )
    assert looks_like_vacancy(
        "💼 Principal Engineer — 🏛️ at Paradex. Applications close in 9 days ✅ Apply → https://cjl.ist/x"
    )
    # a blog is not a job board: «рынок ищет причину», prices in $ and «ставка» are not vacancy signals
    assert not looks_like_vacancy(
        "🪩 В ФИНТЕХЕ ОХВАТ НЕ РАВЕН СПРОСУ. В финтехе редко кто покупает с первой формы. Человек полгода сравнивает "
        "решения и ищет причину не принимать решение. SEO здесь работает на доверие, а не на клики за $"
    )
    assert not looks_like_vacancy(
        "💰 ONCHAIN-БАНК: РИТЕЙЛ МЕНЬШЕ ЗАРАБАТЫВАЕТ. Я долго смотрел на разницу ставок: ставка по USDT 12% против 4% "
        "в долларах ($). Ставка это не касса, это цена риска, которую платит каждый."
    )
    # channel settings: keywords narrow, mode all widens
    text = page.posts[2].text
    assert is_vacancy_post(text, mode="all", keywords=[]) is True
    assert is_vacancy_post(text, mode="all", keywords=["python"]) is False
    assert is_vacancy_post(page.posts[0].text, mode="vacancies", keywords=["python", "go"]) is True
    assert is_vacancy_post(page.posts[0].text, mode="vacancies", keywords=["java"]) is False
    assert parse_keywords(" python, Go;\nLLM ,,") == ["python", "Go", "LLM"]


def test_language_detection_and_filter() -> None:
    ru = "Ищем Senior Python Developer: FastAPI, Kafka, PostgreSQL, Docker, Kubernetes — удалёнка, зарплата 300k ₽"
    en = "Senior Python Developer (remote, EU). Requirements: 5+ years, FastAPI, Kafka. Salary $6k. Apply: hr@x.com"
    uk = "Шукаємо Media Buyer (FB). Вимоги: досвід від 1 року, знання ЦА. Умови: ставка + %, повний ремоут"
    mixed = "▶️ Media Buyer Nutra | FB ⭐️ Работодатель Prime Team ‼ Требования • Маєш досвід у FB • Вмієш працювати зі спендом"
    assert detect_language(ru) == "ru" and detect_language(en) == "en"
    assert detect_language(uk) == "uk" and detect_language(mixed) == "uk"
    assert detect_language("🔥 12345") == "other"
    assert detect_language("Київстар ищет менеджера: обязанности, зарплата, офис в Москве") == "ru"  # a name with і
    # the language filter is applied before mode and keywords
    assert is_vacancy_post(uk, mode="all", keywords=[], languages={"ru", "en"}) is False
    assert is_vacancy_post(uk, mode="vacancies", keywords=[], languages=set()) is True
    assert is_vacancy_post(uk, mode="vacancies", keywords=[], languages={"uk"}) is True
    assert is_vacancy_post(ru, mode="vacancies", keywords=[], languages={"ru", "en"}) is True
    assert is_vacancy_post(en, mode="vacancies", keywords=[], languages={"ru"}) is False
