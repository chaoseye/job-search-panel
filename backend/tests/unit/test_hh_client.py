from __future__ import annotations

import httpx
import pytest
import respx

from app.hh.client import HHClient
from app.hh.errors import (
    HHAuthError,
    HHBadArgument,
    HHCaptchaRequired,
    HHNegotiationError,
    HHNotFound,
    HHServerError,
    HHTransportError,
)
from tests.conftest import FakeSleep, MemoryTokenStore

BASE = "https://api.hh.ru"


@respx.mock(base_url=BASE)
async def test_headers_and_bearer(client: HHClient, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get("/me").mock(return_value=httpx.Response(200, json={"id": "1", "is_applicant": True}))
    data = await client.get("/me")
    assert data["is_applicant"] is True
    req = route.calls.last.request
    assert req.headers["HH-User-Agent"] == "TestApp/1.0 (t@test.local)"
    assert req.headers["User-Agent"] == "TestApp/1.0 (t@test.local)"
    assert req.headers["Authorization"] == "Bearer tok-1"
    assert req.headers["Accept"] == "application/json"


@respx.mock(base_url=BASE)
async def test_no_auth_header_when_auth_false(client: HHClient, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get("/dictionaries").mock(return_value=httpx.Response(200, json={}))
    await client.get("/dictionaries", auth=False)
    assert "Authorization" not in route.calls.last.request.headers


@respx.mock(base_url=BASE)
async def test_retry_on_503_then_ok(client: HHClient, respx_mock: respx.MockRouter, fake_sleep: FakeSleep) -> None:
    route = respx_mock.get("/vacancies").mock(
        side_effect=[
            httpx.Response(503, json={"errors": [{"type": "service_unavailable"}]}),
            httpx.Response(200, json={"found": 0}),
        ]
    )
    data = await client.get("/vacancies")
    assert data == {"found": 0}
    assert route.call_count == 2
    assert fake_sleep.calls == [2.0]


@respx.mock(base_url=BASE)
async def test_retry_after_header_respected(
    client: HHClient, respx_mock: respx.MockRouter, fake_sleep: FakeSleep
) -> None:
    respx_mock.get("/vacancies").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})]
    )
    await client.get("/vacancies")
    assert fake_sleep.calls == [7.0]


@respx.mock(base_url=BASE)
async def test_gives_up_after_max_retries(
    client: HHClient, respx_mock: respx.MockRouter, fake_sleep: FakeSleep
) -> None:
    route = respx_mock.get("/vacancies").mock(return_value=httpx.Response(502))
    with pytest.raises(HHServerError):
        await client.get("/vacancies")
    assert route.call_count == 5  # 1 + 4 retries
    assert fake_sleep.calls == [2.0, 4.0, 8.0, 16.0]


@respx.mock(base_url=BASE)
async def test_transport_error_retried(client: HHClient, respx_mock: respx.MockRouter) -> None:
    respx_mock.get("/me").mock(side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={"ok": 1})])
    assert await client.get("/me") == {"ok": 1}


@respx.mock(base_url=BASE)
async def test_transport_error_exhausted(client: HHClient, respx_mock: respx.MockRouter) -> None:
    respx_mock.get("/me").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(HHTransportError):
        await client.get("/me")


@respx.mock(base_url=BASE)
async def test_bad_argument_parsed(client: HHClient, respx_mock: respx.MockRouter) -> None:
    respx_mock.get("/vacancies").mock(
        return_value=httpx.Response(400, json={"errors": [{"type": "bad_argument", "value": "text"}]})
    )
    with pytest.raises(HHBadArgument) as exc:
        await client.get("/vacancies")
    assert exc.value.field == "text"
    assert exc.value.status == 400


@respx.mock(base_url=BASE)
async def test_not_found(client: HHClient, respx_mock: respx.MockRouter) -> None:
    respx_mock.get("/vacancies/1").mock(return_value=httpx.Response(404, json={"errors": [{"type": "not_found"}]}))
    with pytest.raises(HHNotFound):
        await client.get("/vacancies/1")


@respx.mock(base_url=BASE)
async def test_token_expired_triggers_single_refresh(
    client: HHClient, respx_mock: respx.MockRouter, token_store: MemoryTokenStore
) -> None:
    route = respx_mock.get("/me").mock(
        side_effect=[
            httpx.Response(403, json={"errors": [{"type": "oauth", "value": "token_expired"}]}),
            httpx.Response(200, json={"id": "1"}),
        ]
    )
    assert await client.get("/me") == {"id": "1"}
    assert token_store.refresh_calls == 1
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok-1"
    assert route.calls[1].request.headers["Authorization"] == "Bearer tok-2"
    assert token_store.failures == []


@respx.mock(base_url=BASE)
async def test_token_expired_twice_raises(
    client: HHClient, respx_mock: respx.MockRouter, token_store: MemoryTokenStore
) -> None:
    respx_mock.get("/me").mock(
        return_value=httpx.Response(403, json={"errors": [{"type": "oauth", "value": "token_expired"}]})
    )
    with pytest.raises(HHAuthError):
        await client.get("/me")
    assert token_store.refresh_calls == 1
    assert token_store.failures == ["token_expired"]


@respx.mock(base_url=BASE)
async def test_token_revoked_reports_failure(
    client: HHClient, respx_mock: respx.MockRouter, token_store: MemoryTokenStore
) -> None:
    respx_mock.get("/me").mock(
        return_value=httpx.Response(403, json={"errors": [{"type": "oauth", "value": "token_revoked"}]})
    )
    with pytest.raises(HHAuthError) as exc:
        await client.get("/me")
    assert exc.value.is_revoked
    assert token_store.refresh_calls == 0
    assert token_store.failures == ["token_revoked"]


