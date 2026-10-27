from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.session import make_engine, make_session_factory, run_migrations_sync
from app.hh.auth import (
    build_authorize_url,
    exchange_code,
    generate_pkce,
    pkce_challenge,
    refresh_tokens,
    revoke_token,
)
from app.hh.errors import HHOAuthTokenError
from app.hh.models import TokenResponse
from app.services.state import DbTokenStore, get_hh_auth, save_tokens

TOKEN_URL = "https://api.hh.ru/token"


def test_pkce_rfc7636_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_generate_pkce_shape() -> None:
    verifier, challenge = generate_pkce()
    assert 43 <= len(verifier) <= 128
    assert challenge == pkce_challenge(verifier)
    assert "=" not in challenge


def test_authorize_url(settings: Settings) -> None:
    url = build_authorize_url(settings, "st4te", "ch4llenge")
    parsed = urlparse(url)
    assert parsed.scheme == "https" and parsed.netloc == "hh.ru" and parsed.path == "/oauth/authorize"
    q = parse_qs(parsed.query)
    assert q["response_type"] == ["code"]
    assert q["client_id"] == ["cid"]
    assert q["state"] == ["st4te"]
    assert q["redirect_uri"] == ["http://localhost:8000/api/auth/hh/callback"]
    assert q["code_challenge"] == ["ch4llenge"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["role"] == ["applicant"]
    assert q["force_role"] == ["true"]
    assert q["skip_choose_account"] == ["true"]


@respx.mock
async def test_exchange_code_posts_form(settings: Settings) -> None:
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "a", "refresh_token": "r", "expires_in": 100, "token_type": "bearer"}
        )
    )
    async with httpx.AsyncClient() as http:
        tokens = await exchange_code(http, settings, "c0de", "ver1fier")
    assert tokens.access_token == "a" and tokens.refresh_token == "r"
    req = route.calls.last.request
    assert req.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
    assert req.headers["HH-User-Agent"] == settings.hh_user_agent
    form = parse_qs(req.content.decode())
    assert form["grant_type"] == ["authorization_code"]
    assert form["client_id"] == ["cid"] and form["client_secret"] == ["csecret"]
    assert form["code"] == ["c0de"] and form["code_verifier"] == ["ver1fier"]
    assert form["redirect_uri"] == [settings.hh_redirect_uri]


@respx.mock
async def test_refresh_form_minimal(settings: Settings) -> None:
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "a2", "refresh_token": "r2", "expires_in": 5})
    )
    async with httpx.AsyncClient() as http:
        tokens = await refresh_tokens(http, settings, "r1")
    assert tokens.access_token == "a2"
    form = parse_qs(route.calls.last.request.content.decode())
    assert form == {"grant_type": ["refresh_token"], "refresh_token": ["r1"]}


@respx.mock
async def test_refresh_invalid_client_fallback_adds_credentials(settings: Settings) -> None:
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(
                400, json={"error": "invalid_client", "error_description": "client_id or client_secret not found"}
            ),
            httpx.Response(200, json={"access_token": "a3", "refresh_token": "r3", "expires_in": 5}),
        ]
    )
    async with httpx.AsyncClient() as http:
        tokens = await refresh_tokens(http, settings, "r1")
    assert tokens.access_token == "a3"
    form = parse_qs(route.calls[1].request.content.decode())
    assert form["client_id"] == ["cid"]


@respx.mock
async def test_token_error_not_expired(settings: Settings) -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant", "error_description": "token not expired"})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(HHOAuthTokenError) as exc:
            await refresh_tokens(http, settings, "r1")
    assert exc.value.not_expired_yet


@respx.mock
async def test_revoke_falls_back_to_legacy_path(settings: Settings) -> None:
    respx.delete("https://api.hh.ru/token").mock(return_value=httpx.Response(404))
    legacy = respx.delete("https://api.hh.ru/oauth/token").mock(return_value=httpx.Response(204))
    async with httpx.AsyncClient() as http:
        assert await revoke_token(http, settings, "tok") is True
    assert legacy.calls.last.request.headers["Authorization"] == "Bearer tok"


# ---- DbTokenStore -----------------------------------------------------------


