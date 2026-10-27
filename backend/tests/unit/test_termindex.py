"""The indexes that replaced the per-pair regex searches of the analytics give exactly the old answers (property tests):
TermIndex ≡ contains_term on every text, _unused_skills ≡ the old «terms_equal or contains_term» over every demanded
skill, Base.title_keys ≡ contains_term of every target title."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.analytics import Base, V, _unused_skills
from app.services.textmatch import TermIndex, contains_term, norm, terms_equal

hypothesis = pytest.importorskip("hypothesis")
st = pytest.importorskip("hypothesis.strategies")
given, settings = hypothesis.given, hypothesis.settings

# letters of both scripts (ё and the Ukrainian і are outside [а-я]), digits, the separators the rules care about
ALPHABET = "abcjpytoнпытонажерсяё0123 -.+#/\nіIЯ"
texts = st.lists(st.text(alphabet=ALPHABET, max_size=40), max_size=12)
terms = st.text(alphabet=ALPHABET, min_size=1, max_size=10)
REAL = ["python", "python3", "c++", "ci/cd", "big data", "go", "golang", "стажер", "стажера", "1с", "rag", "back-end"]


@settings(max_examples=300, deadline=None)
@given(texts, st.lists(terms | st.sampled_from(REAL), min_size=1, max_size=8))
def test_term_index_matches_contains_term(raw: list[str], queries: list[str]) -> None:
    for normalized in (False, True):
        items = [norm(t) for t in raw] if normalized else raw
        index = TermIndex(items)
        for q in queries:
            assert index.matching(q) == {i for i, t in enumerate(items) if contains_term(t, q)}, (items, q)


@settings(max_examples=200, deadline=None)
@given(st.lists(terms | st.sampled_from(REAL), max_size=6), st.lists(terms | st.sampled_from(REAL), max_size=12))
def test_unused_skills_match_the_pairwise_rule(mine: list[str], demanded_raw: list[str]) -> None:
    demanded = list(dict.fromkeys(k for k in (norm(d) for d in demanded_raw) if k))
    expected = [s for s in mine if not any(terms_equal(s, d) or contains_term(d, s) for d in demanded)]
    assert _unused_skills(mine, demanded) == expected


def _v(i: int, name: str) -> V:
    return V(
        id=i,
        hh_id=str(i),
        name=name,
        url=None,
        employer_id=None,
        employer_name=None,
        accredited=None,
        trusted=None,
        area=None,
        salary=None,
        experience=None,
        formats=[],
        published_at=None,
        first_seen_at=None,
        archived=False,
        direct=False,
        has_test=False,
        letter_required=False,
        key_skills=[],
        responses=None,
        status="prescored",
        prescore=None,
        score=None,
        set_id=None,
        search_ids=[],
        hidden=False,
    )


@settings(max_examples=200, deadline=None)
@given(
    st.lists(st.text(alphabet=ALPHABET, max_size=30), max_size=10), st.lists(terms | st.sampled_from(REAL), max_size=5)
)
def test_title_keys_match_contains_term(names: list[str], titles: list[str]) -> None:
    vacancies = [_v(i, n) for i, n in enumerate(names)]
    base = Base(vacancies, {}, [], {}, {}, titles, [], None, set(), ZoneInfo("UTC"), datetime.now(UTC))
    scoped = base.scoped("passed")
    for v in vacancies:
        expected = [(norm(t), t) for t in titles if contains_term(norm(v.name), t)]
        assert base.title_keys(v) == expected
        assert scoped.title_keys(v) == expected  # a scoped base asks its root
