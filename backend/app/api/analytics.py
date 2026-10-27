"""Analytics of vacancy segments (page «Аналитика»): segments, hot vacancies, skills, salary, funnel, employers,
trends. All reports take `days` (0 = the whole base) and, where a slice makes sense, `by` + `key` from
`/analytics/segments`. The reports share one base (`an.cached_base`), are computed in the analytics worker pool
(the page asks nine of them at once, and the panel must keep answering meanwhile) and are kept with the base until
the data changes."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_runtime, get_session
from app.config import Settings
from app.runtime import Runtime
from app.services import analytics as an
from app.services.audit import audit

router = APIRouter(prefix="/analytics", tags=["analytics"])

DaysQ = Annotated[int, Query(ge=0, le=3650, description="0 = вся база")]
ScopeQ = Annotated[str, Query(description="passed (прошли правила) | all | evaluated")]


def _check_kind(by: str | None) -> None:
    if by and by not in an.SEGMENT_KINDS:
        raise HTTPException(status_code=400, detail=f"by должен быть одним из {', '.join(an.SEGMENT_KINDS)}")


def _check_scope(scope: str) -> None:
    if scope not in an.SCOPES:
        raise HTTPException(status_code=400, detail=f"scope должен быть одним из {', '.join(an.SCOPES)}")


async def _base(runtime: Runtime, *, days: int = 0, scope: str = "passed") -> an.Base:
    return await an.cached_base(runtime.session_factory, runtime.settings, days=days, scope=scope)


def _key_part(value: Any) -> Any:
    if isinstance(value, Settings):  # the thresholds the reports read from the settings
        return (value.prescore_min_for_claude, value.score_threshold_maybe)
    return getattr(value, "__name__", value) if callable(value) else value


async def _report(base: an.Base, fn: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
    """`fn(base, *args, **kwargs)` in the analytics pool; the result is kept with the base, so the page opened
    again (or another tab asking the same report) gets it at once."""
    key = (fn.__name__, *(_key_part(a) for a in args), *sorted((k, _key_part(v)) for k, v in kwargs.items()))
    result: dict[str, Any] = await an.run_cpu(base.report, key, partial(fn, base, *args, **kwargs))
    return result


def _segment_report(
    base: an.Base, fn: Callable[..., dict[str, Any]], by: str | None, key: str | None, **kwargs: Any
) -> dict[str, Any]:
    """A report over the vacancies of one segment (`by` + `key`; all of them without a slice)."""
    return fn(an.select_segment(base, by, key), base, **kwargs)


@router.get("/kinds")
async def kinds() -> dict[str, Any]:
    return {
        "segments": [{"id": k, "label": v} for k, v in an.SEGMENT_KINDS.items()],
        "funnel": [{"id": k, "label": v} for k, v in an.FUNNEL_DIMENSIONS.items()],
        "scopes": [{"id": k, "label": v} for k, v in an.SCOPES.items()],
    }


@router.get("/segments")
async def segments(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    by: str = Query("search"),
    days: DaysQ = 0,
    scope: ScopeQ = "passed",
) -> dict[str, Any]:
    _check_kind(by)
    _check_scope(scope)
    base = await _base(runtime, days=days, scope=scope)
    return {"days": days, "scope": scope, **await _report(base, an.segments_report, by)}


@router.get("/hot")
async def hot(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    max_responses: int = Query(50, ge=0, le=100_000),
    max_age_days: int = Query(3, ge=1, le=60),
    min_score: int = Query(50, ge=0, le=100),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    base = await _base(runtime, scope="all")
    return await _report(
        base,
        an.hot_report,
        runtime.settings,
        max_responses=max_responses,
        max_age_days=max_age_days,
        min_score=min_score,
        limit=limit,
    )


@router.get("/skills")
async def skills(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    by: str | None = None,
    key: str | None = None,
    days: DaysQ = 0,
    scope: ScopeQ = "passed",
) -> dict[str, Any]:
    _check_kind(by)
    _check_scope(scope)
    base = await _base(runtime, days=days, scope=scope)
    return {"by": by, "key": key, "days": days, **await _report(base, _segment_report, an.skills_report, by, key)}


@router.get("/salary")
async def salary(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    by: str | None = None,
    key: str | None = None,
    days: DaysQ = 0,
    scope: ScopeQ = "passed",
) -> dict[str, Any]:
    _check_kind(by)
    _check_scope(scope)
    base = await _base(runtime, days=days, scope=scope)
    return {"by": by, "key": key, "days": days, **await _report(base, _segment_report, an.salary_report, by, key)}


@router.get("/funnel")
async def funnel(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    by: str = Query("search"),
    days: DaysQ = 0,
) -> dict[str, Any]:
    if by not in an.FUNNEL_DIMENSIONS:
        raise HTTPException(status_code=400, detail=f"by должен быть одним из {', '.join(an.FUNNEL_DIMENSIONS)}")
    base = await _base(runtime, days=days, scope="all")
    return {"days": days, **await _report(base, an.funnel_report, by)}


@router.get("/employers")
async def employers(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    by: str | None = None,
    key: str | None = None,
    days: DaysQ = 0,
    scope: ScopeQ = "passed",
    limit: int = Query(30, ge=1, le=200),
) -> dict[str, Any]:
    _check_kind(by)
    _check_scope(scope)
    base = await _base(runtime, days=days, scope=scope)
    report = await _report(base, _segment_report, an.employers_report, by, key, limit=limit)
    return {"by": by, "key": key, "days": days, **report}


@router.get("/trends")
async def trends(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    by: str | None = None,
    key: str | None = None,
    weeks: int = Query(12, ge=2, le=52),
    scope: ScopeQ = "passed",
) -> dict[str, Any]:
    _check_kind(by)
    _check_scope(scope)
    base = await _base(runtime, scope=scope)
    return {"by": by, "key": key, **await _report(base, _segment_report, an.trends_report, by, key, weeks=weeks)}


@router.get("/recommendations")
async def recommendations(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    scope: ScopeQ = "passed",
) -> dict[str, Any]:
    """What the data suggests: search terms with a high share of good scores, stop words, weights to raise,
    skills to add to the resume, where the odds are best."""
    _check_scope(scope)
    base = await _base(runtime, scope=scope)
    return {"scope": scope, **await _report(base, an.recommendations_report)}


class SearchSuggestion(BaseModel):
    term: str = Field(..., min_length=2, max_length=120)
    query: str | None = Field(None, max_length=300)


@router.post("/recommendations/search")
async def create_suggested_search(
    body: SearchSuggestion,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Creates a disabled title search from a suggested term, copying filters from the panel's own title search."""
    from app.services.searches import SearchInput, SearchParams, create_search, list_searches, search_row

    template = None
    for s in await list_searches(session):
        params = SearchParams.model_validate_json(s.params_json or "{}")
        if s.enabled and params.search_field == ["name"] and s.selection_profile_id is None:
            template = (s, params)
            break
        if template is None and s.enabled:
            template = (s, params)
    if template is not None:
        src, params = template
        params = params.model_copy(update={"text": body.query or body.term, "search_field": ["name"]})
        interval = src.interval_min
    else:
        params = SearchParams(text=body.query or body.term, search_field=["name"], work_format=["REMOTE"])
        interval = 30
    name = f"{body.term} (по названию)"
    if any(s.name == name for s in await list_searches(session)):
        raise HTTPException(status_code=409, detail=f"Поиск «{name}» уже есть")
    row = await create_search(session, SearchInput(name=name, params=params, interval_min=interval, enabled=False))
    await audit(session, "analytics", f"Поиск «{name}» создан по рекомендации (выключен)")
    await session.commit()
    return search_row(row)


