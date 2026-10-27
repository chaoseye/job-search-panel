"""Tolerant normalization of hh.ru negotiation and message payloads (chats per the public spec).

Field names follow the public spec: negotiation items carry `id`, `state{id,name}`, `created_at`, `updated_at`,
`has_updates`, `viewed_by_opponent`, `messaging_status`, `chat_id`, `counters{messages, unread_messages}`,
`resume{id}`, `vacancy{…}` (short representation), `hidden`, `url`. Messages come either from
`GET /negotiations/{nid}/messages` (`items[]{id, text, created_at, author.participant_type, viewed_by_me,
viewed_by_opponent}`) or from `GET /common/chats/{chat_id}/messages` (`messages[]{id, creation_time,
payload.text, sender_display_info{role, name}, viewed_by_opponent}`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.hh.vacancy import _dict, _list, _obj_id, _obj_name, parse_hh_datetime

NEGOTIATION_STATES = ("response", "invitation", "discard", "hidden")
STATE_LABELS = {
    "response": "отклик",
    "invitation": "приглашение",
    "discard": "отказ",
    "hidden": "скрыт",
}


class NegotiationData(BaseModel):
    hh_id: str
    state_id: str | None = None
    state_name: str | None = None
    hidden: bool = False
    viewed_by_opponent: bool = False
    has_updates: bool = False
    messaging_status: str | None = None
    chat_id: str | None = None
    unread_count: int = 0
    messages_count: int = 0
    url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    resume_hh_id: str | None = None
    vacancy_hh_id: str | None = None
    vacancy_name: str | None = None
    employer_name: str | None = None
    vacancy_raw: dict[str, Any] | None = None
    decline_allowed: bool | None = None
    source: str | None = None


class MessageData(BaseModel):
    hh_message_id: str
    author_type: str = "unknown"  # applicant | employer | bot | unknown
    author_name: str | None = None
    text: str | None = None
    created_at: datetime | None = None
    viewed_by_me: bool | None = None
    viewed_by_opponent: bool | None = None
    source: str = "negotiation"
    raw: dict[str, Any] = Field(default_factory=dict)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _bool(value: Any) -> bool:
    return value is True


def normalize_negotiation(raw: dict[str, Any]) -> NegotiationData:
    state = _dict(raw, "state")
    counters = _dict(raw, "counters")
    resume = _dict(raw, "resume")
    vacancy: dict[str, Any] | None = _dict(raw, "vacancy") or None
    employer = _dict(vacancy, "employer") if vacancy else {}
    state_id = (_obj_id(state) or "").lower() or None
    chat_id = raw.get("chat_id")
    return NegotiationData(
        hh_id=str(raw.get("id") or ""),
        state_id=state_id,
        state_name=_obj_name(state),
        hidden=_bool(raw.get("hidden")) or state_id == "hidden",
        viewed_by_opponent=_bool(raw.get("viewed_by_opponent")),
        has_updates=_bool(raw.get("has_updates")),
        messaging_status=str(raw["messaging_status"]) if raw.get("messaging_status") else None,
        chat_id=str(chat_id) if chat_id not in (None, "") else None,
        unread_count=_int(counters.get("unread_messages")),
        messages_count=_int(counters.get("messages")),
        url=str(raw["url"]) if raw.get("url") else None,
        created_at=parse_hh_datetime(raw.get("created_at")),
        updated_at=parse_hh_datetime(raw.get("updated_at")),
        resume_hh_id=_obj_id(resume),
        vacancy_hh_id=_obj_id(vacancy) if vacancy else None,
        vacancy_name=_obj_name(vacancy) if vacancy else None,
        employer_name=_obj_name(employer) if employer else None,
        vacancy_raw=vacancy,
        decline_allowed=raw.get("decline_allowed") if isinstance(raw.get("decline_allowed"), bool) else None,
        source=str(raw["source"]) if raw.get("source") else None,
    )


def _author_from_participant(value: Any) -> str:
    text = str(value or "").lower()
    if text in ("applicant", "employer"):
        return text
    if text in ("bot", "system"):
        return "bot"
    return "unknown"


def normalize_negotiation_messages(payload: Any) -> list[MessageData]:
    """`GET /negotiations/{nid}/messages` → items[]."""
    items = payload.get("items") if isinstance(payload, dict) else None
    out: list[MessageData] = []
    for item in _list(items):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        author = _dict(item, "author")
        out.append(
            MessageData(
                hh_message_id=str(item["id"]),
                author_type=_author_from_participant(author.get("participant_type")),
                text=str(item["text"]) if item.get("text") else None,
                created_at=parse_hh_datetime(item.get("created_at")),
                viewed_by_me=item.get("viewed_by_me") if isinstance(item.get("viewed_by_me"), bool) else None,
                viewed_by_opponent=(
                    item.get("viewed_by_opponent") if isinstance(item.get("viewed_by_opponent"), bool) else None
                ),
                source="negotiation",
                raw=item,
            )
        )
    return out


def normalize_chat_messages(payload: Any) -> list[MessageData]:
    """`GET /common/chats/{chat_id}/messages` → messages[] (roles APPLICANT/EMPLOYER/BOT)."""
    items = payload.get("messages") if isinstance(payload, dict) else None
    out: list[MessageData] = []
    for item in _list(items):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        if item.get("type") not in (None, "SIMPLE"):
            continue  # participant joined/left — not a message
        sender = _dict(item, "sender_display_info")
        payload_obj = _dict(item, "payload")
        role = str(sender.get("role") or "").upper()
        if sender.get("is_current_participant") is True:
            author = "applicant"
        elif role == "EMPLOYER":
            author = "employer"
        elif role == "APPLICANT":
            author = "applicant"
        elif role == "BOT":
            author = "bot"
        else:
            author = "unknown"
        text = payload_obj.get("text")
        attachments = payload_obj.get("attachments") if isinstance(payload_obj.get("attachments"), list) else []
        if not text and attachments:
            titles = [str(a.get("title") or "файл") for a in attachments if isinstance(a, dict)]
            text = "[вложение] " + ", ".join(titles)
        out.append(
            MessageData(
                hh_message_id=str(item["id"]),
                author_type=author,
                author_name=str(sender["name"]) if sender.get("name") else None,
                text=str(text) if text else None,
                created_at=parse_hh_datetime(item.get("creation_time")),
                viewed_by_opponent=(
                    item.get("viewed_by_opponent") if isinstance(item.get("viewed_by_opponent"), bool) else None
                ),
                source="chat",
                raw=item,
            )
        )
    return out


__all__ = [
    "NEGOTIATION_STATES",
    "STATE_LABELS",
    "NegotiationData",
    "MessageData",
    "normalize_negotiation",
    "normalize_negotiation_messages",
    "normalize_chat_messages",
]
