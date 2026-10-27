"""Made-up demo data for an empty base: `python -m app.cli demo-seed` (README, «Демо-данные»).

A candidate, a résumé and a questionnaire, saved searches, vacancies in every step of the pipeline with Claude's
evaluations and letters, applications with employers' answers, tenders of workspace.ru, studios with letters,
Telegram channels, the event feed. Everything is invented: companies, people, sites (the reserved `.example` domain),
the vacancy numbers (a range hh.ru has not reached). Nothing calls hh.ru, Telegram or Claude, and the panel started on
this base behaves as with real data, so the screens can be shown without anybody's personal data.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.db.models import (
    Application,
    AuditLog,
    ChannelPost,
    ClaudeBatch,
    CoverLetter,
    HHAuth,
    InterviewPrep,
    JobRun,
    Resume,
    SavedSearch,
    SelectionProfile,
    Studio,
    StudioLetter,
    TelegramChannel,
    Vacancy,
    VacancyEvaluation,
    WsTender,
    WsTenderReply,
)
from app.hh.vacancy import normalize_vacancy, storable_full
from app.services.audit import audit
from app.services.manual_negotiation import ensure_manual, record_update
from app.services.notify import emit_event
from app.services.preferences import LetterStyle, Preferences, SkillWeight, save_preferences
from app.services.profile import rebuild_profile, upload_resume_file
from app.services.scanner import apply_data_to_row
from app.services.state import get_system_state, set_setting

# vacancy numbers far ahead of the ones hh.ru gives out: a link of a demo card leads nowhere real
HH_ID_BASE = 990_000_100
EMPLOYER_ID_BASE = 99_000_000
SEED = 20261027


class DemoRefused(Exception):
    """The base already has data: the demo goes only into a new, empty base."""


RESUME_MD = """# Иван Петров

Python-разработчик (backend) · Москва · удалённо или гибрид

## Ключевые навыки

Python, FastAPI, Django, PostgreSQL, Redis, Celery, Kafka, Docker, REST API, pytest, Git, Linux

## Опыт работы

### Облачная касса «Кассета» — Senior Python-разработчик
Март 2022 — настоящее время

- Перевёл расчёт чеков на очередь Kafka: задержка обработки упала с 40 до 3 секунд.
- Собрал сервис отчётов на FastAPI и PostgreSQL, около 120 тысяч запросов в день.
- Настроил CI с тестами и линтерами, покрытие выросло с 35 до 80 %.

### Маркетплейс «Полка» — Python-разработчик
Июль 2019 — февраль 2022

- Писал backend каталога на Django: 2 млн товаров, полнотекстовый поиск на PostgreSQL.
- Вынес фоновые задачи в Celery и Redis, нагрузка на базу снизилась на треть.

## Образование

Технический университет, прикладная математика, 2019

## Языки

