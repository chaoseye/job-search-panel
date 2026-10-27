"""Approximate term matching for Russian/English skill names and stop words."""

from __future__ import annotations

import re
from bisect import bisect_right
from functools import lru_cache

_SPACES = re.compile(r"\s+")
_SPECIAL = set("+#./")
# pictographs, emoticons, flags, dingbats, misc symbols/technical, geometric shapes, ‼ ⁉ ™ ℹ Ⓜ ⤴ ⤵, variation
# selectors, ZWJ, keycaps, skin tones, private-use placeholders of custom emoji
_EMOJI = re.compile(
    "["
    "\U0001f000-\U0001faff"
    "\U00002600-\U000027bf"
    "\U00002b00-\U00002bff"
    "\U00002300-\U000023ff"
    "\U000025a0-\U000025ff"
    "\U0000203c\U00002049\U00002122\U00002139\U000024c2\U00002934\U00002935"
    "\U0000fe0e\U0000fe0f\U0000200d\U000020e3"
    "\U0000e000-\U0000f8ff"
    "]+"
)
_EDGE_SEPARATORS = " \t|—–-·•~*"


def strip_emoji(text: str | None) -> str:
    """Titles without emoji: they waste space in Telegram and do not always render. Separators left hanging
    at the edges («| FB», «— »)  are trimmed, inner whitespace collapsed."""
    if not text:
        return ""
    out = _EMOJI.sub(" ", text)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip().strip(_EDGE_SEPARATORS).strip()


NORM_CACHE_MAX_CHARS = 120  # titles, skills, tags, terms repeat by the thousand; descriptions are not cached
# Cyrillic letters that look like Latin ones (а в е к м н о р с т у х ↔ a b e k m h o p c t y x, by the capitals).
# Inside a word that mixes the scripts they are read as Latin: «1С» typed with a Cyrillic С and «1C» are one term,
# so is «Сodex» from a resume and «codex»; a whole Cyrillic word («стажер») is never touched
_HOMOGLYPHS = str.maketrans("авекмнорстух", "abekmhopctyx")
_MIXED_WORD = re.compile(r"(?<![a-zа-я0-9])(?=[a-zа-я0-9]*[а-я])(?=[a-zа-я0-9]*[a-z0-9])[a-zа-я0-9]+")
_CYRILLIC_C_LANG = re.compile(r"(?<![a-zа-я0-9])с(?=\+|#)")  # «С++», «С#» with a Cyrillic С


def norm_plain(text: str | None) -> str:
    """Lower case, ё → е, whitespace collapsed — without the homoglyph folding of norm(). For values already stored
    under this form (the ids of mass-hiring vacancies of company sites hash it)."""
    if not text:
        return ""
    return _SPACES.sub(" ", text.lower().replace("ё", "е")).strip()


def _norm(text: str) -> str:
    out = norm_plain(text)
    if "с" in out:
        out = _CYRILLIC_C_LANG.sub("c", out)
    return _MIXED_WORD.sub(lambda m: m.group(0).translate(_HOMOGLYPHS), out)


_norm_cached = lru_cache(maxsize=65536)(_norm)


def norm(text: str | None) -> str:
    if not text:
        return ""
    # the analytics normalize the same short strings ~400 000 times per report (a term per vacancy per skill)
    return _norm_cached(text) if len(text) <= NORM_CACHE_MAX_CHARS else _norm(text)


@lru_cache(maxsize=4096)
def _pattern(term: str) -> re.Pattern[str]:
    # Cyrillic words may carry up to 3 extra letters (простые словоформы: стажер → стажера, стажером);
    # Latin terms of 5+ chars may carry a short suffix (postgres → postgresql, react → reactjs, python → python3),
    # short Latin terms match exactly (java must not match javascript, go must not match golang).
    if re.search(r"[а-я]", term):
        suffix = "[а-я]{0,3}"
    elif len(term) >= 5:
        suffix = "[a-z0-9]{0,3}"
    else:
        suffix = ""
    return re.compile(rf"(?<![a-zа-я0-9]){re.escape(term)}{suffix}(?![a-zа-я0-9])")


