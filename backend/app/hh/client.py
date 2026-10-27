"""Single HTTP client for api.hh.ru: headers, throttling, retries, typed errors, token refresh, captcha hook."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import structlog

from app.config import Settings
from app.hh.errors import HHAuthError, HHCaptchaRequired, HHError, HHTransportError, build_error
from app.hh.ratelimit import TokenBucket
from app.net import TRANSPORT_ERRORS, error_text, ssl_context

log = structlog.get_logger("hh.client")

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
BACKOFF_SECONDS = (2.0, 4.0, 8.0, 16.0)


class TokenProvider(Protocol):
    async def get_access_token(self) -> str | None: ...

    async def refresh(self) -> str | None: ...

    async def on_auth_failure(self, value: str) -> None: ...

    async def get_app_token(self, *, force: bool = False) -> str | None: ...


AuthMode = bool | str  # False | True/"user" (user token) | "app" (application token)


@dataclass
class HHResponse:
    status: int
    headers: dict[str, str]
    data: Any
    text: str

    @property
    def location(self) -> str | None:
        return self.headers.get("location")


SleepFn = Callable[[float], Awaitable[None]]
CaptchaHook = Callable[[HHCaptchaRequired], Awaitable[None]]


class HHClient:
    def __init__(
        self,
        settings: Settings,
        *,
        token_provider: TokenProvider | None = None,
        http: httpx.AsyncClient | None = None,
        limiter: TokenBucket | None = None,
        sleep: SleepFn | None = None,
        on_captcha: CaptchaHook | None = None,
        max_retries: int = 4,
    ) -> None:
        self._settings = settings
        self._token_provider = token_provider
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0), verify=ssl_context())
        self._limiter = limiter or TokenBucket(settings.hh_rps, burst=3)
        self._sleep: SleepFn = sleep or asyncio.sleep
        self._on_captcha = on_captcha
        self._max_retries = max_retries
        self.requests_made = 0

    @property
    def settings(self) -> Settings:
        return self._settings

    def base_headers(self) -> dict[str, str]:
        ua = self._settings.hh_user_agent
        return {"HH-User-Agent": ua, "User-Agent": ua, "Accept": "application/json"}

    def url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self._settings.hh_api_base}{path}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        multipart: bool = False,
        auth: AuthMode = True,
        headers: Mapping[str, str] | None = None,
    ) -> HHResponse:
        url = self.url(path)
        attempt = 0
        refreshed = False
        mode = "user" if auth is True else ("" if auth is False else str(auth))
        while True:
            hdrs = self.base_headers()
            if headers:
                hdrs.update(headers)
            if mode and self._token_provider is not None:
                if mode == "app":
                    token = await self._token_provider.get_app_token(force=refreshed)
                else:
                    token = await self._token_provider.get_access_token()
                if token:
                    hdrs["Authorization"] = f"Bearer {token}"
            kwargs: dict[str, Any] = {}
            if data is not None:
                if multipart:
                    kwargs["files"] = {k: (None, str(v)) for k, v in data.items()}
                else:
                    kwargs["data"] = dict(data)

            await self._limiter.acquire()
            self.requests_made += 1
            try:
                resp = await self._http.request(
                    method, url, params=params, headers=hdrs, follow_redirects=False, **kwargs
                )
            except TRANSPORT_ERRORS as exc:
                if attempt < self._max_retries:
                    delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
                    log.warning("hh_transport_retry", method=method, path=path, attempt=attempt, error=error_text(exc))
                    attempt += 1
                    await self._sleep(delay)
                    continue
                raise HHTransportError(f"{method} {path}: {error_text(exc)}") from exc

            status = resp.status_code
            if status in RETRY_STATUSES and attempt < self._max_retries:
                delay = self._retry_after(resp) or BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
                log.warning("hh_status_retry", method=method, path=path, status=status, attempt=attempt)
                attempt += 1
                await self._sleep(delay)
                continue

            payload = self._parse_json(resp)
            if status < 400:
                return HHResponse(status, {k.lower(): v for k, v in resp.headers.items()}, payload, resp.text)

            err = build_error(status, payload, resp.headers.get("x-request-id"), resp.text)
            log.info("hh_error", method=method, path=path, status=status, error=err.summary())
            if isinstance(err, HHAuthError) and mode == "app" and self._token_provider is not None:
                # application token rejected: fetch a fresh one once, never touch the user's tokens
                if not refreshed:
                    refreshed = True
                    continue
            elif isinstance(err, HHAuthError) and mode and self._token_provider is not None:
                if err.is_expired and not refreshed:
                    new_token = await self._token_provider.refresh()
                    if new_token:
                        refreshed = True
                        continue
                await self._token_provider.on_auth_failure(err.value)
            if isinstance(err, HHCaptchaRequired) and self._on_captcha is not None:
                await self._on_captcha(err)
            raise err

    async def get(self, path: str, *, params: Mapping[str, Any] | None = None, auth: AuthMode = True) -> Any:
        return (await self.request("GET", path, params=params, auth=auth)).data

    @property
    def public_auth(self) -> str:
        """Token used for public methods (search, vacancy details, dictionaries, suggests): HH_PUBLIC_AUTH."""
        return "user" if self._settings.hh_public_auth == "user" else "app"

    async def public(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        """GET of a public method with the token configured by HH_PUBLIC_AUTH (default: application token)."""
        return await self.get(path, params=params, auth=self.public_auth)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        raw = resp.headers.get("retry-after")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            return None

    @staticmethod
    def _parse_json(resp: httpx.Response) -> Any:
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None


__all__ = ["HHClient", "HHResponse", "TokenProvider", "AuthMode", "HHError"]