Русский — родной, английский — B2
"""

PREFERENCES = Preferences(
    target_titles=["Python-разработчик", "Backend-разработчик", "Data engineer"],
    target_keywords=["FastAPI", "Kafka", "PostgreSQL"],
    stop_words_title=["стажёр", "продаж", "PHP"],
    salary_min=220_000,
    work_formats=["REMOTE", "HYBRID"],
    areas=["Москва"],
    own_experience_years=6,
    no_agencies=True,
    wants="Продуктовая команда, backend на Python, понятные метрики и код-ревью.",
    avoids="Аутстафф, ночные смены, поддержка легаси без развития.",
    letter_style=LetterStyle(tone="деловой, живой", min_chars=600, max_chars=1100),
    skills_weights=[
        SkillWeight(name="Python", weight=5),
        SkillWeight(name="FastAPI", weight=5),
        SkillWeight(name="Django", weight=4),
        SkillWeight(name="PostgreSQL", weight=4),
        SkillWeight(name="Kafka", weight=4),
        SkillWeight(name="Docker", weight=3),
        SkillWeight(name="Redis", weight=3),
        SkillWeight(name="Celery", weight=3),
        SkillWeight(name="Go", weight=2),
    ],
)
DATA_SET = {
    "target_titles": ["Data engineer", "Аналитик данных"],
    "skills_weights": [
        {"name": "Python", "weight": 5},
        {"name": "SQL", "weight": 5},
        {"name": "Airflow", "weight": 4},
        {"name": "Kafka", "weight": 4},
        {"name": "ClickHouse", "weight": 4},
    ],
}

# name, industry for the description, hh area id and city
COMPANIES: list[tuple[str, str, str, str]] = [
    ("Kitebyte", "финтех: платёжный шлюз для малого бизнеса", "1", "Москва"),
    ("Датарека", "аналитическая платформа для ритейла", "1", "Москва"),
    ("Маркетсфера", "маркетплейс товаров для дома", "1", "Москва"),
    ("Учебный город", "онлайн-школа программирования", "88", "Казань"),
    ("Здравница Онлайн", "сервис записи к врачам", "1", "Москва"),
    ("Сигнал Телеком", "оператор связи и облачная телефония", "3", "Екатеринбург"),
    ("Тёплый квартал", "девелопер жилых комплексов", "1", "Москва"),
    ("Логос Логистик", "логистика и доставка последней мили", "4", "Новосибирск"),
    ("Пиксельная мануфактура", "студия мобильных игр", "1", "Москва"),
    ("Сфера HR", "сервис подбора персонала", "66", "Нижний Новгород"),
    ("Медиаполис", "медиагруппа и новостные сайты", "1", "Москва"),
    ("Квант и Ко", "заказная разработка для банков", "78", "Самара"),
    ("Стартап Нова", "SaaS для учёта в кофейнях", "1", "Москва"),
    ("Городские сервисы", "цифровые сервисы для жителей города", "88", "Казань"),
    ("Облачный дом", "хостинг и облачная инфраструктура", "1", "Москва"),
    ("Финполис", "инвестиционная платформа", "1", "Москва"),
]


@dataclass
class Role:
    title: str
    skills: list[str]
    duties: list[str]
    needs: list[str]
    salary: tuple[int | None, int | None]
    experience: str
    formats: list[str]


ROLES: list[Role] = [
    Role(
        "Senior Python-разработчик (FastAPI)",
        ["Python", "FastAPI", "PostgreSQL", "Kafka", "Docker"],
        ["развивать платёжный backend и его API", "переводить синхронные вызовы на события в Kafka", "ревьюить код"],
        ["4+ года коммерческой разработки на Python", "FastAPI или aiohttp", "PostgreSQL и индексы", "Kafka"],
        (330_000, 420_000),
        "between3And6",
        ["REMOTE"],
    ),
    Role(
        "Python-разработчик (Django)",
        ["Python", "Django", "PostgreSQL", "Celery", "Redis"],
        ["развивать каталог и корзину", "переносить тяжёлые задачи в Celery", "писать тесты"],
        ["3+ года на Django", "PostgreSQL", "Celery и Redis", "умение читать чужой код"],
        (250_000, 320_000),
        "between3And6",
        ["HYBRID"],
    ),
    Role(
        "Backend-разработчик Python",
        ["Python", "FastAPI", "PostgreSQL", "Docker", "REST API"],
        ["проектировать сервисы и схемы данных", "интегрироваться с внешними API", "дежурить по релизам"],
        ["Python 3.11+", "асинхронный код", "SQL", "Docker"],
        (280_000, 350_000),
        "between3And6",
        ["REMOTE", "HYBRID"],
    ),
    Role(
        "Data engineer",
        ["Python", "SQL", "Airflow", "Kafka", "ClickHouse"],
        ["строить пайплайны данных", "поддерживать витрины в ClickHouse", "настраивать DAG в Airflow"],
        ["Python и SQL", "Airflow", "Kafka", "опыт с ClickHouse будет плюсом"],
        (260_000, 340_000),
        "between3And6",
        ["REMOTE"],
    ),
    Role(
        "Ведущий backend-разработчик",
        ["Python", "Go", "PostgreSQL", "Kubernetes", "Kafka"],
        ["вести команду из трёх разработчиков", "отвечать за архитектуру сервисов", "растить младших"],
        ["6+ лет в разработке", "Python или Go", "Kubernetes", "опыт технического лидерства"],
        (400_000, 500_000),
        "moreThan6",
        ["REMOTE"],
    ),
    Role(
        "Python-разработчик интеграций",
        ["Python", "REST API", "PostgreSQL", "Docker", "RabbitMQ"],
        ["подключать партнёрские API", "разбирать инциденты обмена", "описывать контракты"],
        ["Python", "REST и вебхуки", "очереди сообщений", "аккуратность с данными"],
        (220_000, 280_000),
        "between1And3",
        ["REMOTE", "HYBRID"],
    ),
    Role(
        "Аналитик данных",
        ["SQL", "Python", "Power BI", "ClickHouse"],
        ["считать метрики продукта", "собирать дашборды", "проверять гипотезы"],
        ["SQL на уровне оконных функций", "Python для анализа", "BI-инструменты"],
        (180_000, 240_000),
        "between1And3",
        ["HYBRID"],
    ),
    Role(
        "QA-инженер (автотесты на Python)",
        ["Python", "pytest", "Playwright", "CI/CD"],
        ["писать автотесты API и интерфейса", "встраивать тесты в CI", "разбирать флаки"],
        ["pytest", "Playwright или Selenium", "понимание HTTP"],
        (170_000, 230_000),
        "between1And3",
        ["REMOTE"],
    ),
    Role(
        "DevOps-инженер",
        ["Linux", "Docker", "Kubernetes", "Terraform", "CI/CD"],
        ["поддерживать кластеры Kubernetes", "автоматизировать выкладки", "настраивать мониторинг"],
        ["Linux", "Kubernetes", "Terraform", "Prometheus и Grafana"],
        (280_000, 360_000),
        "between3And6",
        ["REMOTE", "HYBRID"],
    ),
    Role(
        "Frontend-разработчик (React)",
        ["JavaScript", "TypeScript", "React", "Next.js"],
        ["делать интерфейсы личного кабинета", "писать компоненты дизайн-системы"],
        ["React и TypeScript", "вёрстка без фреймворков", "тесты компонентов"],
        (220_000, 300_000),
        "between3And6",
        ["REMOTE"],
    ),
    Role(
        "Go-разработчик",
        ["Go", "PostgreSQL", "Kafka", "gRPC"],
        ["писать высоконагруженные сервисы", "оптимизировать запросы", "покрывать код тестами"],
        ["Go от двух лет", "gRPC", "PostgreSQL"],
        (300_000, 380_000),
        "between3And6",
        ["REMOTE"],
    ),
    Role(
        "Стажёр Python-разработчик",
        ["Python", "Git"],
        ["помогать команде с задачами бэклога", "учиться у наставника"],
        ["базовый Python", "желание учиться"],
        (60_000, 80_000),
        "noExperience",
        ["ON_SITE"],
    ),
    Role(
        "Менеджер по продажам IT-решений",
        ["Продажи", "CRM", "B2B"],
        ["вести сделки с корпоративными клиентами", "выполнять план продаж"],
        ["опыт B2B-продаж", "знание CRM"],
        (120_000, None),
        "between1And3",
        ["ON_SITE"],
    ),
    Role(
        "PHP-разработчик (Laravel)",
        ["PHP", "Laravel", "MySQL"],
        ["поддерживать монолит на Laravel", "переписывать модули"],
        ["PHP 8", "Laravel", "MySQL"],
        (200_000, 260_000),
        "between3And6",
        ["HYBRID"],
    ),
    Role(
        "Технический писатель",
        ["Markdown", "REST API", "Git"],
        ["описывать публичное API", "вести базу знаний для разработчиков"],
        ["опыт документации API", "Git", "внимание к деталям"],
        (150_000, 200_000),
        "between1And3",
        ["REMOTE"],
    ),
    Role(
        "Python-разработчик (ML-сервисы)",
        ["Python", "FastAPI", "PyTorch", "Docker"],
        ["оборачивать модели в сервисы", "следить за задержкой и качеством", "готовить данные для обучения"],
        ["Python", "FastAPI", "опыт выкладки моделей", "PyTorch будет плюсом"],
        (300_000, 400_000),
        "between3And6",
        ["REMOTE", "HYBRID"],
    ),
]

FORMAT_NAMES = {"REMOTE": "Удалённо", "HYBRID": "Гибрид", "ON_SITE": "На месте работодателя"}
EXPERIENCE_NAMES = {
    "noExperience": "Нет опыта",
    "between1And3": "От 1 года до 3 лет",
    "between3And6": "От 3 до 6 лет",
    "moreThan6": "Более 6 лет",
}

# Claude's letters (written for the demo candidate; [[УТОЧНИТЬ]] marks a fact the profile does not have)
LETTERS = [
    "Здравствуйте!\n\nУвидел, что вы переводите платёжный backend на события. Я делал похожую задачу в облачной "
    "кассе «Кассета»: перевёл расчёт чеков на очередь Kafka, и задержка обработки упала с 40 до 3 секунд.\n\n"
    "Там же собрал сервис отчётов на FastAPI и PostgreSQL, сейчас он отвечает примерно на 120 тысяч запросов в "
    "день. До этого три года писал backend каталога маркетплейса на Django.\n\nМне близко, что команда сама "
    "отвечает за сервис от схемы данных до мониторинга. Готов рассказать про очереди и про то, как мы держим "
    "покрытие тестами на 80 %.\n\nУдобно созвониться на этой неделе?",
    "Здравствуйте!\n\nКаталог на два миллиона товаров и поиск на PostgreSQL я писал сам, когда работал в "
    "маркетплейсе «Полка». Там же вынес фоновые задачи в Celery и Redis, и нагрузка на базу снизилась на треть.\n\n"
    "Сейчас в облачной кассе отвечаю за сервисы на FastAPI и Kafka, но Django знаю хорошо и с удовольствием "
    "вернусь к нему в продукте, где есть каталог и корзина.\n\nПодскажите, как у вас устроены релизы и "
    "код-ревью? Готов созвониться в любой день после обеда.",
    "Здравствуйте!\n\nВаш сервис работает с партнёрскими API, а интеграции у меня занимают большую часть работы: "
    "в «Кассете» я подключал банки и ОФД, разбирал инциденты обмена и описывал контракты.\n\nСервис отчётов на "
    "FastAPI и PostgreSQL, который я собрал, держит около 120 тысяч запросов в день, а CI с тестами поднял "
    "покрытие с 35 до 80 %.\n\nХотел бы узнать, какие интеграции сейчас самые болезненные. Удобно обсудить "
    "по видеосвязи?",
    "Здравствуйте!\n\nПайплайны на Kafka и PostgreSQL я строю каждый день: в облачной кассе перевёл расчёт чеков "
    "на очередь и сократил задержку с 40 до 3 секунд. С ClickHouse работал на отчётах, а Airflow использовал "
    "[[УТОЧНИТЬ: сколько лет работал с Airflow]].\n\nМне интересна роль data engineer как продолжение backend: "
    "те же очереди и схемы, но с упором на витрины.\n\nЕсли вам подходит такой опыт, давайте созвонимся.",
    "Здравствуйте!\n\nМодели в ваших сервисах уже обучены, и задача в том, чтобы они работали быстро и "
    "надёжно. Это как раз моя часть: я пишу сервисы на FastAPI, слежу за задержкой и держу код под тестами.\n\n"
    "В «Кассете» сократил задержку обработки с 40 до 3 секунд за счёт очереди Kafka, а сервис отчётов отвечает "
    "примерно на 120 тысяч запросов в день.\n\nС PyTorch работал [[УТОЧНИТЬ: на каких задачах]]. Готов "
    "рассказать подробнее на звонке.",
    "Здравствуйте!\n\nМеня зовут Иван, я backend-разработчик на Python с шестью годами опыта. Последние три года "
    "отвечаю за сервисы облачной кассы: Kafka, FastAPI, PostgreSQL, CI с тестами.\n\nВ вашей вакансии вижу "
    "задачи, которые уже решал: очереди, отчёты под нагрузкой и аккуратные миграции схемы.\n\nБуду рад "
    "обсудить, чем могу помочь команде.",
]

SUMMARIES: dict[str, tuple[str, list[str], list[str], list[str]]] = {
    "Senior Python-разработчик (FastAPI)": (
        "Платёжный backend на FastAPI и Kafka: стек совпадает почти полностью, вилка выше ожиданий.",
        ["Python", "FastAPI", "Kafka", "PostgreSQL"],
        ["нет опыта с Kubernetes"],
        [],
    ),
    "Python-разработчик (Django)": (
        "Каталог и корзина на Django, Celery и Redis: прямой опыт из маркетплейса, гибрид в Москве подходит.",
        ["Django", "Celery", "Redis", "PostgreSQL"],
        [],
        [],
    ),
    "Backend-разработчик Python": (
        "Сервисы на FastAPI с внешними API: совпадают и стек, и тип задач; дежурства по релизам стоит уточнить.",
        ["Python", "FastAPI", "Docker", "REST API"],
        ["дежурства по релизам"],
        [],
    ),
    "Data engineer": (
        "Пайплайны на Kafka и витрины в ClickHouse: опыт с очередями есть, Airflow в профиле не подтверждён.",
        ["Python", "SQL", "Kafka"],
        ["Airflow", "ClickHouse только на отчётах"],
        [],
    ),
    "Ведущий backend-разработчик": (
        "Ведущая роль с командой: технически подходит, но нет опыта руководства и Kubernetes.",
        ["Python", "PostgreSQL", "Kafka"],
        ["опыт руководства командой", "Kubernetes"],
        [],
    ),
    "Python-разработчик интеграций": (
        "Интеграции с партнёрскими API: прямой опыт из облачной кассы, зарплата на нижней границе ожиданий.",
        ["Python", "REST API", "PostgreSQL"],
        ["RabbitMQ"],
        [],
    ),
    "Аналитик данных": (
        "Роль аналитика: SQL и Python есть, но основная работа в BI, а это не профиль кандидата.",
        ["SQL", "Python"],
        ["Power BI", "продуктовая аналитика"],
        [],
    ),
    "QA-инженер (автотесты на Python)": (
        "Автотесты на pytest знакомы, но роль QA не совпадает с целевыми должностями.",
        ["pytest", "Python"],
        ["Playwright"],
        [],
    ),
    "DevOps-инженер": (
        "Инфраструктурная роль: Docker и Linux есть, Kubernetes и Terraform нет.",
        ["Docker", "Linux"],
        ["Kubernetes", "Terraform"],
        [],
    ),
    "Python-разработчик (ML-сервисы)": (
        "Сервисы вокруг моделей на FastAPI: backend-часть совпадает, опыт с PyTorch нужно уточнить.",
        ["Python", "FastAPI", "Docker"],
        ["PyTorch"],
        [],
    ),
    "Go-разработчик": (
        "Сервисы на Go: Go у кандидата на базовом уровне, остальное совпадает.",
        ["PostgreSQL", "Kafka"],
        ["Go от двух лет"],
        [],
    ),
    "Frontend-разработчик (React)": (
        "Frontend на React: не профиль кандидата.",
        [],
        ["React", "TypeScript"],
        [],
    ),
}

EMPLOYER_MESSAGES = {
    "invitation": "Здравствуйте, Иван! Спасибо за отклик. Приглашаем на техническое интервью: 40 минут, "
    "без live-coding. Удобно в четверг в 15:00?",
    "discard": "Спасибо за интерес к вакансии. Сейчас мы выбрали кандидата с опытом руководства командой.",
    "message": "Добрый день! Подскажите, пожалуйста, когда вы готовы выйти и какие ожидания по зарплате?",
}

TENDERS: list[dict[str, Any]] = [
    {
        "title": "Личный кабинет клиента для сервиса доставки",
        "category": "web-development",
        "service": "Разработка сайтов",
        "conditions": [["Тип сайта", "Веб-сервис"], ["CMS", "Без CMS"]],
        "budget": "до 600 000 рублей",
        "budget_max": 600_000,
        "city": "Новосибирск",
        "organizer": "Юридическое лицо",
        "participants": 4,
        "views": 63,
        "fit": 74,
        "status": "drafted",
        "description": "Нужен личный кабинет: история заказов, адреса, оплата картой, уведомления. Backend на Python "
        "или Go, интеграция с нашей учётной системой по REST. В отклике просим указать срок, стоимость "
        "и похожие проекты.",
    },
    {
        "title": "Интеграция CRM с сайтом и телефонией",
        "category": "crm",
        "service": "CRM, 1С, ПО, чат-боты",
        "conditions": [["Требуемая услуга", "Интеграция"]],
        "budget": "150 000 – 250 000 рублей",
        "budget_max": 250_000,
        "city": "Казань",
        "organizer": "Юридическое лицо",
        "participants": 7,
        "views": 91,
        "fit": 66,
        "status": "responded",
        "description": "Заявки с сайта и звонки должны попадать в CRM с источником и записью разговора. Сейчас "
        "часть заявок теряется. Нужны аудит текущей схемы и доработка.",
    },
    {
        "title": "Сервис онлайн-записи для сети клиник",
        "category": "web-development",
        "service": "Разработка сайтов",
        "conditions": [["Тип сайта", "Веб-сервис"]],
        "budget": "до 900 000 рублей",
        "budget_max": 900_000,
        "city": "Москва",
        "organizer": "Юридическое лицо",
        "participants": 2,
        "views": 28,
        "fit": 81,
        "status": "new",
        "description": "Запись к врачам с расписанием из МИС, напоминания пациентам, личный кабинет администратора. "
        "Важны нагрузка в утренние часы и аккуратная работа с персональными данными.",
    },
    {
        "title": "Мобильное приложение для фитнес-клуба",
        "category": "apps-development",
        "service": "Мобильные приложения",
        "conditions": [["Платформа", "iOS и Android"]],
        "budget": "по договорённости",
        "budget_max": None,
        "city": "Екатеринбург",
        "organizer": "Юридическое лицо",
        "participants": 11,
        "views": 140,
        "fit": 52,
        "status": "interesting",
        "description": "Расписание занятий, абонементы, оплата, push-уведомления. Backend можно на готовой "
        "платформе или своём API.",
    },
    {
        "title": "Парсер цен конкурентов с выгрузкой в Google Таблицы",
        "category": "crm",
        "service": "CRM, 1С, ПО, чат-боты",
        "conditions": [["Требуемая услуга", "Разработка ПО"]],
        "budget": "до 120 000 рублей",
        "budget_max": 120_000,
        "city": "Самара",
        "organizer": "Индивидуальный предприниматель",
        "participants": 9,
        "views": 77,
        "fit": 58,
        "status": "replied",
        "description": "Раз в сутки собирать цены с пяти сайтов конкурентов и складывать в таблицу с историей.",
    },
    {
        "title": "Доработка интернет-магазина на Django",
        "category": "web-development",
        "service": "Разработка сайтов",
        "conditions": [["CMS", "Django"]],
        "budget": "до 300 000 рублей",
        "budget_max": 300_000,
        "city": "Москва",
        "organizer": "Юридическое лицо",
        "participants": 3,
        "views": 41,
        "fit": 77,
        "status": "drafted",
        "description": "Перенести корзину на новую платёжную систему, ускорить каталог, покрыть оформление заказа "
        "тестами. Есть документация и доступ к старому разработчику.",
    },
    {
        "title": "Дизайн логотипа и фирменного стиля",
        "category": None,
        "service": "Дизайн и брендинг",
        "conditions": [],
        "budget": "до 80 000 рублей",
        "budget_max": 80_000,
        "city": "Нижний Новгород",
        "organizer": "Юридическое лицо",
        "participants": 15,
        "views": 210,
        "fit": 8,
        "status": "new",
        "relevant": False,
        "description": "Логотип, паттерны и гайдлайн для кофейни.",
    },
    {
        "title": "Корпоративный портал для сотрудников",
        "category": "web-development",
        "service": "Разработка сайтов",
        "conditions": [["Тип сайта", "Корпоративный портал"]],
        "budget": "до 1 500 000 рублей",
        "budget_max": 1_500_000,
        "city": "Москва",
        "organizer": "Юридическое лицо",
        "participants": 6,
        "views": 98,
        "fit": 63,
        "status": "declined",
        "description": "Новости, база знаний, заявки в отделы, вход через Active Directory.",
    },
    {
        "title": "Чат-бот для ответов на частые вопросы покупателей",
        "category": "crm",
        "service": "CRM, 1С, ПО, чат-боты",
        "conditions": [["Требуемая услуга", "Чат-бот"]],
        "budget": "до 200 000 рублей",
        "budget_max": 200_000,
        "city": "Казань",
        "organizer": "Юридическое лицо",
        "participants": 12,
        "views": 160,
        "fit": 61,
        "status": "new",
        "description": "Бот на сайте и в мессенджере: статус заказа, возвраты, передача оператору.",
    },
]
TENDER_REPLY = (
    "Здравствуйте!\n\nПонял задачу так: клиенту нужен личный кабинет с историей заказов и оплатой, а данные "
    "живут в вашей учётной системе.\n\n- Похожий проект: сервис отчётов на FastAPI и PostgreSQL, около 120 тысяч "
    "запросов в день.\n- Стек: Python, FastAPI, PostgreSQL, очереди для уведомлений.\n- Этапы: схема данных и "
    "API, интеграция с учётной системой, кабинет, нагрузочное тестирование.\n- Срок: [[УТОЧНИТЬ: срок]].\n"
    "- Стоимость: [[УТОЧНИТЬ: стоимость]].\n\nПодскажите, есть ли у учётной системы тестовый стенд?"
)

STUDIOS: list[dict[str, Any]] = [
    {
        "name": "Веб-студия Сайтоград",
        "domain": "sitograd.example",
        "kind": "dev",
        "source": "list",
        "letter_kind": "offer",
        "services": ["веб-разработка", "e-commerce"],
        "emails": ["hello@sitograd.example"],
        "fit": 72,
        "status": "drafted",
        "area": "Москва",
        "description": "Сайты и интернет-магазины под ключ, поддержка проектов на Django.",
    },
    {
        "name": "Пиксель Плюс",
        "domain": "pixel-plus.example",
        "kind": "mixed",
        "source": "hh_search",
        "letter_kind": "application",
        "services": ["веб-разработка", "дизайн и UX/UI"],
        "emails": ["team@pixel-plus.example"],
        "fit": 64,
        "status": "ready",
        "area": "Казань",
        "open_vacancies": 3,
        "description": "Веб-студия: сайты, личные кабинеты, мобильные приложения для клиентов из ритейла.",
    },
    {
        "name": "Северный код",
        "domain": "north-code.example",
        "kind": "dev",
        "source": "hh_vacancies",
        "letter_kind": "application",
        "services": ["веб-разработка", "CRM и интеграции"],
        "emails": ["jobs@north-code.example"],
        "fit": 69,
        "status": "sent",
        "area": "Екатеринбург",
        "open_vacancies": 2,
        "description": "Заказная разработка: интеграции, личные кабинеты, внутренние сервисы.",
    },
    {
        "name": "Digital-агентство «Маяк»",
        "domain": "mayak-digital.example",
        "kind": "mixed",
        "source": "channel",
        "letter_kind": "offer",
        "services": ["маркетинг и SEO", "веб-разработка"],
        "emails": ["info@mayak-digital.example"],
        "fit": 41,
        "status": "ready",
        "area": "Москва",
        "description": "Сайты и продвижение для локального бизнеса.",
    },
    {
        "name": "Студия разработки «Крона»",
        "domain": "krona-dev.example",
        "kind": "dev",
        "source": "hh_search",
        "letter_kind": "application",
        "services": ["продуктовая разработка", "веб-разработка"],
        "emails": ["hr@krona-dev.example"],
        "fit": 78,
        "status": "replied",
        "area": "Москва",
        "open_vacancies": 4,
        "description": "Продуктовая студия: MVP для стартапов на Python и React.",
    },
    {
        "name": "Дизайн-бюро «Линия»",
        "domain": "liniya-design.example",
        "kind": "design",
        "source": "list",
        "letter_kind": "offer",
        "services": ["дизайн и UX/UI", "брендинг"],
        "emails": [],
        "fit": 22,
        "status": "no_contacts",
        "area": "Нижний Новгород",
        "description": "Интерфейсы и брендинг для цифровых продуктов.",
    },
    {
        "name": "Студия Код",
        "domain": "kod-studio.example",
        "kind": "dev",
        "source": "list",
        "letter_kind": "offer",
        "services": ["веб-разработка", "AI и автоматизация"],
        "emails": ["hello@kod-studio.example"],
        "fit": 66,
        "status": "drafted",
        "area": "Новосибирск",
        "description": "Небольшая команда: сервисы на Python, чат-боты, автоматизация процессов.",
    },
    {
        "name": "Агентство «Фокус»",
        "domain": "focus-agency.example",
        "kind": "mixed",
        "source": "hh_search",
        "letter_kind": "application",
        "services": ["веб-разработка", "мобильные приложения"],
        "emails": ["career@focus-agency.example"],
        "fit": 57,
        "status": "ready",
        "area": "Самара",
        "open_vacancies": 1,
        "description": "Мобильные и веб-приложения для банков и страховых.",
    },
    {
        "name": "Студия «Облако»",
        "domain": "oblako-studio.example",
        "kind": "dev",
        "source": "list",
        "letter_kind": "offer",
        "services": ["CRM и интеграции"],
        "emails": [],
        "fit": 48,
        "status": "new",
        "area": "Москва",
        "description": "Внедрение CRM и интеграции с телефонией.",
    },
    {
        "name": "Продуктовая студия «Сота»",
        "domain": "sota-product.example",
        "kind": "mixed",
        "source": "hh_search",
        "letter_kind": "application",
        "services": ["продуктовая разработка", "дизайн и UX/UI"],
        "emails": ["hello@sota-product.example"],
        "fit": 70,
        "status": "ready",
        "area": "Казань",
        "open_vacancies": 2,
        "description": "Делаем SaaS-продукты для клиентов и свои сервисы для HR.",
    },
]
STUDIO_LETTERS = {
    "business": (
        "Python-разработчик для проектов {name}: в штат или на проект",
        "Здравствуйте!\n\nВидел, что {name} делает сайты и сервисы для клиентов, а backend у вас часто на "
        "Python. Я backend-разработчик с шестью годами опыта: в облачной кассе перевёл расчёт чеков на Kafka и "
        "сократил задержку с 40 до 3 секунд, а сервис отчётов на FastAPI держит около 120 тысяч запросов в "
        "день.\n\nМогу взять серверную часть проектов: API, интеграции, очереди и тесты. Подойдёт и работа в "
        "штате, и проектный формат.\n\nУдобно созвониться на 15 минут на этой неделе?\n\nЕсли сейчас неактуально, "
        "просто ответьте, и я больше не напишу.",
    ),
    "simple": (
        "Разработчик для {name}",
        "Здравствуйте!\n\nЯ пишу backend на Python: сервисы, интеграции, отчёты.\n\nНапример, ускорил обработку "
        "чеков в облачной кассе с 40 до 3 секунд.\n\nЕсли вам нужен такой человек в команду или на проект, "
        "давайте созвонимся?\n\nЕсли сейчас неактуально, просто ответьте, и я больше не напишу.",
    ),
}

CHANNELS = [
    ("demo_python_jobs", "Python Jobs · демо"),
    ("demo_remote_it", "Удалённая работа в IT · демо"),
    ("demo_data_jobs", "Data Jobs · демо"),
]
CHANNEL_POSTS = [
    (
        "demo_python_jobs",
        "Python-разработчик (FastAPI) в финтех",
        "Удалённо, от 300 000 ₽. FastAPI, PostgreSQL, Kafka. Команда из шести человек, релизы каждую неделю.",
    ),
    (
        "demo_python_jobs",
        "Backend-разработчик на Django",
        "Гибрид в Москве, 250–320 тыс. ₽. Каталог и заказы, Celery, Redis.",
    ),
    (
        "demo_remote_it",
        "Senior Python для B2B SaaS",
        "Полная удалёнка, оплата в рублях. Асинхронный Python, PostgreSQL, Docker.",
    ),
    ("demo_remote_it", "QA-инженер на автотесты", "pytest, Playwright, CI. Удалённо по России."),
    ("demo_data_jobs", "Data engineer в ритейл", "Airflow, ClickHouse, Kafka. Удалённо, до 340 000 ₽."),
    ("demo_data_jobs", "Аналитик данных в продуктовую команду", "SQL, Python, A/B-тесты. Гибрид, Казань."),
]


@dataclass
class Plan:
    """Where a demo vacancy ends up: its status and what the pipeline left on it."""

    status: str
    days_ago: float
    score: int | None = None
    prescore: int | None = None
    letter: int | None = None
    reason: str | None = None
    stage: str | None = None  # sent: none | viewed | invitation | discard | message
    sent_days_ago: float | None = None
    searches: str = ",1,"
    responses: int | None = None


def _iso_hh(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S+0000")


def _description(role: Role, company: tuple[str, str, str, str]) -> str:
    name, industry, _area, _city = company
    duties = "".join(f"<li>{d}</li>" for d in role.duties)
    needs = "".join(f"<li>{n}</li>" for n in role.needs)
    formats = ", ".join(FORMAT_NAMES[f].lower() for f in role.formats)
    return (
        f"<p><strong>{name}</strong> — {industry}.</p>"
        f"<p><strong>Чем предстоит заниматься:</strong></p><ul>{duties}</ul>"
        f"<p><strong>Что важно:</strong></p><ul>{needs}</ul>"
        f"<p><strong>Условия:</strong></p><ul><li>формат: {formats}</li><li>оформление по ТК РФ</li>"
        "<li>оплачиваемое обучение и конференции</li></ul>"
    )


def _hh_vacancy(
    index: int, role: Role, company: tuple[str, str, str, str], published: datetime, responses: int
) -> dict[str, Any]:
    """A vacancy in the shape hh.ru returns it, made up from the role and the company."""
    hh_id = str(HH_ID_BASE + index)
    name, _industry, area_id, city = company
    employer_id = str(EMPLOYER_ID_BASE + COMPANIES.index(company))
    low, high = role.salary
    salary = {"from": low, "to": high, "currency": "RUR", "gross": False} if low or high else None
    return {
        "id": hh_id,
        "name": role.title,
        "area": {"id": area_id, "name": city},
        "salary": salary,
        "salary_range": {**salary, "mode": {"id": "MONTH"}} if salary else None,
        "employer": {
            "id": employer_id,
            "name": name,
            "trusted": True,
            "accredited_it_employer": index % 3 != 0,
            "alternate_url": f"https://hh.ru/employer/{employer_id}",
        },
        "published_at": _iso_hh(published),
        "created_at": _iso_hh(published),
        "archived": False,
        "type": {"id": "open", "name": "Открытая"},
        "experience": {"id": role.experience, "name": EXPERIENCE_NAMES[role.experience]},
        "employment_form": {"id": "FULL", "name": "Полная"},
        "work_format": [{"id": f, "name": FORMAT_NAMES[f]} for f in role.formats],
        "schedule": {"id": "remote" if "REMOTE" in role.formats else "fullDay", "name": ""},
        "response_letter_required": index % 4 == 0,
        "has_test": index % 7 == 0,
        "test": {"required": False},
        "response_url": None,
        "alternate_url": f"https://hh.ru/vacancy/{hh_id}",
        "apply_alternate_url": f"https://hh.ru/applicant/vacancy_response?vacancyId={hh_id}",
        "key_skills": [{"name": s} for s in role.skills],
        "professional_roles": [{"id": "96", "name": "Программист, разработчик"}],
        "description": _description(role, company),
        "relations": [],
        "counters": {"responses": responses},
        "address": None,
    }


# role title → plans for the vacancies made from it (company picked in turn)
def _plans() -> list[tuple[str, Plan]]:
    plans: list[tuple[str, Plan]] = [
        # waiting for the user's decision: Claude's verdict and a letter
        ("Senior Python-разработчик (FastAPI)", Plan("letter_ready", 0.3, 88, 79, letter=0, responses=12)),
        ("Python-разработчик (Django)", Plan("letter_ready", 1.2, 84, 74, letter=1, responses=31)),
        ("Backend-разработчик Python", Plan("letter_ready", 2.0, 81, 72, letter=5, responses=44)),
        ("Python-разработчик интеграций", Plan("letter_ready", 3.5, 76, 66, letter=2, responses=58)),
        ("Data engineer", Plan("maybe", 1.0, 67, 61, letter=3, searches=",3,", responses=19)),
        ("Python-разработчик (ML-сервисы)", Plan("maybe", 2.4, 64, 63, letter=4, responses=26)),
        ("Senior Python-разработчик (FastAPI)", Plan("suitable", 4.0, 79, 70, letter=5, responses=73)),
        ("Ведущий backend-разработчик", Plan("maybe", 5.0, 58, 64, responses=96)),
        ("Go-разработчик", Plan("maybe", 6.0, 55, 52, searches=",4,", responses=40)),
        # Claude has not seen them yet
        ("Backend-разработчик Python", Plan("prescored", 0.1, None, 68, responses=3)),
        ("Python-разработчик (Django)", Plan("prescored", 0.2, None, 63, responses=5)),
        ("Data engineer", Plan("prescored", 0.4, None, 57, searches=",3,", responses=8)),
        ("Python-разработчик интеграций", Plan("prescored", 0.6, None, 55, responses=11)),
        # not for this candidate
        ("Аналитик данных", Plan("not_suitable", 7.0, 38, 44, searches=",3,", responses=120)),
        ("QA-инженер (автотесты на Python)", Plan("not_suitable", 8.0, 34, 41, responses=88)),
        ("DevOps-инженер", Plan("not_suitable", 9.0, 31, 39, responses=64)),
        ("Frontend-разработчик (React)", Plan("not_suitable", 12.0, None, 18, responses=150)),
        ("Стажёр Python-разработчик", Plan("filtered_out", 3.0, None, None, reason="стоп-слово в названии: стажёр")),
        (
            "Менеджер по продажам IT-решений",
            Plan("filtered_out", 5.0, None, None, reason="стоп-слово в названии: продаж"),
        ),
        ("PHP-разработчик (Laravel)", Plan("filtered_out", 10.0, None, None, reason="стоп-слово в названии: PHP")),
        ("Ведущий backend-разработчик", Plan("skipped", 11.0, 61, 66, reason="пропущена: нужен опыт руководства")),
        # a page opened on hh.ru in the assistant mode, waiting for the confirmation
        ("Python-разработчик (Django)", Plan("needs_manual", 1.5, 82, 71, letter=1, responses=36)),
        ("Backend-разработчик Python", Plan("needs_manual", 2.2, 77, 69, letter=5, responses=52)),
        # watch-only search: links in the bot, no evaluation
        ("Технический писатель", Plan("watched", 0.5, searches=",5,", responses=9)),
        ("Технический писатель", Plan("watched", 2.5, searches=",5,", responses=21)),
    ]
    # sent applications and what the employers did with them, spread over the last month
    sent = [
        ("Senior Python-разработчик (FastAPI)", 3.0, "invitation"),
        ("Python-разработчик (Django)", 4.0, "viewed"),
        ("Backend-разработчик Python", 5.0, "message"),
        ("Python-разработчик интеграций", 6.0, "discard"),
        ("Data engineer", 7.0, "none"),
        ("Python-разработчик (ML-сервисы)", 8.0, "viewed"),
        ("Senior Python-разработчик (FastAPI)", 10.0, "invitation"),
        ("Backend-разработчик Python", 12.0, "none"),
        ("Python-разработчик (Django)", 14.0, "discard"),
        ("Python-разработчик интеграций", 16.0, "viewed"),
        ("Go-разработчик", 18.0, "none"),
        ("Backend-разработчик Python", 20.0, "invitation"),
        ("Senior Python-разработчик (FastAPI)", 23.0, "discard"),
        ("Python-разработчик (Django)", 26.0, "viewed"),
        ("Data engineer", 29.0, "none"),
        ("Backend-разработчик Python", 33.0, "viewed"),
        ("Python-разработчик (ML-сервисы)", 38.0, "discard"),
        ("Python-разработчик интеграций", 45.0, "none"),
        ("Backend-разработчик Python", 0.2, "none"),
        ("Python-разработчик (Django)", 0.15, "none"),
    ]
    for n, (title, days, stage) in enumerate(sent):
        score = 70 + (n * 7) % 22
        letter = [0, 1, 5, 2, 3, 4][n % 6]
        plans.append(
            (
                title,
                Plan(
                    "sent",
                    days + 1.0,
                    score,
                    score - 8,
                    letter=letter,
                    stage=stage,
                    sent_days_ago=days,
                    searches=",3," if title == "Data engineer" else ",1,2," if n % 3 == 0 else ",1,",
                    responses=20 + (n * 13) % 140,
                ),
            )
        )
    # the history of the last two months: what was found, evaluated and left (for the trends and segments)
    history = [
        ("Python-разработчик (Django)", 72),
        ("Аналитик данных", 41),
        ("Backend-разработчик Python", 66),
        ("QA-инженер (автотесты на Python)", 36),
        ("Senior Python-разработчик (FastAPI)", 75),
        ("DevOps-инженер", 33),
        ("Python-разработчик интеграций", 61),
        ("Frontend-разработчик (React)", 18),
        ("Data engineer", 57),
        ("Go-разработчик", 52),
        ("Python-разработчик (ML-сервисы)", 63),
        ("Ведущий backend-разработчик", 58),
    ]
    for n in range(36):
        title, base = history[n % len(history)]
        days = 7.0 + n * 1.5
        score = base + (n * 7) % 9 - 4
        if score < 50:
            status, reason = "not_suitable", None
        elif score < 70 and n % 4 == 0:
            status, reason = "maybe", None
        elif score < 70:
            status, reason = "skipped", "пропущена: задачи не совпадают с целевыми"
        else:
            status, reason = "skipped", "пропущена: отклик на похожую вакансию уже отправлен"
        searches = ",3," if title in ("Data engineer", "Аналитик данных") else ",1," if n % 2 else ",1,2,"
        plans.append((title, Plan(status, days, score, score - 6, reason=reason, searches=searches)))
    return plans


async def _refuse_if_used(session: AsyncSession) -> None:
    used = []
    for model, label in ((Vacancy, "вакансии"), (Resume, "резюме"), (SavedSearch, "поиски"), (HHAuth, "вход в hh.ru")):
        if (await session.execute(select(func.count()).select_from(model))).scalar():
            used.append(label)
    if used:
        raise DemoRefused(
            "В базе уже есть данные (" + ", ".join(used) + "). Демо-данные кладутся только в новую базу: "
            "задайте отдельную папку, например DATA_DIR=./demo-data."
        )


async def seed_demo(session: AsyncSession, settings: Settings, now: datetime | None = None) -> dict[str, int]:
    """Fills an empty base with the demo. Returns what was added, by kind."""
    await _refuse_if_used(session)
    now = now or utcnow()
    rnd = random.Random(SEED)
    counts: dict[str, int] = {}

    # the candidate: a résumé file and the questionnaire → profile.md for Claude
    await upload_resume_file(session, settings, "resume-ivan-petrov.md", RESUME_MD.encode("utf-8"))
    data_set = SelectionProfile(name="Data-направление", criteria_json=json.dumps(DATA_SET, ensure_ascii=False))
    session.add(data_set)
    await session.flush()
    await save_preferences(session, PREFERENCES.model_copy(deep=True))
    await rebuild_profile(session, settings, force=True)

    # searches (ids 1–5 are referred to by the vacancies' source_search_ids)
    searches = [
        ("Python backend · удалённо", {"text": "python backend", "work_format": ["REMOTE"]}, True, False, None),
        ("FastAPI (по названию)", {"text": "fastapi", "search_field": ["name"]}, True, False, None),
        (
            "Data engineer · Москва",
            {"text": "data engineer", "areas": [{"id": "1", "name": "Москва"}]},
            True,
            False,
            data_set.id,
        ),
        ("Go-разработчик", {"text": "golang"}, False, False, None),
        ("Технический писатель", {"text": "технический писатель", "work_format": ["REMOTE"]}, True, True, None),
    ]
    for i, (name, params, enabled, watch, set_id) in enumerate(searches):
        session.add(
            SavedSearch(
                name=name,
                params_json=json.dumps(params, ensure_ascii=False),
                interval_min=30 if not watch else 120,
                enabled=enabled,
                watch_only=watch,
                selection_profile_id=set_id,
                last_run_at=now - timedelta(minutes=12 + i),
                last_seen_published_at=now - timedelta(minutes=40),
                last_found=[214, 37, 96, 58, 12][i],
                last_new=[3, 1, 2, 0, 1][i],
                last_requests=[3, 1, 2, 1, 1][i],
            )
        )
    await session.flush()
    counts["searches"] = len(searches)

    # Claude's packets the evaluations came from
    batch_ids = []
    for days in (1, 6, 15, 30):
        moment = now - timedelta(days=days)
        batch_id = f"{moment:%Y%m%d_%H%M}_{rnd.randrange(16**4):04x}"
        batch_ids.append(batch_id)
        session.add(
            ClaudeBatch(
                batch_id=batch_id,
                packet_path=str(settings.exchange_dir / "imported" / f"batch_{batch_id}.json"),
                result_path=str(settings.exchange_dir / "imported" / f"batch_{batch_id}.result.json"),
                status="imported",
                tasks_count=12,
                results_count=12,
                profile_version=1,
                imported_at=moment + timedelta(minutes=18),
                summary_json=json.dumps({"evaluations": 12, "letters": 9}),
                created_at=moment,
            )
        )

    roles = {r.title: r for r in ROLES}
    company_turn: dict[str, int] = {}
    vacancies: list[tuple[Vacancy, Plan]] = []
    for index, (title, plan) in enumerate(_plans()):
        role = roles[title]
        turn = company_turn.get(title, 0)
        company_turn[title] = turn + 1
        company = COMPANIES[(ROLES.index(role) * 3 + turn * 5) % len(COMPANIES)]
        first_seen = now - timedelta(days=plan.days_ago)
        published = first_seen - timedelta(hours=rnd.randint(1, 20))
        raw = _hh_vacancy(index, role, company, published, plan.responses or rnd.randint(5, 90))
        data = normalize_vacancy(raw, full=True)
        row = Vacancy(hh_id=data.hh_id, first_seen_at=first_seen, source_search_ids=plan.searches)
        apply_data_to_row(row, data, {})
        row.raw_full_json = json.dumps(storable_full(raw), ensure_ascii=False)
        row.full_fetched_at = first_seen
        row.last_seen_at = now - timedelta(minutes=12)
        row.pipeline_status = plan.status
        row.prescore = plan.prescore
        row.score = plan.score
        row.rules_passed = plan.status not in ("filtered_out", "watched")
        row.pipeline_reason = plan.reason
        if plan.status == "skipped":
            row.user_override = True
            row.skip_reason = plan.reason
        if plan.status == "watched":
            row.pipeline_reason = f"поиск-наблюдение «{searches[4][0]}»"
        if plan.searches == ",3,":
            row.selection_profile_id = data_set.id
        if plan.status in ("sent", "not_suitable", "filtered_out", "skipped") or plan.days_ago > 2:
            row.seen_by_user_at = first_seen + timedelta(hours=2)
        session.add(row)
        vacancies.append((row, plan))
    await session.flush()
    counts["vacancies"] = len(vacancies)

    # evaluations and letters
    evaluations = letters = 0
    for row, plan in vacancies:
        if plan.score is None:
            continue
        summary, matched, gaps, flags = SUMMARIES.get(
            row.name, ("Совпадение частичное: часть стека знакома, роль смежная.", [], [], [])
        )
        verdict = "apply" if plan.score >= 70 else "maybe" if plan.score >= 50 else "skip"
        evaluated_at = now - timedelta(days=plan.days_ago) + timedelta(hours=3)
        session.add(
            VacancyEvaluation(
                vacancy_id=row.id,
                batch_id=batch_ids[min(int(plan.days_ago // 8), len(batch_ids) - 1)],
                profile_version=1,
                score=plan.score,
                verdict=verdict,
                summary=summary,
                matched_json=json.dumps(matched, ensure_ascii=False),
                gaps_json=json.dumps(gaps, ensure_ascii=False),
                red_flags_json=json.dumps(flags, ensure_ascii=False),
                salary_fit="within" if (row.salary_to or 0) >= (PREFERENCES.salary_min or 0) else "below",
                seniority_fit="match" if plan.score >= 60 else "under",
                language="ru",
                tags_json=json.dumps({"domain": [], "tasks": [], "stack": matched[:3]}, ensure_ascii=False),
                created_at=evaluated_at,
            )
        )
        evaluations += 1
        if plan.letter is not None:
            text = LETTERS[plan.letter]
            clarify = ["сколько лет с Airflow"] if "[[УТОЧНИТЬ" in text and "Airflow" in text else []
            if "PyTorch" in text and "[[УТОЧНИТЬ" in text:
                clarify = ["задачи с PyTorch"]
            session.add(
                CoverLetter(
                    vacancy_id=row.id,
                    version=1,
                    text=text,
                    language="ru",
                    char_count=len(text),
                    origin="claude",
                    batch_id=batch_ids[0],
                    mentions_json=json.dumps(
                        ["Kafka: 40 → 3 секунды", "FastAPI: 120 тысяч запросов"], ensure_ascii=False
                    ),
                    needs_clarification_json=json.dumps(clarify, ensure_ascii=False),
                    warnings_json="[]",
                    created_at=evaluated_at,
                )
            )
            letters += 1
    await session.flush()
    counts["evaluations"], counts["letters"] = evaluations, letters

    # applications: the assistant mode (the response page opened on hh.ru, confirmed by the user) and the manual
    # journal of what the employers did
    applications = 0
    for row, plan in vacancies:
        if plan.status == "needs_manual":
            session.add(
                Application(
                    vacancy_id=row.id,
                    mode="assist",
                    status="needs_manual",
                    letter_text=LETTERS[plan.letter or 0],
                    queued_at=now - timedelta(days=plan.days_ago) + timedelta(hours=5),
                )
            )
            applications += 1
            continue
        if plan.status != "sent" or plan.sent_days_ago is None:
            continue
        sent_at = now - timedelta(days=plan.sent_days_ago)
        app_row = Application(
            vacancy_id=row.id,
            mode="assist",
            status="sent",
            letter_text=LETTERS[plan.letter or 0],
            queued_at=sent_at - timedelta(minutes=20),
            sent_at=sent_at,
            confirmed_by_user=True,
        )
        session.add(app_row)
        await session.flush()
        applications += 1
        await ensure_manual(session, app_row)
        later = sent_at + timedelta(days=min(2.0, plan.sent_days_ago * 0.4), hours=3)
        if plan.stage == "viewed":
            await record_update(session, app_row, viewed=True, occurred_at=later)
        elif plan.stage in ("invitation", "discard"):
            await record_update(
                session, app_row, state=plan.stage, text=EMPLOYER_MESSAGES[plan.stage], occurred_at=later
            )
        elif plan.stage == "message":
            await record_update(session, app_row, text=EMPLOYER_MESSAGES["message"], occurred_at=later)
        if plan.stage == "invitation" and plan.sent_days_ago < 5:
            session.add(
                InterviewPrep(
                    application_id=app_row.id,
                    vacancy_id=row.id,
                    auto=True,
                    status="done",
                    batch_id=batch_ids[0],
                    result_json=json.dumps(INTERVIEW_PREP, ensure_ascii=False),
                )
            )
    await session.flush()
    counts["applications"] = applications

    counts["tenders"] = await _tenders(session, now, batch_ids[0])
    counts["studios"] = await _studios(session, now, batch_ids[0])
    counts["channels"], counts["channel_posts"] = await _channels(session, now)

    # the state of the panel: the assistant mode (hh.ru closed applying through the API to new applications),
    # consent given, the last scan a few minutes ago
    state = await get_system_state(session)
    state.apply_mode = "assist"
    state.consent_accepted_at = now - timedelta(days=40)
    state.last_scan_at = now - timedelta(minutes=12)
    state.probe_at = now - timedelta(days=3)
    state.capabilities_json = json.dumps(
        {
            "me": "available",
            "vacancies_search": "available",
            "vacancy_get": "available",
            "resumes_mine": "unavailable",
            "negotiations_list": "unavailable",
            "negotiations_post": "unavailable",
            "chats": "unavailable",
            "resume_publish": "unavailable",
        }
    )
    session.add(
        JobRun(
            job="scan",
            started_at=now - timedelta(minutes=12, seconds=40),
            finished_at=now - timedelta(minutes=12),
            status="ok",
            stats_json=json.dumps({"searches": 4, "new": 6, "hh_requests": 9, "processed": {"prescored": 4}}),
        )
    )
    await set_setting(
        session,
        "studios_settings",
        {
            "keywords": ["веб-студия", "студия разработки", "digital-агентство", "продуктовая студия"],
            "only_with_vacancies": True,
            "signature": "Иван Петров\nivan.petrov@example.com",
            "offer_notes": "Проектная работа или частичная занятость, от 2 500 ₽ в час.",
        },
    )
    await set_setting(
        session,
        "ws_tenders_settings",
        {
            "sections": ["web-development", "crm", "apps-development"],
            "min_score": 30,
            "rates": "Backend-сервис — от 300 000 ₽, 6–8 недель; доработка — 2 500 ₽ в час.",
        },
    )

    # the event feed of the dashboard
    feed: list[tuple[str, dict[str, Any]]] = [
        ("batch_imported", {"batch_id": batch_ids[0], "evaluations": 12, "letters": 9}),
        ("digest", {"suitable": 4, "maybe": 3, "top": [{"name": v.name, "score": v.score} for v, _ in vacancies[:3]]}),
        ("hot", {"total": 2, "items": [{"name": v.name, "employer": v.employer_name} for v, _ in vacancies[:2]]}),
        (
            "ws_tender",
            {"title": TENDERS[2]["title"], "budget": TENDERS[2]["budget"], "url": "https://workspace.ru/tenders/"},
        ),
        ("studio_reply", {"name": "Студия разработки «Крона»", "subject": "Re: Python-разработчик для проектов"}),
    ]
    for kind, payload in feed:
        await emit_event(session, settings, kind, payload)
    await audit(session, "demo", "Демо-данные загружены: выдуманные вакансии, отклики, тендеры и студии")
    session.add(AuditLog(area="scan", message="Скан: 4 поиска, новых 6", ts=now - timedelta(minutes=12)))
    return counts


INTERVIEW_PREP = {
    "summary": "Команде нужен backend-разработчик, который возьмёт платёжные события на Kafka. Стоит говорить про "
    "задержку чеков и про тесты.",
    "questions": [
        {
            "question": "Как вы перевели расчёт чеков на Kafka?",
            "answer": "Разделил приём чека и расчёт, расчёт ушёл в консьюмеры. Задержка упала с 40 до 3 секунд, "
            "повторы ловим по ключу идемпотентности.",
        },
        {
            "question": "Как вы держите качество кода?",
            "answer": "CI с тестами и линтерами на каждый merge request, покрытие подняли с 35 до 80 %.",
        },
    ],
    "ask_them": ["Какие задачи на первые три месяца?", "Как устроены дежурства?"],
    "risks": ["нет опыта с Kubernetes: честно сказать и показать опыт с Docker"],
    "prepare": ["освежить гарантии доставки в Kafka", "посмотреть публичное API компании"],
}


async def _tenders(session: AsyncSession, now: datetime, batch_id: str) -> int:
    for n, t in enumerate(TENDERS):
        published = now - timedelta(days=1 + n * 1.5)
        status = t["status"]
        row = WsTender(
            ws_id=990_100 + n,
            url=f"https://workspace.ru/tenders/demo-{990_100 + n}/",
            title=t["title"],
            category=t["category"],
            service=t["service"],
            conditions_json=json.dumps([{"title": a, "value": b} for a, b in t["conditions"]], ensure_ascii=False),
            budget=t["budget"],
            budget_max=t["budget_max"],
            published_on=published.date().isoformat(),
            deadline_on=(now + timedelta(days=5 + n)).date().isoformat(),
            state="open",
            city=t["city"],
            organizer=t["organizer"],
            participants=t["participants"],
            views=t["views"],
            fit_score=t["fit"],
            fit_notes_json=json.dumps(["совпадает: Python, PostgreSQL"] if t["fit"] > 50 else [], ensure_ascii=False),
            relevant=t.get("relevant", True),
            status=status,
            notified_at=published,
            detail_fetched_at=published,
            first_seen_at=published,
            last_seen_at=now - timedelta(minutes=30),
            description=t["description"],
        )
        if status in ("responded", "replied", "declined"):
            row.responded_at = published + timedelta(hours=6)
        if status in ("replied", "declined"):
            row.answered_at = published + timedelta(days=1)
        session.add(row)
        await session.flush()
        if status in ("drafted", "responded", "replied", "declined"):
            session.add(
                WsTenderReply(
                    tender_id=row.id,
                    version=1,
                    origin="claude",
                    batch_id=batch_id,
                    needs_clarification_json=json.dumps(["срок", "стоимость"], ensure_ascii=False),
                    warnings_json="[]",
                    created_at=published + timedelta(hours=2),
                    text=TENDER_REPLY,
                )
            )
    return len(TENDERS)


async def _studios(session: AsyncSession, now: datetime, batch_id: str) -> int:
    for n, s in enumerate(STUDIOS):
        seen = now - timedelta(days=2 + n)
        status = s["status"]
        row = Studio(
            name=s["name"],
            domain=s["domain"],
            site_url=f"https://{s['domain']}",
            area=s["area"],
            kind=s["kind"],
            source=s["source"],
            sources_json=json.dumps([s["source"]]),
            letter_kind=s["letter_kind"],
            open_vacancies=s.get("open_vacancies"),
            services_json=json.dumps(s["services"], ensure_ascii=False),
            emails_json=json.dumps(s["emails"]),
            contacts_checked_at=None if status == "new" else seen + timedelta(hours=1),
            contacts_error="на сайте нет почты и ссылки на Telegram" if status == "no_contacts" else None,
            fit_score=s["fit"],
            fit_notes_json=json.dumps(
                ["совпадает: Python, веб-разработка"] if s["fit"] > 50 else [], ensure_ascii=False
            ),
            status=status,
            first_seen_at=seen,
            last_seen_at=now - timedelta(hours=3),
            description=s["description"],
            site_summary=s["description"],
        )
        if status in ("sent", "replied"):
            row.sent_at = seen + timedelta(days=1)
            row.sent_to = s["emails"][0]
            row.sent_via = "email"
        if status == "replied":
            row.replied_at = seen + timedelta(days=2)
            row.reply_subject = "Re: Python-разработчик для проектов"
        session.add(row)
        await session.flush()
        if status in ("drafted", "sent", "replied"):
            for style, (subject, text) in STUDIO_LETTERS.items():
                session.add(
                    StudioLetter(
                        studio_id=row.id,
                        version=1,
                        kind=s["letter_kind"],
                        style=style,
                        subject=subject.format(name=s["name"]),
                        origin="claude",
                        batch_id=batch_id,
                        needs_clarification_json="[]",
                        warnings_json="[]",
                        is_current=style == "business",
                        created_at=seen + timedelta(hours=4),
                        text=text.format(name=s["name"]),
                    )
                )
    return len(STUDIOS)


async def _channels(session: AsyncSession, now: datetime) -> tuple[int, int]:
    ids: dict[str, int] = {}
    for username, title in CHANNELS:
        row = TelegramChannel(
            username=username,
            title=title,
            last_message_id=500,
            last_checked_at=now - timedelta(minutes=20),
            last_new=1,
            posts_count=sum(1 for p in CHANNEL_POSTS if p[0] == username),
        )
        session.add(row)
        await session.flush()
        ids[username] = row.id
    for n, (username, title, text) in enumerate(CHANNEL_POSTS):
        session.add(
            ChannelPost(
                channel_id=ids[username],
                message_id=480 + n,
                posted_at=now - timedelta(hours=3 + n * 7),
                title=title,
                text=f"{title}\n{text}",
                links_json="[]",
                is_vacancy=True,
                lang="ru",
                status="new" if n < 2 else "seen",
            )
        )
    return len(CHANNELS), len(CHANNEL_POSTS)