def contains_term(text_norm: str, term: str) -> bool:
    """`text_norm` must already be normalized with norm()."""
    t = norm(term)
    if not t or not text_norm:
        return False
    if " " in t or any(ch in _SPECIAL for ch in t) or len(t) <= 2:
        return t in text_norm if len(t) > 2 else _pattern(t).search(text_norm) is not None
    return _pattern(t).search(text_norm) is not None


_WORD = re.compile(r"[a-zа-я0-9]+")  # the «word» of _pattern's look-arounds
_CYRILLIC = frozenset(chr(c) for c in range(ord("а"), ord("я") + 1))  # [а-я]: ё is not in it
_LATIN = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")


def _suffix_allowed(stem: str, suffix: str) -> bool:
    """Whether `stem` + `suffix` is a form of the term `stem` under _pattern's rules."""
    if any(ch in _CYRILLIC for ch in stem):
        return all(ch in _CYRILLIC for ch in suffix)
    return len(stem) >= 5 and all(ch in _LATIN for ch in suffix)


@lru_cache(maxsize=65536)
def _forms(word: str) -> tuple[str, ...]:
    """The single-word terms a word of a text matches: itself and its stems without an allowed 1–3 letter ending."""
    forms = [word]
    for cut in range(1, min(3, len(word) - 1) + 1):
        if _suffix_allowed(word[:-cut], word[-cut:]):
            forms.append(word[:-cut])
    return tuple(forms)


@lru_cache(maxsize=4096)
def _literal(term: str) -> re.Pattern[str]:
    return re.compile(re.escape(term))


class TermIndex:
    """contains_term over many texts at once — which of `texts_norm` contain a term. The analytics ask it for
    thousands of terms against thousands of lines (skills × evaluation gaps: ~1 million regex searches, 10 s).

    A term of word characters matches a text exactly when some word of the text is the term plus an allowed ending,
    so every word is indexed under itself and its stems without the last 1–3 letters. Other terms («c++», «ci/cd»,
    «работа в команде») are searched once in all the texts joined by «\\n»: no term or ending contains it, and to the
    look-arounds of _pattern it is a boundary like the start or the end of a text."""

    def __init__(self, texts_norm: list[str]) -> None:
        self.texts = texts_norm
        self._stems: dict[str, set[int]] = {}
        for i, text in enumerate(texts_norm):
            for word in set(_WORD.findall(text)):
                for form in _forms(word):
                    self._stems.setdefault(form, set()).add(i)
        self._joined: str | None = None
        self._starts: list[int] = []

    def matching(self, term: str) -> set[int]:
        """Positions of the texts that contain `term` (same result as contains_term on each)."""
        t = norm(term)
        if not t:
            return set()
        if _WORD.fullmatch(t):
            return set(self._stems.get(t, ()))
        if self._joined is None:
            offset = 0
            for text in self.texts:
                self._starts.append(offset)
                offset += len(text) + 1
            self._joined = "\n".join(self.texts)
        # the same rule as contains_term: a phrase or a term with + # . / (longer than 2) is a plain substring
        literal = len(t) > 2 and (" " in t or any(ch in _SPECIAL for ch in t))
        rx = _literal(t) if literal else _pattern(t)
        return {bisect_right(self._starts, m.start()) - 1 for m in rx.finditer(self._joined)}


def count_term(text_norm: str, term: str) -> int:
    """How many times the term occurs (same matching rules as contains_term)."""
    t = norm(term)
    if not t or not text_norm:
        return 0
    if len(t) > 2 and (" " in t or any(ch in _SPECIAL for ch in t)):
        return text_norm.count(t)
    return len(_pattern(t).findall(text_norm))


def terms_equal(a: str, b: str) -> bool:
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # "python 3" vs "python", "postgresql" vs "postgres" — but not "java" vs "javascript"
    shorter, longer = sorted((na, nb), key=len)
    return len(shorter) >= 4 and len(longer) - len(shorter) <= 3 and longer.startswith(shorter)


def significant_words(text: str, min_len: int = 3) -> list[str]:
    return [w for w in re.split(r"[^a-zа-я0-9+#]+", norm(text)) if len(w) >= min_len]
