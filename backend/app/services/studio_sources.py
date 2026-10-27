"""Where studios come from (job `studios_find`): hh.ru employer search, the employers of the panel's vacancies, Telegram
channel posts. The user's own list is imported by services/studios.import_list.

hh.ru: `GET /employers` (text in the name and description, `area`, `type=company` — no recruiting agencies,
`only_with_vacancies`, `sort_by=by_vacancies_open`, at most 5000 results deep) and `GET /employers/{id}` (site_url,
description, industries, area, open_vacancies) — both in the public specification (https://api.hh.ru/openapi/redoc).
A card counts as a studio when its industries are development or design ones (STUDIO_INDUSTRIES), it is not a
recruiting agency and it does digital work (studios.digital_profile: «студия дизайна» on hh.ru is mostly an interior
studio); an employer of the panel's vacancies must also describe itself as a service company (studio, agency,
outsourcing, «под ключ», clients). Employers found not to be studios are remembered for REJECT_TTL_DAYS so the next
run does not ask hh.ru about them again. A social network in the card's `site_url` is no site (studios.studio_site).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select

from app.db.base import as_utc, utcnow
from app.db.models import ChannelPost, HiddenEmployer, Studio, Vacancy
from app.db.session import release_writes
from app.hh.errors import HHAuthError, HHCaptchaRequired, HHError
from app.hh.textclean import html_to_text
from app.services.audit import audit
from app.services.jobs import FAILED_KEY
from app.services.preferences import get_preferences
from app.services.rules import looks_like_agency
from app.services.state import get_setting, set_setting
from app.services.studios import (
    CAREER_SUBDOMAINS,
    RELEVANT_STATUSES,
    digital_profile,
    looks_like_studio,
    refresh_fit,
    site_parts,
    studio_by_name,
    studio_settings,
    studio_site,
    upsert_studio,
)
from app.services.textmatch import norm

log = structlog.get_logger("studio_sources")

JOB = "studios_find"
SOURCES = ("hh_search", "hh_vacancies", "channels")
# hh.ru industries of development and design studios (the /industries dictionary)
STUDIO_INDUSTRIES = {
    "7.539",  # системная интеграция, автоматизация, ИТ-консалтинг
    "7.540",  # разработка программного обеспечения
    "7.541",  # интернет-компания (в том числе продвижение сайтов)
    "11.453",  # производство мультимедиа, контента
    "11.459",  # маркетинговые, рекламные, BTL, дизайнерские, Event-, PR-агентства
}
RECRUITING_INDUSTRIES = {"44.391"}  # кадровые агентства
RUSSIA_AREA = "113"
PER_PAGE = 100
SEARCH_PAGES = 2
REJECTED_KEY = "studios_hh_rejected"  # {employer_id: ISO date} — checked, not a studio
REJECT_TTL_DAYS = 60
# a studio card from hh.ru is not asked again for this long (the search still refreshes open_vacancies)
DETAIL_TTL_DAYS = 30
RECRUITING_WORDS = ("рекрут", "recruit", "кадров", "hr-агентств", "подбор персонала", "staffing")


@dataclass
class FindSummary:
    added: int = 0
    updated: int = 0
    rejected: int = 0
    hh_requests: int = 0
    errors: list[str] = field(default_factory=list)
    aborted: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "added": self.added,
            "updated": self.updated,
            "rejected": self.rejected,
            "hh_requests": self.hh_requests,
            "errors": self.errors[:10],
            "aborted": self.aborted,
        }


# ---- hh.ru -----------------------------------------------------------------------------------------------------------


def _industry_ids(card: dict[str, Any]) -> list[str]:
    return [str(i.get("id")) for i in card.get("industries") or [] if isinstance(i, dict) and i.get("id")]


def is_studio_card(card: dict[str, Any], *, strict: bool) -> bool:
    """`strict` for employers of the panel's vacancies: they were not found by a studio keyword, so the card must
    say so itself. Either way the studio must do digital work (studios.digital_profile): no interior studios."""
    industries = set(_industry_ids(card))
    if industries & RECRUITING_INDUSTRIES or card.get("type") == "agency":
        return False
    name = str(card.get("name") or "")
    if looks_like_agency(name):
        return False
    text = " ".join([name, html_to_text(str(card.get("description") or ""))[:4000]])
    if not digital_profile(text, sorted(industries)):
        return False
    if not industries:
        return looks_like_studio(text)
    if not industries & STUDIO_INDUSTRIES:
        return False
    return (looks_like_studio(text) or studio_by_name(name)) if strict else True


async def _rejected(session: Any, now: datetime) -> dict[str, str]:
    saved = await get_setting(session, REJECTED_KEY)
    if not isinstance(saved, dict):
        return {}
    cutoff = now - timedelta(days=REJECT_TTL_DAYS)
    out: dict[str, str] = {}
    for key, value in saved.items():
        try:
            if datetime.fromisoformat(str(value)) >= cutoff:
                out[str(key)] = str(value)
        except ValueError:
            continue
    return out


async def _hidden_employers(session: Any) -> tuple[set[str], set[str]]:
    rows = (await session.execute(select(HiddenEmployer.employer_id, HiddenEmployer.employer_name))).all()
    return {str(i) for i, _ in rows if i}, {str(n).strip().lower() for _, n in rows if n}


async def _save_card(session: Any, card: dict[str, Any], source: str, summary: FindSummary, prefs: Any) -> None:
    raw_area = card.get("area")
    area: dict[str, Any] = raw_area if isinstance(raw_area, dict) else {}
    studio, created = await upsert_studio(
        session,
        name=str(card.get("name") or ""),
        source=source,
        site_url=str(card.get("site_url") or "") or None,
        hh_employer_id=str(card.get("id")),
        area=str(area.get("name") or "") or None,
        description=html_to_text(str(card.get("description") or ""))[:3000] or None,
        open_vacancies=int(card["open_vacancies"]) if isinstance(card.get("open_vacancies"), int | float) else None,
        industries=_industry_ids(card),
    )
    await refresh_fit(session, studio, prefs)
    if created:
        summary.added += 1
    else:
        summary.updated += 1


async def _fetch_cards(
    session: Any,
    client: Any,
    ids: list[str],
    source: str,
    summary: FindSummary,
    budget: list[int],
    *,
    strict: bool,
    now: datetime,
) -> None:
    """Employer cards → studios; the rest is remembered as rejected. `budget` is the run's remaining card count."""
    rejected = await _rejected(session, now)
    hidden_ids, hidden_names = await _hidden_employers(session)
    fresh_cutoff = now - timedelta(days=DETAIL_TTL_DAYS)
    known = {
        str(i): seen
        for i, seen in (
            await session.execute(
                select(Studio.hh_employer_id, Studio.last_seen_at).where(Studio.hh_employer_id.in_(ids))
            )
        ).all()
    }
    prefs = await get_preferences(session)
    for employer_id in ids:
        if budget[0] <= 0:
            break
        seen = known.get(employer_id)
        if employer_id in rejected or employer_id in hidden_ids:
            continue
        if seen is not None and (as_utc(seen) or now) >= fresh_cutoff:
            continue
        budget[0] -= 1
        await release_writes(session)  # no open write transaction across the hh request (one SQLite writer)
        try:
            card = await client.public(f"/employers/{employer_id}")
        except (HHAuthError, HHCaptchaRequired):
            raise
        except HHError as exc:
            summary.errors.append(f"работодатель {employer_id}: {exc.summary()}")
            continue
        finally:
            summary.hh_requests += 1
        if not isinstance(card, dict):
            continue
        name = str(card.get("name") or "").strip().lower()
        if "blacklisted" in (card.get("relations") or []) or name in hidden_names:
            continue
        if is_studio_card(card, strict=strict):
            await _save_card(session, card, source, summary, prefs)
        else:
            rejected[employer_id] = now.isoformat(timespec="seconds")
            summary.rejected += 1
    await set_setting(session, REJECTED_KEY, rejected)