@pytest.fixture
async def session_factory(settings: Settings):
    run_migrations_sync(settings)
    engine = make_engine(settings.db_url)
    sf = make_session_factory(engine)
    yield sf
    await engine.dispose()


async def _seed(sf, expires_in: int = 3600, expired: bool = False) -> None:
    async with sf() as session:
        await save_tokens(session, TokenResponse(access_token="old", refresh_token="ref", expires_in=expires_in), "u1")
        if expired:
            row = await get_hh_auth(session)
            assert row is not None
            row.expires_at = utcnow() - timedelta(minutes=5)
        await session.commit()


@respx.mock
async def test_store_uses_valid_token_without_refresh(settings: Settings, session_factory) -> None:
    await _seed(session_factory)
    async with httpx.AsyncClient() as http:
        store = DbTokenStore(session_factory, settings, http)
        assert await store.get_access_token() == "old"
    assert not respx.calls  # no network


@respx.mock
async def test_store_refreshes_expired_token(settings: Settings, session_factory) -> None:
    await _seed(session_factory, expired=True)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "new", "refresh_token": "ref2", "expires_in": 1000})
    )
    async with httpx.AsyncClient() as http:
        store = DbTokenStore(session_factory, settings, http)
        assert await store.get_access_token() == "new"
    async with session_factory() as session:
        row = await get_hh_auth(session)
        assert row is not None
        assert row.access_token == "new" and row.refresh_token == "ref2" and row.state == "ok"
        assert (as_utc(row.expires_at) or utcnow()) > utcnow()


@respx.mock
async def test_store_keeps_token_when_hh_says_not_expired(settings: Settings, session_factory) -> None:
    await _seed(session_factory, expired=True)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant", "error_description": "token not expired"})
    )
    async with httpx.AsyncClient() as http:
        store = DbTokenStore(session_factory, settings, http)
        assert await store.refresh() == "old"


@respx.mock
async def test_store_marks_expired_on_refresh_failure(settings: Settings, session_factory) -> None:
    await _seed(session_factory, expired=True)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant", "error_description": "token was revoked"})
    )
    async with httpx.AsyncClient() as http:
        store = DbTokenStore(session_factory, settings, http)
        assert await store.refresh() is None
    async with session_factory() as session:
        row = await get_hh_auth(session)
        assert row is not None and row.state == "expired"


async def test_store_on_auth_failure_revoked(settings: Settings, session_factory) -> None:
    await _seed(session_factory)
    async with httpx.AsyncClient() as http:
        store = DbTokenStore(session_factory, settings, http)
        await store.on_auth_failure("token_revoked")
        assert await store.get_access_token() is None
    async with session_factory() as session:
        row = await get_hh_auth(session)
        assert row is not None and row.state == "revoked"


@respx.mock
async def test_app_token_cached_in_memory_and_rate_limited(settings: Settings, session_factory) -> None:
    """client_credentials token: fetched once per process, forced re-fetch obeys the 5-minute rule, no DB writes."""
    route = respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "app-1", "token_type": "bearer"}),
            httpx.Response(200, json={"access_token": "app-2", "token_type": "bearer"}),
        ]
    )
    async with httpx.AsyncClient() as http:
        store = DbTokenStore(session_factory, settings, http)
        assert await store.get_app_token() == "app-1"
        assert await store.get_app_token() == "app-1" and route.call_count == 1
        assert route.calls.last.request.content.decode().startswith("grant_type=client_credentials")
        # forced refresh within 5 minutes keeps the old token (hh rule: ≤ once per 5 minutes)
        assert await store.get_app_token(force=True) == "app-1" and route.call_count == 1
        store._app_token_attempt_at = utcnow() - timedelta(minutes=6)
        assert await store.get_app_token(force=True) == "app-2" and route.call_count == 2
    async with session_factory() as session:
        from app.services.state import get_setting

        assert await get_setting(session, "hh_app_token") is None  # nothing persisted


@respx.mock
async def test_app_token_failure_returns_none(settings: Settings, session_factory) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_client"}))
    async with httpx.AsyncClient() as http:
        store = DbTokenStore(session_factory, settings, http)
        assert await store.get_app_token() is None
        assert await store.get_app_token() is None  # rate-limited: no second request
    assert len(respx.calls) == 1
