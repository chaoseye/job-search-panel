"""Words that keep a vacancy out of the Telegram bot (kinds of jobs the user does not want to be told about). The
vacancy stays in the panel and in the UI feed; only its Telegram message is suppressed, or it is dropped from a list
message (digest, hot). A word matches the start of a word of the title: «продаж» finds «продажам», «продажи»; a
phrase («холодные звонки») matches as written. Only the title is checked — the employer's name or a post's text would
hide too much.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.textmatch import norm

SETTING = "telegram_stop_words"
MAX_WORDS = 100
MAX_WORD_CHARS = 60
# the events about vacancies: one vacancy (the title key) or a list of them (the list key, the title key of an item)
SINGLE_EVENTS = {"search_new": "title", "site_vacancy": "title", "channel_post": "title", "letter": "name"}
LIST_EVENTS = {"digest": ("top", "name"), "hot": ("items", "name")}
FILTERED_EVENTS = frozenset(SINGLE_EVENTS) | frozenset(LIST_EVENTS)


def clean_words(words: list[str]) -> list[str]:
    """Trimmed, one space inside, no duplicates (by the normalized form), at most MAX_WORDS."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in words:
        word = " ".join(str(raw).split())[:MAX_WORD_CHARS]
        key = norm(word)
        if key and key not in seen:
            seen.add(key)
            out.append(word)
    return out[:MAX_WORDS]


async def stop_words(session: AsyncSession) -> list[str]:
    from app.services.state import get_setting

    stored = await get_setting(session, SETTING)
    return clean_words(stored) if isinstance(stored, list) else []


async def save_stop_words(session: AsyncSession, words: list[str]) -> list[str]:
    from app.services.audit import audit
    from app.services.state import set_setting

    value = clean_words(words)
    await set_setting(session, SETTING, value)
    await audit(
        session,
        "telegram",
        "Вакансии со словами в названии не приходят в бота: " + (", ".join(value) if value else "список пуст"),
    )
    return value


def matched_word(title: Any, words: list[str]) -> str | None:
    """The first word of the list that starts a word of the title."""
    text = norm(str(title or ""))
    if not text:
        return None
    for word in words:
        key = norm(word)
        if key and re.search(r"(?<![a-zа-я0-9])" + re.escape(key), text):
            return word
    return None


def telegram_payload(
    event_type: str, payload: dict[str, Any], words: list[str]
) -> tuple[dict[str, Any] | None, str | None]:
    """(the payload for the Telegram message, why it is suppressed). The same payload when nothing matched; a copy
    without the matched vacancies for a list; (None, reason) when nothing is left to send."""
    if not words:
        return payload, None
    if event_type in SINGLE_EVENTS:
        word = matched_word(payload.get(SINGLE_EVENTS[event_type]), words)
        return (None, f"в названии «{word}» — не для бота") if word else (payload, None)
    if event_type in LIST_EVENTS:
        list_key, title_key = LIST_EVENTS[event_type]
        items = payload.get(list_key)
        if not isinstance(items, list) or not items:
            return payload, None
        kept = [i for i in items if not (isinstance(i, dict) and matched_word(i.get(title_key), words))]
        if len(kept) == len(items):
            return payload, None
        if not kept:
            first = next(matched_word(i.get(title_key), words) for i in items if isinstance(i, dict))
            return None, f"в названиях «{first}» — не для бота"
        copy = {**payload, list_key: kept}
        if event_type == "hot":
            copy["total"] = len(kept)
        return copy, None
    return payload, None
