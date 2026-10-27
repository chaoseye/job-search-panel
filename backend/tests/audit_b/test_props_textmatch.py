"""Property-тесты чистых функций сопоставления текста (services/textmatch.py) и мелких помощников."""

from __future__ import annotations

import re

from hypothesis import given
from hypothesis import strategies as st

from app.services.textmatch import (
    _EMOJI,
    contains_term,
    count_term,
    norm,
    significant_words,
    strip_emoji,
    terms_equal,
)

any_text = st.text(min_size=0, max_size=200)
word = st.from_regex(r"[a-zа-я]{3,10}", fullmatch=True)
words = st.lists(word, min_size=0, max_size=6)


@given(any_text)
def test_norm_is_idempotent_and_tidy(text: str) -> None:
    once = norm(text)
    assert norm(once) == once
    assert "ё" not in once and "Ё" not in once
    assert once == once.strip()
    assert "  " not in once
    assert once == once.lower()


@given(any_text, any_text)
def test_contains_and_count_never_raise_and_agree(text: str, term: str) -> None:
    t = norm(text)
    found = contains_term(t, term)
    assert isinstance(found, bool)
    assert (count_term(t, term) > 0) == found


@given(word, words, words)
def test_whole_word_is_found_between_other_words(term: str, before: list[str], after: list[str]) -> None:
    text = " ".join([*before, term, *after])
    assert contains_term(norm(text), term)


@given(st.from_regex(r"[0-9 .,;:!?()]{0,40}", fullmatch=True), word)
def test_term_absent_from_text_without_letters(text: str, term: str) -> None:
    assert not contains_term(norm(text), term)


@given(any_text)
def test_empty_term_or_text_never_matches(text: str) -> None:
    assert not contains_term(norm(text), "")
    assert not contains_term("", text)


@given(any_text, any_text)
def test_terms_equal_is_symmetric(a: str, b: str) -> None:
    assert terms_equal(a, b) == terms_equal(b, a)


@given(any_text)
def test_terms_equal_is_reflexive_for_non_empty(a: str) -> None:
    assert terms_equal(a, a) == bool(norm(a))


def test_terms_equal_known_pairs() -> None:
    assert terms_equal("postgres", "PostgreSQL")
    assert terms_equal("python 3", "python")
    assert not terms_equal("java", "javascript")
    assert not terms_equal("go", "golang")


@given(any_text)
def test_strip_emoji_is_idempotent_and_removes_pictographs(text: str) -> None:
    out = strip_emoji(text)
    assert strip_emoji(out) == out
    assert _EMOJI.search(out) is None
    assert out == out.strip()


@given(any_text, st.integers(min_value=1, max_value=6))
def test_significant_words_shape(text: str, min_len: int) -> None:
    out = significant_words(text, min_len=min_len)
    for w in out:
        assert len(w) >= min_len
        assert re.fullmatch(r"[a-zа-я0-9+#]+", w)


def test_cyrillic_word_forms_and_latin_suffixes() -> None:
    t = norm("Ищем стажёра в отдел, нужен Python3 и React")
    assert contains_term(t, "стажер")
    assert contains_term(t, "python")
    assert contains_term(t, "react")
    assert not contains_term(t, "java")  # «java» не должно находить «javascript»-подобные слова
    assert not contains_term(norm("javascript developer"), "java")