@respx.mock(base_url=BASE)
async def test_oauth_error_field_variant(
    client: HHClient, respx_mock: respx.MockRouter, token_store: MemoryTokenStore
) -> None:
    respx_mock.get("/negotiations").mock(
        side_effect=[
            httpx.Response(
                403, json={"description": "expired", "errors": [{"type": "forbidden"}], "oauth_error": "token-expired"}
            ),
            httpx.Response(200, json={"items": []}),
        ]
    )
    assert await client.get("/negotiations") == {"items": []}
    assert token_store.refresh_calls == 1


@respx.mock(base_url=BASE)
async def test_captcha_required(client: HHClient, respx_mock: respx.MockRouter, captcha_hits: list[object]) -> None:
    respx_mock.get("/vacancies").mock(
        return_value=httpx.Response(
            403,
            json={
                "errors": [
                    {
                        "type": "captcha_required",
                        "value": "captcha_required",
                        "captcha_url": "https://hh.ru/account/captcha?state=x",
                        "fallback_url": None,
                    }
                ]
            },
        )
    )
    with pytest.raises(HHCaptchaRequired) as exc:
        await client.get("/vacancies")
    assert exc.value.captcha_url == "https://hh.ru/account/captcha?state=x"
    assert len(captcha_hits) == 1


@respx.mock(base_url=BASE)
async def test_negotiation_error(client: HHClient, respx_mock: respx.MockRouter) -> None:
    respx_mock.post("/negotiations").mock(
        return_value=httpx.Response(403, json={"errors": [{"type": "negotiations", "value": "limit_exceeded"}]})
    )
    with pytest.raises(HHNegotiationError) as exc:
        await client.request("POST", "/negotiations", data={"vacancy_id": "1", "resume_id": "2"}, multipart=True)
    assert exc.value.value == "limit_exceeded"


@respx.mock(base_url=BASE)
async def test_multipart_form_and_303_not_followed(client: HHClient, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post("/negotiations").mock(
        return_value=httpx.Response(303, headers={"Location": "https://employer.example/respond"})
    )
    resp = await client.request(
        "POST", "/negotiations", data={"vacancy_id": "1", "resume_id": "2", "message": "hi"}, multipart=True
    )
    assert resp.status == 303
    assert resp.location == "https://employer.example/respond"
    req = route.calls.last.request
    assert req.headers["Content-Type"].startswith("multipart/form-data")
    body = req.content.decode()
    assert 'name="vacancy_id"' in body and 'name="message"' in body


@respx.mock(base_url=BASE)
async def test_repeated_query_params(client: HHClient, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get("/vacancies").mock(return_value=httpx.Response(200, json={}))
    await client.get("/vacancies", params={"area": ["1", "2"], "text": "python"})
    assert str(route.calls.last.request.url).endswith("/vacancies?area=1&area=2&text=python")


# ---- application token for public methods (verified live 2026-09-11: user token → 403 on /vacancies) ----


@respx.mock(base_url=BASE)
async def test_public_uses_application_token(
    client: HHClient, respx_mock: respx.MockRouter, token_store: MemoryTokenStore
) -> None:
    route = respx_mock.get("/vacancies").mock(return_value=httpx.Response(200, json={"found": 1, "items": []}))
    assert client.public_auth == "app"
    data = await client.public("/vacancies", params={"text": "python"})
    assert data["found"] == 1
    assert route.calls.last.request.headers["Authorization"] == "Bearer app-tok"
    assert token_store.app_token_calls == [False]
    # explicit user token still works for applicant methods
    me = respx_mock.get("/me").mock(return_value=httpx.Response(200, json={"id": "1"}))
    await client.get("/me")
    assert me.calls.last.request.headers["Authorization"] == "Bearer tok-1"


@respx.mock(base_url=BASE)
async def test_rejected_application_token_is_refetched_once_without_touching_user_auth(
    client: HHClient, respx_mock: respx.MockRouter, token_store: MemoryTokenStore
) -> None:
    oauth_403 = httpx.Response(403, json={"errors": [{"type": "oauth", "value": "bad_authorization"}]})
    route = respx_mock.get("/vacancies").mock(
        side_effect=[oauth_403, httpx.Response(200, json={"found": 2, "items": []})]
    )
    data = await client.get("/vacancies", auth="app")
    assert data["found"] == 2 and route.call_count == 2
    assert token_store.app_token_calls == [False, True]  # second attempt forced a fresh app token
    assert token_store.failures == [] and token_store.refresh_calls == 0  # user tokens untouched
    route.mock(side_effect=[oauth_403, oauth_403])
    with pytest.raises(HHAuthError):
        await client.get("/vacancies", auth="app")
    assert token_store.failures == []


@respx.mock(base_url=BASE)
async def test_public_auth_user_mode(
    settings, respx_mock: respx.MockRouter, token_store: MemoryTokenStore, fake_sleep: FakeSleep
) -> None:
    from app.hh.ratelimit import TokenBucket

    user_settings = settings.model_copy(update={"hh_public_auth": "user"})
    async with httpx.AsyncClient() as http:
        c = HHClient(
            user_settings,
            token_provider=token_store,
            http=http,
            limiter=TokenBucket(1000.0, burst=100_000, sleep=FakeSleep()),
            sleep=fake_sleep,
        )
        route = respx_mock.get("/vacancies").mock(return_value=httpx.Response(200, json={"found": 0, "items": []}))
        await c.public("/vacancies")
        assert route.calls.last.request.headers["Authorization"] == "Bearer tok-1"
