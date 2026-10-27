"""Smoke всех маршрутов API на пустой базе: ни один GET/POST/PUT/DELETE не должен отвечать 500 (hh.ru и внешние сайты
замоканы «503», поэтому 502 от прокси-ошибок hh — ожидаемый ответ, а 500 — дефект панели)."""

from __future__ import annotations

import re

import httpx
import pytest
import respx
from httpx import ASGITransport

from app.config import Settings
from app.main import app
from app.runtime import Runtime

# маршруты, которые нельзя дёргать вслепую (запускают внешние процессы / ждут долго)
SKIP = {("POST", "/api/claude/run")}


@pytest.fixture
async def api(settings: Settings):
    settings = settings.model_copy(update={"batch_auto_create": False, "sites_sync_min": 0, "channel_sync_min": 0})
    runtime = await Runtime.create(settings)
    app.state.runtime = runtime
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


def _routes() -> list[tuple[str, str]]:
    """(METHOD, path) всех маршрутов /api/* по OpenAPI-схеме приложения (FastAPI 0.141 прячет роутеры в
    `_IncludedRouter`, поэтому `app.routes` напрямую не годится)."""
    out: list[tuple[str, str]] = []
    for path, methods in app.openapi()["paths"].items():
        if not path.startswith("/api/"):
            continue
        for method in methods:
            out.append((method.upper(), path))
    return sorted(set(out))


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


@respx.mock(assert_all_called=False)
async def test_every_route_survives_an_empty_database(api, respx_mock: respx.MockRouter) -> None:
    respx_mock.route().mock(return_value=httpx.Response(503, json={"errors": [{"type": "service_unavailable"}]}))
    client, _runtime = api
    routes = _routes()
    assert len(routes) > 100, "ожидались все маршрутизаторы панели"
    failures: list[str] = []
    for method, path in routes:
        if (method, path) in SKIP:
            continue
        url = _fill(path)
        kwargs = {} if method in ("GET", "DELETE") else {"json": {}}
        try:
            r = await client.request(method, url, **kwargs)
        except Exception as exc:  # noqa: BLE001 - собираем всё, чтобы показать полный список
            failures.append(f"{method} {url}: exception {type(exc).__name__}: {exc}")
            continue
        if r.status_code == 500:
            failures.append(f"{method} {url}: 500 {r.text[:200]}")
    assert failures == [], "\n".join(failures)


async def test_get_routes_return_json_or_text(api) -> None:
    client, _ = api
    for method, path in _routes():
        if method != "GET" or "{" in path or "suggest" in path or "login" in path:
            continue
        r = await client.get(path)
        assert r.status_code in (200, 302, 400, 404, 409, 422, 502), (path, r.status_code)
        if r.status_code == 200:
            ctype = r.headers.get("content-type", "")
            assert ctype.startswith(("application/json", "text/plain", "text/csv")), (path, ctype)


# ---- every JSON body route with a valid body -------------------------------------------------


def _resolve(schema: dict, spec: dict) -> dict:
    while "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].rsplit("/", 1)[1]]
    return schema


def _sample(schema: dict, spec: dict, *, full: bool) -> object:
    """A value the schema accepts: only the required fields (full=False) or every field (full=True). The API schemas use
    no regex patterns and only the date-time format (checked in the test), so plain values do."""
    schema = _resolve(schema, spec)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][-1 if full else 0]
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            options = [o for o in schema[key] if _resolve(o, spec).get("type") != "null"]
            return _sample(options[0], spec, full=full) if options else None
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        required = set(schema.get("required", []))
        return {
            name: _sample(prop, spec, full=full)
            for name, prop in schema.get("properties", {}).items()
            if full or name in required
        }
    if kind == "array":
        count = max(int(schema.get("minItems", 0)), 1 if full else 0)
        return [_sample(schema.get("items", {}), spec, full=full) for _ in range(count)]
    if kind == "string":
        if schema.get("format") == "date-time":
            return "2026-09-20T12:00:00+03:00"
        text = "Python разработчик, FastAPI и PostgreSQL" if full else "Python"
        low, high = int(schema.get("minLength", 0)), int(schema.get("maxLength", 10_000))
        while len(text) < low:
            text += " текст"
        return text[: max(low, min(high, len(text)))]
    if kind in ("integer", "number"):
        low = schema.get("minimum", schema.get("exclusiveMinimum", 0) + 1 if "exclusiveMinimum" in schema else 1)
        value = max(low, 1) if full else low
        if "maximum" in schema:
            value = min(value, schema["maximum"])
        return int(value) if kind == "integer" else float(value)
    if kind == "boolean":
        return full
    return {}


def _json_body_routes(spec: dict) -> list[tuple[str, str, dict]]:
    out = []
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            content = (op.get("requestBody") or {}).get("content", {})
            if path.startswith("/api/") and "application/json" in content:
                out.append((method.upper(), path, content["application/json"]["schema"]))
    return sorted(out, key=lambda r: (r[1], r[0]))


async def _seed_one_of_each(runtime: Runtime) -> None:
    """Row id 1 of everything a route addresses by id, so the handlers run past their «not found»."""
    from app.db.models import (
        Application,
        ChannelPost,
        CoverLetter,
        Negotiation,
        SavedSearch,
        SelectionProfile,
        TelegramChannel,
        Vacancy,
    )

    async with runtime.session_factory() as session:
        session.add(
            Vacancy(
                id=1,
                hh_id="1",
                name="Python-разработчик",
                employer_name="Рога и копыта",
                prescore=60,
                pipeline_status="suitable",
                score=80,
            )
        )
        session.add(CoverLetter(vacancy_id=1, version=1, text="Здравствуйте! " + "Письмо. " * 80, origin="claude"))
        session.add(Application(id=1, vacancy_id=1, mode="assist", status="needs_manual"))
        session.add(SavedSearch(id=1, name="Python", params_json='{"text": "python"}'))
        session.add(Negotiation(hh_id="1", vacancy_id=1))
        session.add(TelegramChannel(id=1, username="job_python"))
        session.add(ChannelPost(id=1, channel_id=1, message_id=1, text="Ищем Python-разработчика"))
        session.add(SelectionProfile(id=1, name="Набор"))
        await session.commit()


@pytest.mark.parametrize("variant", ["required", "full"])
@respx.mock(assert_all_called=False)
async def test_every_body_route_accepts_a_valid_body(api, respx_mock: respx.MockRouter, variant: str) -> None:
    respx_mock.route().mock(return_value=httpx.Response(503, json={"errors": [{"type": "service_unavailable"}]}))
    client, runtime = api
    await _seed_one_of_each(runtime)
    spec = app.openapi()
    routes = _json_body_routes(spec)
    assert len(routes) > 30, "ожидались все маршруты с JSON-телом"
    failures: list[str] = []
    for method, path, schema in routes:
        if (method, path) in SKIP:
            continue
        body = _sample(schema, spec, full=variant == "full")
        url = _fill(path)
        try:
            r = await client.request(method, url, json=body)
        except Exception as exc:  # noqa: BLE001 - собираем всё
            failures.append(f"{method} {url}: exception {type(exc).__name__}: {exc}")
            continue
        # a validation error (a list of pydantic errors) for a body built from the schema means the schema lies — a
        # hidden validator accepts less than it announces; a 422 with a sentence is the handler's own rule
        # («Нечего записывать» for an empty manual update) and is fine
        schema_refused = r.status_code == 422 and isinstance(r.json().get("detail"), list)
        if r.status_code == 500 or schema_refused:
            failures.append(f"{method} {url} {body}: {r.status_code} {r.text[:300]}")
    assert failures == [], "\n".join(failures)
