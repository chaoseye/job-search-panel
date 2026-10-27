"""Rule tags for analytics: vocabulary matching, canonical mapping of Claude labels, cleaning and merging."""

from __future__ import annotations

from app.services.tagging import GROUPS, canonical, clean_tags, empty_tags, load_tags, merge_tags, rule_tags


def test_rule_tags_from_title_skills_and_description() -> None:
    tags = rule_tags(
        "ML-инженер (LLM, RAG) в финтех",
        ["Python", "LangChain", "PostgreSQL"],
        "Строим RAG-пайплайны и чат-ботов для банка на Claude Code и n8n; векторная база Qdrant, Docker. "
        "Автоматизация процессов поддержки клиентов.",
    )
    assert "финтех и банки" in tags["domain"]
    assert {"RAG / поиск по документам", "чат-боты и ассистенты", "автоматизация процессов"} <= set(tags["tasks"])
    assert {"Python", "LangChain", "SQL / PostgreSQL", "Claude Code", "n8n", "векторные БД", "RAG"} <= set(
        tags["stack"]
    )
    assert all(len(tags[g]) <= 10 for g in GROUPS)
    # a Cyrillic stem picks up its word forms, «играет ключевую роль» is not the games industry
    tags = rule_tags("Разработчик", [], "Автоматизацией занимаемся давно; ИИ играет ключевую роль в недвижимости.")
    assert "автоматизация процессов" in tags["tasks"] and "игры" not in tags["domain"]
    assert "недвижимость и строительство" in tags["domain"]
    assert rule_tags(None, None, None) == empty_tags()


def test_canonical_and_clean() -> None:
    assert canonical("stack", "Python") == "Python"
    assert canonical("stack", "работа с LangChain и агентами") == "LangChain"
    assert canonical("domain", "финтех") == "финтех и банки"
    assert canonical("tasks", "RAG-пайплайны") == "RAG / поиск по документам"
    assert canonical("stack", "Какой-то редкий инструмент") == "Какой-то редкий инструмент"
    assert canonical("stack", "   ") == ""
    cleaned = clean_tags(
        {"domain": ["финтех", "Финтех и банки", "x" * 80], "tasks": "not a list", "stack": [1, "n8n", "N8N"], "junk": 1}
    )
    assert cleaned["domain"] == ["финтех и банки", "x" * 40]
    assert cleaned["tasks"] == [] and cleaned["stack"] == ["1", "n8n"]
    assert clean_tags(None) == empty_tags() and load_tags("not json") == empty_tags()
    assert load_tags('{"stack": ["Cursor"]}')["stack"] == ["Cursor"]


def test_merge_tags() -> None:
    merged = merge_tags({"stack": ["Python", "n8n"]}, {"stack": ["python", "Cursor"], "domain": ["медицина"]}, {})
    assert merged["stack"] == ["Python", "n8n", "Cursor"] and merged["domain"] == ["медицина"] and merged["tasks"] == []
