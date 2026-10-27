"""Typed exceptions for hh.ru API responses."""

from __future__ import annotations

from typing import Any


class HHError(Exception):
    """Base class. Carries status, parsed `errors[]`, description, request id and a body excerpt."""

    def __init__(
        self,
        message: str | None = None,
        *,
        status: int | None = None,
        errors: list[dict[str, Any]] | None = None,
        description: str | None = None,
        request_id: str | None = None,
        body: str = "",
        oauth_error: str | None = None,
    ) -> None:
        self.status = status
        self.errors = errors or []
        self.description = description
        self.request_id = request_id
        self.body = body
        self.oauth_error = oauth_error
        super().__init__(message or self.summary())

    def summary(self) -> str:
        parts = [f"HTTP {self.status}" if self.status else "no response"]
        for err in self.errors:
            t = err.get("type")
            v = err.get("value")
            parts.append(f"{t}/{v}" if v else str(t))
        if self.oauth_error:
            parts.append(f"oauth_error={self.oauth_error}")
        if self.description:
            parts.append(self.description)
        return "; ".join(str(p) for p in parts if p)

    @property
    def error_types(self) -> list[str]:
        return [str(e.get("type")) for e in self.errors if e.get("type")]

    def has_type(self, error_type: str) -> bool:
        return error_type in self.error_types


class HHTransportError(HHError):
    """Network failure or timeout after all retries."""


class HHBadRequest(HHError):
    pass


class HHBadArgument(HHBadRequest):
    def __init__(self, field: str, **kw: Any) -> None:
        self.field = field
        super().__init__(f"bad_argument: {field}", **kw)


class HHForbidden(HHError):
    pass


class HHAuthError(HHForbidden):
    """errors[].type == oauth or top-level oauth_error."""

    EXPIRED = {"token_expired"}
    REVOKED = {"token_revoked", "bad_authorization", "application_not_found", "client_id_deleted"}

    def __init__(self, value: str, **kw: Any) -> None:
        self.value = value.replace("-", "_")
        super().__init__(f"oauth: {self.value}", **kw)

    @property
    def is_expired(self) -> bool:
        return self.value in self.EXPIRED

    @property
    def is_revoked(self) -> bool:
        return self.value in self.REVOKED


class HHCaptchaRequired(HHForbidden):
    def __init__(self, captcha_url: str | None, fallback_url: str | None, **kw: Any) -> None:
        self.captcha_url = captcha_url
        self.fallback_url = fallback_url
        super().__init__("captcha_required", **kw)


class HHNegotiationError(HHError):
    """errors[].type == negotiations (apply/message errors)."""

    def __init__(self, value: str, **kw: Any) -> None:
        self.value = value
        super().__init__(f"negotiations: {value}", **kw)


class HHNotFound(HHError):
    pass


class HHRateLimited(HHError):
    pass


class HHServerError(HHError):
    pass


class HHOAuthTokenError(HHError):
    """Error from the token endpoint: {"error": "...", "error_description": "..."}."""

    def __init__(self, error: str, error_description: str | None, **kw: Any) -> None:
        self.error = error
        self.error_description = error_description
        super().__init__(f"token endpoint: {error}: {error_description or ''}", **kw)

    @property
    def not_expired_yet(self) -> bool:
        return self.error == "invalid_grant" and "not expired" in (self.error_description or "")


def build_error(status: int, payload: Any, request_id: str | None = None, text: str = "") -> HHError:
    errors: list[dict[str, Any]] = []
    description: str | None = None
    oauth_error: str | None = None
    if isinstance(payload, dict):
        raw = payload.get("errors")
        if isinstance(raw, list):
            errors = [e for e in raw if isinstance(e, dict)]
        elif payload.get("type"):
            errors = [payload]
        description = payload.get("description") if isinstance(payload.get("description"), str) else None
        oauth_error = payload.get("oauth_error") if isinstance(payload.get("oauth_error"), str) else None
        if "error" in payload and "errors" not in payload and isinstance(payload.get("error"), str):
            return HHOAuthTokenError(
                payload["error"],
                payload.get("error_description"),
                status=status,
                errors=errors,
                request_id=request_id,
                body=text[:2000],
            )

    common: dict[str, Any] = {
        "status": status,
        "errors": errors,
        "description": description,
        "request_id": request_id,
        "body": text[:2000],
        "oauth_error": oauth_error,
    }
    for err in errors:
        t = err.get("type")
        v = err.get("value")
        if t == "captcha_required":
            return HHCaptchaRequired(err.get("captcha_url"), err.get("fallback_url"), **common)
        if t == "oauth":
            return HHAuthError(str(v or "bad_authorization"), **common)
        if t == "negotiations":
            return HHNegotiationError(str(v or ""), **common)
        if t == "bad_argument":
            return HHBadArgument(str(v or ""), **common)
    if oauth_error:
        return HHAuthError(oauth_error, **common)
    if status == 400:
        return HHBadRequest(**common)
    if status in (401, 403):
        return HHForbidden(**common)
    if status == 404:
        return HHNotFound(**common)
    if status == 429:
        return HHRateLimited(**common)
    if status >= 500:
        return HHServerError(**common)
    return HHError(**common)
