"""Network error classes shared by every outgoing HTTP call (hh.ru, Telegram, t.me previews, company sites)."""

from __future__ import annotations

import ssl
from functools import lru_cache

import httpx

# httpcore maps socket and TLS failures to httpx.TransportError — except a TLS record error in the middle of a read
# (ssl.SSLError: DECRYPTION_FAILED_OR_BAD_RECORD_MAC, a flaky Wi-Fi or proxy), which comes through raw: one
# such error would fail a whole scan instead of being retried like any other dropped connection.
TRANSPORT_ERRORS: tuple[type[Exception], ...] = (httpx.TransportError, ssl.SSLError)
# the same for callers that also treat HTTP-level errors of httpx (redirect loops, bad URLs) as «network»
HTTP_ERRORS: tuple[type[Exception], ...] = (httpx.HTTPError, ssl.SSLError)


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """One TLS context for every httpx client of the process. Building one reads the whole CA bundle (0,4 s of CPU
    on Windows): every CLI request to the panel and every test runtime paid it, httpx does not share it between
    clients. Pass it as `verify=`."""
    return httpx.create_ssl_context()


def error_text(exc: BaseException) -> str:
    """«ReadTimeout: …» for logs and messages: several httpx exceptions have an empty str(), and the logs used to
    say only `error=''`."""
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