async def find_on_hh(
    session: Any, client: Any, settings: dict[str, Any], summary: FindSummary, budget: list[int], now: datetime
) -> None:
    """Employer search by the keywords of the Studios settings, all of Russia, companies only."""
    ids: list[str] = []
    open_counts: dict[str, int] = {}
    for keyword in settings["keywords"]:
        for page in range(SEARCH_PAGES):
            params: dict[str, Any] = {
                "text": keyword,
                "area": RUSSIA_AREA,
                "type": "company",
                "sort_by": "by_vacancies_open",
                "per_page": PER_PAGE,
                "page": page,
            }
            if settings["only_with_vacancies"]:
                params["only_with_vacancies"] = "true"
            await release_writes(session)
            try:
                data = await client.public("/employers", params=params)
            except (HHAuthError, HHCaptchaRequired):
                raise
            except HHError as exc:
                summary.errors.append(f"поиск «{keyword}»: {exc.summary()}")
                break
            finally:
                summary.hh_requests += 1
            items = data.get("items") if isinstance(data, dict) else None
            for item in items or []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                employer_id = str(item["id"])
                if employer_id not in open_counts:
                    ids.append(employer_id)
                if isinstance(item.get("open_vacancies"), int | float):
                    open_counts[employer_id] = int(item["open_vacancies"])
            pages = int(data.get("pages") or 0) if isinstance(data, dict) else 0
            if page + 1 >= pages:
                break
    # the search refreshes the vacancy counters of studios already known without asking for their cards
    for studio in (await session.execute(select(Studio).where(Studio.hh_employer_id.in_(list(open_counts))))).scalars():
        studio.open_vacancies = open_counts.get(str(studio.hh_employer_id), studio.open_vacancies)
    await _fetch_cards(session, client, ids, "hh_search", summary, budget, strict=False, now=now)


