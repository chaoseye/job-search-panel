"""Vacancy tags for analytics: industry (`domain`), what the job is about (`tasks`) and the tools (`stack`).

Two sources merged at read time: rule tags computed by the panel from the title, key skills and description
(every vacancy gets them, no Claude needed — `vacancies.tags_json`) and tags Claude returns with an evaluation
(`vacancy_evaluations.tags_json`, guidelines v4). Claude's free-form labels are mapped onto the same canonical
labels when a keyword matches, so «RAG-пайплайны» and «rag» count as one tag.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from app.services.textmatch import contains_term, count_term, norm

GROUPS = ("domain", "tasks", "stack")
GROUP_LABELS = {"domain": "отрасль", "tasks": "задачи", "stack": "инструменты"}
# how many labels a vacancy may carry per group (the most frequently mentioned win)
MAX_PER_GROUP = {"domain": 4, "tasks": 6, "stack": 10}
MAX_LABEL_CHARS = 40

# canonical label → keywords (matched with textmatch.contains_term: word boundaries, short Cyrillic suffixes)
STACK: dict[str, tuple[str, ...]] = {
    "Claude Code": ("claude code",),
    "Claude / Anthropic": ("claude", "anthropic"),
    "Cursor": ("cursor",),
    "Codex": ("codex",),
    "Windsurf": ("windsurf",),
    "GitHub Copilot": ("copilot",),
    "ChatGPT / OpenAI": ("chatgpt", "openai", "gpt-4", "gpt-5", "gpt"),
    "Gemini": ("gemini",),
    "DeepSeek / Qwen / Llama": ("deepseek", "qwen", "llama", "mistral"),
    "GigaChat / YandexGPT": ("gigachat", "yandexgpt", "yandex gpt", "яндекс gpt", "yagpt"),
    "LangChain": ("langchain",),
    "LangGraph / CrewAI / AutoGen": ("langgraph", "crewai", "autogen", "crew ai"),
    "LlamaIndex": ("llamaindex", "llama index"),
    "n8n": ("n8n",),
    "Make / Zapier": ("make.com", "zapier", "integromat"),
    "Dify / Flowise": ("dify", "flowise"),
    "RAG": ("rag", "retrieval"),
    "векторные БД": ("pgvector", "qdrant", "pinecone", "weaviate", "chroma", "faiss", "milvus", "векторн"),
    "MCP": ("mcp", "model context protocol"),
    "Ollama / vLLM": ("ollama", "vllm", "llama.cpp"),
    "Hugging Face": ("hugging face", "huggingface", "transformers"),
    "PyTorch / TensorFlow": ("pytorch", "tensorflow", "keras", "scikit", "sklearn"),
    "Whisper / речь": ("whisper", "speech-to-text", "text-to-speech", "elevenlabs"),
    "Stable Diffusion / Midjourney": ("stable diffusion", "midjourney", "comfyui", "dall-e", "kling", "runway"),
    "промпт-инжиниринг": ("prompt", "промпт"),
    "fine-tuning / LoRA": ("fine-tun", "finetun", "дообуч", "lora"),
    "Python": ("python",),
    "JavaScript / TypeScript": ("javascript", "typescript", "node.js", "nodejs", "node js"),
    "React / Next.js": ("react", "next.js", "nextjs"),
    "Vue / Angular": ("vue", "angular", "nuxt"),
    "FastAPI / Django / Flask": ("fastapi", "django", "flask"),
    "Go": ("golang",),
    "Java / Kotlin": ("java", "kotlin", "spring"),
    "C# / .NET": ("c#", ".net", "asp.net"),
    "PHP": ("php", "laravel", "symfony"),
    "1С / Bitrix": ("1с", "1c", "битрикс", "bitrix"),
    "SQL / PostgreSQL": ("sql", "postgresql", "postgres", "mysql", "clickhouse"),
    "NoSQL / Redis": ("mongodb", "redis", "elasticsearch"),
    "Docker / Kubernetes": ("docker", "kubernetes", "k8s"),
    "Linux / DevOps": ("linux", "ci/cd", "gitlab ci", "ansible", "terraform", "nginx"),
    "Git": ("git", "github", "gitlab"),
    "Airflow / Kafka / Spark": ("airflow", "kafka", "spark", "dbt"),
    "Telegram-боты": ("telegram", "aiogram", "телеграм"),
    "Supabase / Firebase": ("supabase", "firebase"),
    "no-code / low-code": ("no-code", "nocode", "low-code", "lowcode", "bubble", "tilda", "webflow", "airtable"),
    "Google Sheets / Excel": ("google sheets", "excel", "гугл таблиц", "google таблиц"),
    "amoCRM / Bitrix24": ("amocrm", "amo crm", "bitrix24", "битрикс24", "битрикс 24"),
    "Power BI / DataLens": ("power bi", "tableau", "datalens", "metabase", "superset"),
    "Figma": ("figma",),
    "Jira / Confluence / Notion": ("jira", "confluence", "notion"),
    "Playwright / Selenium": ("playwright", "selenium", "puppeteer"),
    "мобильная разработка": ("swift", "flutter", "react native", "android", "ios"),
    "REST / GraphQL": ("rest api", "restful", "graphql", "openapi", "swagger"),
    "Computer Vision": ("computer vision", "opencv", "yolo", "компьютерное зрение", "компьютерного зрения"),
}

TASKS: dict[str, tuple[str, ...]] = {
    "RAG / поиск по документам": (
        "rag",
        "retrieval",
        "база знаний",
        "базы знаний",
        "базу знаний",
        "поиск по документам",
        "векторн",
    ),
    "чат-боты и ассистенты": ("чат-бот", "чатбот", "chatbot", "бот", "ассистент", "assistant", "copilot"),
    "AI-агенты": ("агент", "agent", "мультиагент", "multi-agent", "langgraph", "crewai", "autogen"),
    "автоматизация процессов": ("автоматизац", "automation", "n8n", "zapier", "make.com", "workflow", "бизнес-процесс"),
    "генерация контента": (
        "генерац",
        "генерир",
        "создание контента",
        "контент-мейк",
        "копирайт",
        "copywrit",
        "midjourney",
        "генеративн",
    ),
    "разработка продукта / vibe-coding": (
        "vibe",
        "вайб",
        "mvp",
        "прототип",
        "prototype",
        "разработка приложен",
        "веб-приложен",
        "full stack",
        "fullstack",
        "full-stack",
    ),
    "интеграции и API": ("интеграц", "integration", "api", "webhook"),
    "промпт-инжиниринг": ("промпт", "prompt"),
    "обучение моделей / ML": (
        "fine-tun",
        "дообуч",
        "обучение модел",
        "machine learning",
        "машинное обучение",
        "машинного обучения",
        "data science",
        "ml",
    ),
    "аналитика данных": ("аналитик", "analytics", "dashboard", "дашборд", "bi"),
    "компьютерное зрение": ("computer vision", "opencv", "yolo", "распознаван", "компьютерное зрение"),
    "речь и голос": ("speech", "whisper", "голосов", "asr", "tts", "телефони"),
    "внедрение AI в бизнес": (
        "внедрение ai",
        "внедрение ии",
        "внедрению ai",
        "внедрению ии",
        "внедрения ai",
        "внедрения ии",
        "внедрять ai",
        "внедрять ии",
        "внедрение нейросет",
        "внедрение llm",
        "внедрение искусственного интеллекта",
        "ai-трансформац",
        "консалт",
        "consult",
        "обучение сотрудник",
    ),
    "парсинг и данные": ("парсинг", "parsing", "scrap", "сбор данных", "etl", "pipeline"),
    "тестирование": ("тестирован", "qa", "автотест"),
    "поддержка и операции": ("поддержк", "support", "оператор", "модерац"),
}

DOMAIN: dict[str, tuple[str, ...]] = {
    "финтех и банки": ("банк", "финтех", "fintech", "платеж", "кредит", "страхов", "инвестиц", "трейдинг", "финанс"),
    "крипто и web3": ("крипто", "crypto", "web3", "блокчейн", "blockchain", "defi", "nft"),
    "e-commerce и ритейл": (
        "e-commerce",
        "ecommerce",
        "интернет-магазин",
        "маркетплейс",
        "marketplace",
        "ozon",
        "wildberries",
        "ритейл",
        "retail",
        "торгов",
    ),
    "образование / edtech": ("edtech", "образовательн", "онлайн-школ", "онлайн-курс", "университет", "обучающ"),
    "медицина": ("медицин", "здоров", "клиник", "medtech", "health", "фарм", "пациент"),
    "маркетинг и реклама": ("маркетинг", "маркетолог", "реклам", "smm", "seo", "performance-маркет", "таргетолог"),
    "HR и рекрутинг": ("hr", "рекрут", "подбор персонала", "кадров", "hrtech"),
    "недвижимость и строительство": (
        "недвижим",
        "недвижимост",
        "застройщ",
        "строител",
        "строительств",
        "девелопер",
        "proptech",
    ),
    "логистика и транспорт": ("логистик", "доставк", "транспорт", "перевоз", "склад", "такси"),
    "телеком": ("телеком", "оператор связи", "мтс", "билайн", "мегафон", "ростелеком"),
    "госсектор": ("государств", "государственн", "госуслуг", "муниципал", "govtech"),
    "игры": ("игров", "gamedev", "game", "видеоигр", "киберспорт"),
    "медиа и контент": ("медиа", "media", "издател", "издательств", "новост", "кино", "продакшн", "блогер"),
    "консалтинг и аутсорс": (
        "аутсорс",
        "outsourc",
        "аутстаф",
        "консалтинг",
        "интегратор",
        "заказная разработка",
        "студи",
        "агентство",
    ),
    "промышленность и энергетика": (
        "промышлен",
        "промышленност",
        "завод",
        "производств",
        "энергет",
        "нефт",
        "металл",
        "машиностроен",
    ),
    "юридическое": ("юридическ", "legal", "нотари", "судеб"),
    "туризм и HoReCa": ("туризм", "travel", "отел", "ресторан", "horeca", "гостиниц"),
    "стартап": ("стартап", "startup"),
    "SaaS / IT-продукт": ("saas", "it-продукт", "it-компани", "b2b saas", "продуктовая компани", "продуктовой компани"),
    "автомобили": ("автомоб", "автодилер", "каршеринг"),
    "агро": ("агро", "сельск"),
}

VOCAB: dict[str, dict[str, tuple[str, ...]]] = {"domain": DOMAIN, "tasks": TASKS, "stack": STACK}


def empty_tags() -> dict[str, list[str]]:
    return {g: [] for g in GROUPS}


def _match(group: str, text_norm: str, vocab: dict[str, tuple[str, ...]]) -> list[str]:
    hits: list[tuple[int, int, str]] = []
    for order, (label, keywords) in enumerate(vocab.items()):
        # «1с» and «1c» normalize to one term (textmatch folds mixed-script words): count each occurrence once
        n = sum(count_term(text_norm, kw) for kw in dict.fromkeys(norm(k) for k in keywords))
        if n:
            hits.append((-n, order, label))
    hits.sort()
    return [label for _, _, label in hits[: MAX_PER_GROUP[group]]]


def rule_tags(name: str | None, key_skills: list[str] | None, description: str | None) -> dict[str, list[str]]:
    """Tags from the vacancy text; the title and key skills count twice (they are the strongest signal)."""
    head = norm(" ".join([name or "", ", ".join(key_skills or [])]))
    body = norm(description or "")
    text = f"{head} . {head} . {body}"
    if not text.strip(" ."):
        return empty_tags()
    return {g: _match(g, text, VOCAB[g]) for g in GROUPS}


@lru_cache(maxsize=8192)
def canonical(group: str, label: str) -> str:
    """Maps a free-form (Claude) label onto the panel's canonical label when a keyword matches. Cached: VOCAB is
    fixed, and the analytics map the same few hundred labels for every vacancy on every load."""
    text = norm(label)
    if not text:
        return ""
    vocab = VOCAB.get(group, {})
    if label in vocab:
        return label
    for canon, keywords in vocab.items():
        if norm(canon) == text or any(contains_term(text, kw) for kw in keywords):
            return canon
    return re.sub(r"\s+", " ", label.strip())[:MAX_LABEL_CHARS]


def clean_tags(raw: Any) -> dict[str, list[str]]:
    """Validates/normalizes a tags object from JSON (rule or Claude): known groups, ≤ 6 short unique labels."""
    out = empty_tags()
    if not isinstance(raw, dict):
        return out
    for g in GROUPS:
        values = raw.get(g)
        if not isinstance(values, list):
            continue
        seen: set[str] = set()
        for v in values:
            label = canonical(g, str(v)) if isinstance(v, str | int | float) else ""
            key = norm(label)
            if label and key not in seen:
                seen.add(key)
                out[g].append(label)
            if len(out[g]) >= MAX_PER_GROUP[g]:
                break
    return out


def load_tags(value: str | None) -> dict[str, list[str]]:
    if not value:
        return empty_tags()
    try:
        return clean_tags(json.loads(value))
    except ValueError:
        return empty_tags()


def merge_tags(*sources: dict[str, list[str]]) -> dict[str, list[str]]:
    out = empty_tags()
    for src in sources:
        for g in GROUPS:
            seen = {norm(x) for x in out[g]}
            for label in src.get(g, []):
                if norm(label) not in seen:
                    out[g].append(label)
                    seen.add(norm(label))
    return out
