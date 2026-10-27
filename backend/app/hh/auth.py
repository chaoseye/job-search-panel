"""OAuth2 authorization-code flow with PKCE for hh.ru."""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import httpx
import structlog

from app.config import Settings
from app.hh.errors import HHOAuthTokenError, HHTransportError, build_error
from app.hh.models import TokenResponse
from app.net import TRANSPORT_ERRORS, error_text

log = structlog.get_logger("hh.auth")


def generate_pkce() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge) per RFC 7636 (S256)."""
    verifier = secrets.token_urlsafe(64)[:96]
    return verifier, pkce_challenge(verifier)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_state() -> str:
    return secrets.token_urlsafe(24)


def build_authorize_url(settings: Settings, state: str, code_challenge: str) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.hh_client_id,
        "state": state,
        "redirect_uri": settings.hh_redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "role": "applicant",
        "force_role": "true",
        "skip_choose_account": "true",
    }
    return f"{settings.hh_oauth_base}/oauth/authorize?{urlencode(params)}"


def _token_headers(settings: Settings) -> dict[str, str]:
    return {
        "HH-User-Agent": settings.hh_user_agent,
        "User-Agent": settings.hh_user_agent,
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }


async def _post_token(http: httpx.AsyncClient, settings: Settings, form: dict[str, str]) -> TokenResponse:
    url = f"{settings.hh_api_base}/token"
    try:
        resp = await http.post(url, data=form, headers=_token_headers(settings), follow_redirects=False)
    except TRANSPORT_ERRORS as exc:
        raise HHTransportError(f"token endpoint unreachable: {error_text(exc)}") from exc
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if resp.status_code >= 400:
        err = build_error(resp.status_code, payload, resp.headers.get("x-request-id"), resp.text)
        if not isinstance(err, HHOAuthTokenError):
            raise HHOAuthTokenError("http_error", err.summary(), status=resp.status_code, body=resp.text[:2000])
        raise err
    if not isinstance(payload, dict) or "access_token" not in payload:
        raise HHOAuthTokenError("bad_response", "no access_token in response", status=resp.status_code)
    return TokenResponse.model_validate(payload)


async def exchange_code(
    http: httpx.AsyncClient, settings: Settings, code: str, code_verifier: str | None
) -> TokenResponse:
    form = {
        "grant_type": "authorization_code",
        "client_id": settings.hh_client_id,
        "client_secret": settings.hh_client_secret,
        "code": code,
        "redirect_uri": settings.hh_redirect_uri,
    }
    if code_verifier:
        form["code_verifier"] = code_verifier
    return await _post_token(http, settings, form)


async def client_credentials_token(http: httpx.AsyncClient, settings: Settings) -> TokenResponse:
    """Application token (grant_type=client_credentials): permanent until revoked, request ≤ once per 5 minutes."""
    form = {
        "grant_type": "client_credentials",
        "client_id": settings.hh_client_id,
        "client_secret": settings.hh_client_secret,
    }
    return await _post_token(http, settings, form)


async def refresh_tokens(http: httpx.AsyncClient, settings: Settings, refresh_token: str) -> TokenResponse:
    """hh accepts refresh only after the access token expired; the refresh token is single-use."""
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    try:
        return await _post_token(http, settings, form)
    except HHOAuthTokenError as exc:
        if exc.error == "invalid_client":
            form.update({"client_id": settings.hh_client_id, "client_secret": settings.hh_client_secret})
            return await _post_token(http, settings, form)
        raise


async def revoke_token(http: httpx.AsyncClient, settings: Settings, access_token: str) -> bool:
    headers = {
        "HH-User-Agent": settings.hh_user_agent,
        "User-Agent": settings.hh_user_agent,
        "Authorization": f"Bearer {access_token}",
    }
    for path in ("/token", "/oauth/token"):
        try:
            resp = await http.delete(f"{settings.hh_api_base}{path}", headers=headers, follow_redirects=False)
        except TRANSPORT_ERRORS as exc:
            log.warning("revoke_failed", path=path, error=error_text(exc))
            return False
        if resp.status_code in (200, 204):
            return True
        if resp.status_code != 404:
            log.warning("revoke_rejected", path=path, status=resp.status_code)
            return False
    return False