async def find_among_vacancies(
    session: Any, client: Any, summary: FindSummary, budget: list[int], now: datetime
) -> None:
    """Employers of the panel's vacancies that passed the rules or were evaluated — they hire people like the user —
    the ones with most such vacancies first."""
    rows = (
        await session.execute(
            select(Vacancy.employer_id, func.count(Vacancy.id).label("n"))
            .where(Vacancy.employer_id.isnot(None), Vacancy.pipeline_status.in_(RELEVANT_STATUSES))
            .group_by(Vacancy.employer_id)
            .order_by(func.count(Vacancy.id).desc())
        )
    ).all()
    ids = [str(r.employer_id) for r in rows if r.employer_id]
    await _fetch_cards(session, client, ids, "hh_vacancies", summary, budget, strict=True, now=now)


# ---- Telegram channels -----------------------------------------------------------------------------------------------


# words of a position or a headline: «Workflow Specialist — SoFi» puts the role first, «Вакансия: …» is a headline
ROLE_WORDS = re.compile(
    r"(?<![a-zа-я])(?:разработчик\w*|developer|engineer|инженер\w*|designer|дизайнер\w*|менеджер\w*|manager"
    r"|специалист\w*|specialist|аналитик\w*|analyst|lead|head|директор\w*|director|архитектор\w*|architect"
    r"|junior|middle|senior|стажер\w*|intern|ваканси\w*|hiring|seeking|ищем|требуется|подборка|position)(?![a-zа-я])",
    re.IGNORECASE,
)
# a job board or an applicant tracking system under its own domain (cryptojobs.example, remotecareers.io)
BOARD_HOST = re.compile(r"jobs?|careers?|hiring|vacanc|talents?|recruit")


def _post_company(title: str) -> str | None:
    """Channel post titles read «Компания — Должность»; a title without the dash or whose head is a position or a
    headline gives no company."""
    parts = re.split(r"\s+[—–-]\s+", title or "", maxsplit=1)
    if len(parts) < 2:
        return None
    head = parts[0].strip()
    if not 1 < len(head) <= 60 or ROLE_WORDS.search(norm(head)):
        return None
    return head


def _external_site(links: list[str]) -> str | None:
    """The studio's own site among the post's links (studio_site: no social networks and job boards, a careers
    subdomain gives the company's site)."""
    for link in links:
        _, host = site_parts(link)
        site, domain = studio_site(link)
        if not host or not domain:
            continue
        if BOARD_HOST.search(host.rsplit(".", 1)[0]) and not host.startswith(CAREER_SUBDOMAINS):
            continue  # cryptojobs.example, remotework.io …
        return site
    return None


async def find_in_channels(session: Any, summary: FindSummary) -> None:
    """Posts of studios (a studio marker in the text) that link to the studio's own site; recruiters are skipped."""
    prefs = await get_preferences(session)
    posts = (await session.execute(select(ChannelPost).order_by(ChannelPost.id.desc()).limit(3000))).scalars().all()
    for post in posts:
        text = f"{post.title}\n{post.text}"
        low = text.lower()
        if not looks_like_studio(text) or any(w in low for w in RECRUITING_WORDS) or not digital_profile(text):
            continue
        company = _post_company(post.title)
        if not company or looks_like_agency(company):
            continue
        try:
            links = [str(u) for u in json.loads(post.links_json or "[]")]
        except ValueError:
            links = []
        site = _external_site(links)
        if site is None:
            continue
        studio, created = await upsert_studio(
            session, name=company, source="channel", site_url=site, description=(post.text or "")[:1500] or None
        )
        await refresh_fit(session, studio, prefs)
        if created:
            summary.added += 1
        else:
            summary.updated += 1


# ---- the job ---------------------------------------------------------------------------------------------------------


async def find_studios(runtime: Any, sources: tuple[str, ...] = SOURCES) -> dict[str, Any]:
    """Job `studios_find`: every requested source in turn; the hh sources share STUDIOS_HH_DETAILS_PER_RUN cards."""
    now = utcnow()
    client = runtime.hh_client()
    budget = [max(0, runtime.settings.studios_hh_details_per_run)]
    result: dict[str, Any] = {}
    failed: str | None = None
    async with runtime.session_factory() as session:
        settings = await studio_settings(session)
        for source in sources:
            summary = FindSummary()
            try:
                if source == "hh_search":
                    await find_on_hh(session, client, settings, summary, budget, now)
                elif source == "hh_vacancies":
                    await find_among_vacancies(session, client, summary, budget, now)
                elif source == "channels":
                    await find_in_channels(session, summary)
            except (HHAuthError, HHCaptchaRequired) as exc:
                summary.aborted = exc.summary()
                failed = f"hh.ru: {exc.summary()}"
            await session.commit()
            result[source] = summary.as_dict()
            if failed:
                break
        added = sum(int(r["added"]) for r in result.values())
        updated = sum(int(r["updated"]) for r in result.values())
        await audit(
            session,
            "studios",
            f"Поиск студий: новых {added}, обновлено {updated}" + (f" (прервано: {failed})" if failed else ""),
            level="warning" if failed else "info",
        )
        from app.services.counters import bump_counter

        await bump_counter(session, runtime.settings, "hh_requests", client.requests_made)
        await session.commit()
    if failed:
        result[FAILED_KEY] = failed
    return result
