from __future__ import annotations

import sys

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.exchange.guidelines_md import (
    GUIDELINES_MD,
    GUIDELINES_VERSION,
    SCANNER_PYTHON,
    ensure_guidelines,
    guidelines_path,
)
from app.exchange.schemas import Evaluation, Letter, ResultFile
from app.services.letters import check_before_send, letter_from_result, letter_warnings


def test_evaluation_schema_bounds() -> None:
    ev = Evaluation.model_validate(
        {"score": 88, "verdict": "apply", "summary": " ok ", "matched": ["a", "a", ""], "extra": 1}
    )
    assert ev.score == 88 and ev.summary == "ok" and ev.matched == ["a"] and ev.salary_fit == "unknown"
    with pytest.raises(ValidationError):
        Evaluation.model_validate({"score": 120, "verdict": "apply"})
    with pytest.raises(ValidationError):
        Evaluation.model_validate({"score": 50, "verdict": "later"})


def test_letter_schema_and_helpers() -> None:
    letter = Letter.model_validate(
        {"text": "Здравствуйте!\r\nЯ ответственный [[УТОЧНИТЬ: зарплата]] см. www.site.ru", "language": "ru"}
    )
    assert "\r" not in letter.text
    assert letter.markers() == ["[[УТОЧНИТЬ: зарплата]]"]
    assert letter.has_links() and letter.cliches() == ["ответственный"]
    with pytest.raises(ValidationError):
        Letter.model_validate({"text": "   "})


def test_result_file_schema() -> None:
    rf = ResultFile.model_validate(
        {"batch_id": "x", "results": [{"vacancy_id": 123, "evaluation": None, "letter": None, "error": "no data"}]}
    )
    assert rf.protocol_version == 1 and rf.results[0].vacancy_id == "123" and rf.results[0].error == "no data"


def test_letter_from_result_warnings() -> None:
    short = Letter(text="Коротко. Командный игрок.")
    warnings, clarifications = letter_from_result(short, min_chars=600, max_chars=1200)
    assert any("короче" in w for w in warnings) and any("клише" in w for w in warnings)
    assert clarifications == []
    long = Letter(text="x" * 1500 + " [[УТОЧНИТЬ: дата выхода]]", needs_clarification=["дата выхода"])
    warnings, clarifications = letter_from_result(long, min_chars=600, max_chars=1200)
    assert any("длиннее" in w for w in warnings)
    assert clarifications == ["дата выхода", "[[УТОЧНИТЬ: дата выхода]]"]


def test_check_before_send() -> None:
    assert check_before_send(None, required=True) == ["сопроводительное письмо обязательно для этой вакансии"]
    assert check_before_send(None, required=False) == []
    problems = check_before_send("Пишу вам, см. https://x.y и [[УТОЧНИТЬ: что]]" + "z" * 10001, required=True)
    assert len(problems) == 3
    assert check_before_send("Нормальное письмо без ссылок.", required=True) == []
    assert letter_warnings("t.me/user") and not letter_warnings("чистый текст")


def test_ensure_guidelines_writes_once(settings: Settings) -> None:
    assert ensure_guidelines(settings) is True
    path = guidelines_path(settings)
    text = path.read_text(encoding="utf-8")
    assert f"guidelines_version: {GUIDELINES_VERSION}" in text and "## Формат результата" in text
    assert ensure_guidelines(settings) is False
    path.write_text("---\nguidelines_version: 0\n---\nold", encoding="utf-8")
    assert ensure_guidelines(settings) is True


def test_guidelines_name_the_scanner_interpreter_of_this_machine(settings: Settings) -> None:
    """data/ moved from the Windows laptop to a Linux server: guidelines.md of the same version but with the other
    OS's interpreter is rewritten — the background claude -p would start the scanner by a path that does not exist."""
    from app.services.claude_cli import CLI_FLAGS

    windows, linux = "backend/.venv/Scripts/python.exe", "backend/.venv/bin/python"
    assert SCANNER_PYTHON == (windows if sys.platform == "win32" else linux)
    assert f"`{SCANNER_PYTHON} .claude/skills/humanizer-ru/scripts/scan.py data/exchange/tmp/" in GUIDELINES_MD
    path = guidelines_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(GUIDELINES_MD.replace(SCANNER_PYTHON, linux if SCANNER_PYTHON == windows else windows), "utf-8")
    assert ensure_guidelines(settings) is True
    assert path.read_text(encoding="utf-8") == GUIDELINES_MD
    assert ensure_guidelines(settings) is False
    # the background run allows both spellings, whichever machine wrote the file
    assert any(windows in flag for flag in CLI_FLAGS) and any(linux in flag for flag in CLI_FLAGS)
