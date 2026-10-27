"""Look-alike letters: «1С» typed with a Cyrillic С and «1C» with a Latin one are one term for the
stop words, the rules and the tags; words of one script keep their letters."""

from __future__ import annotations

import pytest

from app.hh.vacancy import normalize_vacancy
from app.services.company_sites import _mass_id
from app.services.preferences import Preferences
from app.services.rules import RulesContext, apply_rules
from app.services.tagging import _match, rule_tags
from app.services.textmatch import contains_term, count_term, norm, norm_plain
from tests.unit.test_vacancy_norm import short_item


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1С:Предприятие", "1c:предприятие"),  # a digit and a Cyrillic С: a Latin-script word
        ("Программист 1C", "программист 1c"),
        ("Сodex CLI", "codex cli"),  # Cyrillic С inside a Latin word
        ("Pуthon-разработчик", "python-разработчик"),  # Cyrillic у, the Russian half stays Russian
        ("С++ / С#", "c++ / c#"),
        ("Стажёр-программист", "стажер-программист"),  # one script: nothing to fold
        ("работа с клиентами", "работа с клиентами"),  # the preposition «с» is not the C language
        ("Python  3\n", "python 3"),
        ("", ""),
    ],
)
def test_norm_folds_look_alikes_only_inside_mixed_words(raw: str, expected: str) -> None:
    assert norm(raw) == expected
    assert norm(norm(raw)) == norm(raw)


def test_norm_plain_keeps_the_letters() -> None:
    # the stored ids (company sites) hash this form: it must not change
    assert norm_plain("1С:Предприятие  8") == "1с:предприятие 8"
    assert norm_plain(None) == ""
    assert _mass_id("Москва", "Продавец 1С") == _mass_id("Москва", "продавец  1с")
    assert _mass_id("Москва", "Продавец 1С") != _mass_id("Москва", "Продавец 1C")


@pytest.mark.parametrize("text", ["Программист 1С", "Программист 1C", "Разработчик 1С:ERP", "1c-bitrix"])
@pytest.mark.parametrize("term", ["1с", "1c", "1С", "1C"])
def test_1c_is_one_term_in_both_scripts(text: str, term: str) -> None:
    assert contains_term(norm(text), term)


def test_c_languages_and_words() -> None:
    assert contains_term(norm("Разработчик С++"), "c++")
    assert contains_term(norm("Senior C# developer"), "С#")
    assert contains_term(norm("Pуthon backend"), "python")
    assert not contains_term(norm("работа с клиентами"), "c")
    assert count_term(norm("1С, 1C и снова 1с"), "1c") == 3


@pytest.mark.parametrize(("stop_word", "title"), [("1С", "Программист 1C"), ("1C", "Программист 1С")])
def test_stop_word_in_either_script_filters_the_vacancy(stop_word: str, title: str) -> None:
    ctx = RulesContext(prefs=Preferences(stop_words_title=[stop_word]))
    r = apply_rules(normalize_vacancy(short_item(name=title), full=False), ctx)
    assert r.status == "filtered_out" and r.reasons == [f"стоп-слово в названии: {stop_word}"]
    assert apply_rules(normalize_vacancy(short_item(name="Программист Python"), full=False), ctx).passed


def test_tags_count_1c_once_per_occurrence() -> None:
    tags = rule_tags("Программист 1С", ["1C:Предприятие"], "Доработка конфигураций 1С и интеграция с Битрикс.")
    assert "1С / Bitrix" in tags["stack"]
    # «1с» and «1c» are one keyword now: one mention counts once and loses to a label mentioned twice
    vocab = {"1С": ("1с", "1c"), "Python": ("python",)}
    assert _match("stack", norm("1С, python и снова python"), vocab) == ["Python", "1С"]