class Names(BaseModel):
    names: list[str] = Field(..., min_length=1, max_length=50)


@router.post("/recommendations/weights")
async def apply_weights(
    body: Names,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Raises the questionnaire weight of the given skills to 5 (adds them when missing)."""
    from app.services.preferences import SkillWeight, get_preferences
    from app.services.profile import update_preferences
    from app.services.textmatch import terms_equal

    prefs = await get_preferences(session)
    weights = list(prefs.skills_weights)
    changed: list[str] = []
    for name in body.names:
        name = name.strip()
        if not name:
            continue
        hit = next((w for w in weights if terms_equal(w.name, name)), None)
        if hit is None:
            weights.append(SkillWeight(name=name, weight=5))
        elif hit.weight < 5:
            hit.weight = 5
        else:
            continue
        changed.append(name)
    if changed:
        prefs.skills_weights = weights
        await update_preferences(session, runtime.settings, prefs)
        await audit(session, "analytics", f"Веса навыков подняты до 5 по рекомендации: {', '.join(changed)}")
        await session.commit()
    return {"changed": changed}


@router.post("/recommendations/stop-words")
async def apply_stop_words(
    body: Names,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Adds the given words to the title stop words of the questionnaire."""
    from app.services.preferences import get_preferences
    from app.services.profile import update_preferences
    from app.services.textmatch import norm

    prefs = await get_preferences(session)
    known = {norm(w) for w in prefs.stop_words_title}
    added: list[str] = []
    for word in (w.strip() for w in body.names):
        if word and norm(word) not in known:
            added.append(word)
            known.add(norm(word))
    if added:
        prefs.stop_words_title = [*prefs.stop_words_title, *added]
        await update_preferences(session, runtime.settings, prefs)
        await audit(session, "analytics", f"Стоп-слова добавлены по рекомендации: {', '.join(added)}")
        await session.commit()
    return {"added": added}


@router.get("/borderline")
async def borderline(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    prescore_margin: int = Query(5, ge=1, le=30),
    score_margin: int = Query(5, ge=1, le=30),
    limit: int = Query(60, ge=1, le=300),
) -> dict[str, Any]:
    """Vacancies just under the prescore or the Claude «maybe» threshold — a quick look by eye replenishes the
    funnel when the searches run dry."""
    base = await _base(runtime, scope="all")
    return await _report(
        base,
        an.borderline_report,
        runtime.settings,
        prescore_margin=prescore_margin,
        score_margin=score_margin,
        limit=limit,
    )


class AdviceRequest(BaseModel):
    by: str | None = None
    key: str | None = Field(None, max_length=200)
    notes: str | None = Field(None, max_length=2000)


@router.post("/resume-advice")
async def resume_advice(
    body: AdviceRequest,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """Queues a `resume_advice` task for the segment: concrete edits of the resume text come back with the packet."""
    from app.services.resume_advice import advice_row, list_advices, request_resume_advice

    _check_kind(body.by)
    row, batch_id = await request_resume_advice(session, runtime.settings, by=body.by, key=body.key, notes=body.notes)
    await session.commit()
    return {"advice": advice_row(row), "batch_id": batch_id, "advices": await list_advices(session)}


@router.get("/resume-advice")
async def resume_advices(session: Annotated[AsyncSession, Depends(get_session)]) -> list[dict[str, Any]]:
    from app.services.resume_advice import list_advices

    return await list_advices(session)


@router.post("/hot-digest")
async def send_hot_digest(runtime: Annotated[Runtime, Depends(get_runtime)]) -> dict[str, Any]:
    """Sends the hot-vacancies digest now (the same event the morning schedule sends), ignoring the repeat memory."""
    from app.services.hot_digest import hot_digest_tick
    from app.services.telegram import flush_now

    result = await hot_digest_tick(runtime, force=True)
    if result.get("ok") and result.get("telegram"):
        await flush_now(runtime)
    return result


@router.post("/retag")
async def retag(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, Any]:
    """Recomputes the rule tags of every vacancy (after a vocabulary change or for vacancies scanned before tags)."""
    n = await an.retag_all(session)
    await audit(session, "analytics", f"Метки пересчитаны: {n} вакансий")
    await session.commit()
    return {"retagged": n}
