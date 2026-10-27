"""Pieces of the sign-in by the bot's link (services/web_login.py) and its settings (PANEL_PUBLIC_URL and friends)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.web_login import LoginAttempts, describe_browser, digest, safe_next, well_formed
from tests.conftest import FakeClock


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("", ""),
        ("  ", ""),
        ("https://Panel.Example.com", "https://panel.example.com"),
        ("https://panel.example.com/", "https://panel.example.com"),
        ("https://panel.example.com:443", "https://panel.example.com"),  # as the browser writes Origin
        ("https://panel.example.com:8443", "https://panel.example.com:8443"),
        ("http://panel.localhost:8105", "http://panel.localhost:8105"),  # a local check of the two listeners
        ("http://127.0.0.1:8105", "http://127.0.0.1:8105"),
    ],
)
def test_public_url_is_normalized(raw: str, normalized: str) -> None:
    s = _settings(panel_public_url=raw)
    assert s.panel_public_url == normalized
    assert s.public_host == (normalized.split("//", 1)[1].split(":", 1)[0] if normalized else None)


@pytest.mark.parametrize(
    "raw",
    [
        "panel.example.com",  # no scheme
        "http://panel.example.com",  # plain http outside this machine
        "ftp://panel.example.com",
        "https://panel.example.com/panel",  # a path: the SPA and /login live at the root
        "https://panel.example.com/?x=1",
        "https://user:pw@panel.example.com",
        "https://panel.example.com:99999",
    ],
)
def test_public_url_refuses_what_cannot_work(raw: str) -> None:
    with pytest.raises(ValidationError, match="PANEL_PUBLIC_URL|panel_public_url"):
        _settings(panel_public_url=raw)


def test_public_listener_settings_are_checked() -> None:
    with pytest.raises(ValidationError, match="PANEL_PUBLIC_PORT должен отличаться от APP_PORT"):
        _settings(panel_public_url="https://panel.example", panel_public_port=8000)
    for hosts in ("*", "panel.example", "lan, Panel.Example"):
        with pytest.raises(ValidationError, match="ALLOWED_HOSTS"):
            _settings(panel_public_url="https://panel.example", allowed_hosts=hosts)
    assert _settings(allowed_hosts="*").allowed_host_names is None  # without the public address «*» is as before
    for hours in (0, 721):
        with pytest.raises(ValidationError, match="часов"):
            _settings(panel_session_hours=hours)
    with pytest.raises(ValidationError, match="порт"):
        _settings(panel_public_port=0)
    s = _settings(panel_public_url="https://panel.example")
    # the public name is never one of the tunnel's names
    assert s.panel_public_port == 8100 and s.panel_session_hours == 12 and "panel.example" not in s.allowed_host_names
    assert s.public_url_secure and not _settings(panel_public_url="http://panel.localhost:8105").public_url_secure


@pytest.mark.parametrize(
    ("ua", "expected"),
    [
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 "
            "Safari/537.36",
            "Chrome 141, Windows",
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 "
            "Safari/537.36 Edg/141.0.3537.57",
            "Edge 141, Windows",
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 "
            "YaBrowser/25.8.0.0 Safari/537.36",
            "Яндекс Браузер 25, Windows",
        ),
        ("Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0", "Firefox 143, Linux"),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 "
            "Safari/605.1.15",
            "Safari 18, macOS",
        ),
        (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/18.1 Mobile/15E148 Safari/604.1",
            "Safari 18, iOS",
        ),
        (
            "Mozilla/5.0 (Linux; Android 14; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile "
            "Safari/537.36",
            "Chrome 141, Android",
        ),
        ("curl/8.9.1", "curl/8.9.1"),
        ("", "браузер не назвался"),
        (None, "браузер не назвался"),
    ],
)
def test_describe_browser(ua: str | None, expected: str) -> None:
    assert describe_browser(ua) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/vacancies?group=new", "/vacancies?group=new"),
        ("/", "/"),
        (None, "/"),
        ("", "/"),
        ("//evil.example/x", "/"),
        ("/\\evil.example", "/"),
        ("https://evil.example", "/"),
        ("javascript:alert(1)", "/"),
        ("/login?t=x", "/"),  # never back to the sign-in itself
        ("/a\nb", "/"),
        ("/" + "x" * 600, "/"),
    ],
)
def test_safe_next(value: str | None, expected: str) -> None:
    assert safe_next(value) == expected


def test_token_shape_and_digest() -> None:
    import secrets

    raw = secrets.token_urlsafe(32)
    assert well_formed(raw) and len(raw) == 43
    assert not well_formed(raw[:-1]) and not well_formed(raw + "x") and not well_formed("x" * 42 + "=")
    assert not well_formed(None) and not well_formed("")
    assert digest(raw) != raw and len(digest(raw)) == 64 and digest(raw) == digest(raw)


def test_login_attempts_window_per_address_and_total() -> None:
    clock = FakeClock()
    limiter = LoginAttempts(per_ip=3, total=5, window_sec=600, clock=clock)
    assert [limiter.check("1.1.1.1") for _ in range(3)] == [0.0, 0.0, 0.0]
    wait = limiter.check("1.1.1.1")
    assert wait == pytest.approx(600.0)  # the oldest attempt leaves the window in 10 minutes
    assert limiter.check("2.2.2.2") == 0.0 and limiter.check("2.2.2.2") == 0.0
    assert limiter.check("3.3.3.3") > 0  # five in total: the whole entrance waits
    clock.advance(601)
    assert limiter.check("1.1.1.1") == 0.0 and limiter.check("3.3.3.3") == 0.0
