"""Cached hh.ru dictionaries: /dictionaries, /industries, /professional_roles; 7-day TTL."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import as_utc, utcnow
from app.hh.client import HHClient
from app.hh.errors import HHError
from app.services.state import get_setting, set_setting

log = structlog.get_logger("dictionaries")

EDUCATION_OPTIONS = [
    {"id": "not_required_or_not_specified", "name": "не требуется или не указано"},
    {"id": "special_secondary", "name": "среднее специальное"},
    {"id": "higher", "name": "высшее"},
]


async def get_cached(
    session: AsyncSession,
    client: HHClient,
    name: str,
    path: str,
    *,
    ttl_days: int = 7,
    params: dict[str, Any] | None = None,
) -> Any:
    key = f"dict:{name}"
    cached = await get_setting(session, key)
    if isinstance(cached, dict) and "data" in cached:
        fetched = cached.get("fetched_at")
        try:
            fetched_at = as_utc(__import__("datetime").datetime.fromisoformat(fetched)) if fetched else None
        except ValueError:
            fetched_at = None
        if fetched_at and utcnow() - fetched_at < timedelta(days=ttl_days):
            return cached["data"]
    try:
        data = await client.public(path, params=params)
    except HHError as exc:
        if isinstance(cached, dict) and "data" in cached:
            log.warning("dictionary_refresh_failed_using_stale", name=name, error=exc.summary())
            return cached["data"]
        raise
    if _looks_complete(name, data):
        await set_setting(session, key, {"fetched_at": utcnow().isoformat(), "data": data})
    else:
        log.warning("dictionary_payload_incomplete_not_cached", name=name)
    return data


REQUIRED_KEYS = {"dictionaries": ("experience", "currency", "vacancy_search_order")}


def _looks_complete(name: str, data: Any) -> bool:
    required = REQUIRED_KEYS.get(name)
    if required:
        return isinstance(data, dict) and all(k in data for k in required)
    return isinstance(data, (dict, list)) and bool(data)


async def dictionaries(session: AsyncSession, client: HHClient, ttl_days: int = 7) -> dict[str, Any]:
    data = await get_cached(session, client, "dictionaries", "/dictionaries", ttl_days=ttl_days)
    return data if isinstance(data, dict) else {}


async def industries(session: AsyncSession, client: HHClient, ttl_days: int = 7) -> list[dict[str, Any]]:
    data = await get_cached(session, client, "industries", "/industries", ttl_days=ttl_days)
    return data if isinstance(data, list) else []


def currency_rates(dicts: dict[str, Any]) -> dict[str, float]:
    rates: dict[str, float] = {"RUR": 1.0, "RUB": 1.0}
    for item in dicts.get("currency") or []:
        if isinstance(item, dict) and item.get("code") and item.get("rate"):
            try:
                rates[str(item["code"]).upper()] = float(item["rate"])
            except (TypeError, ValueError):
                continue
    return rates


def _options(dicts: dict[str, Any], key: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in dicts.get(key) or []:
        if isinstance(item, dict) and item.get("id") is not None:
            out.append({"id": str(item["id"]), "name": str(item.get("name") or item["id"])})
    return out


async def search_form_options(session: AsyncSession, client: HHClient, ttl_days: int = 7) -> dict[str, Any]:
    dicts = await dictionaries(session, client, ttl_days)
    inds = await industries(session, client, ttl_days)
    currencies = [
        {"id": str(c.get("code")), "name": str(c.get("name") or c.get("code"))}
        for c in (dicts.get("currency") or [])
        if isinstance(c, dict) and c.get("code") and c.get("in_use", True)
    ]
    return {
        "experience": _options(dicts, "experience"),
        "employment_form": _options(dicts, "vacancy_search_employment_form") or _options(dicts, "employment_form"),
        "work_format": _options(dicts, "work_format"),
        "work_schedule_by_days": _options(dicts, "work_schedule_by_days"),
        "vacancy_label": _options(dicts, "vacancy_label"),
        "search_field": _options(dicts, "vacancy_search_fields"),
        "currency": currencies or [{"id": "RUR", "name": "Рубли"}],
        "education": EDUCATION_OPTIONS,
        "industry": [
            {"id": str(i.get("id")), "name": str(i.get("name") or i.get("id"))}
            for i in inds
            if isinstance(i, dict) and i.get("id") is not None
        ],
    }
