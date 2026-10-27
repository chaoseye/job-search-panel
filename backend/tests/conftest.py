from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.hh.client import HHClient
from app.hh.ratelimit import TokenBucket
from app.net import ssl_context


@pytest.fixture(autouse=True)
def no_running_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI talks to a panel that answers on APP_PORT — on a developer's machine that is the real one (port 8000):
    tests must never send it commands. The CLI tests of the panel path mock their own panel."""
    monkeypatch.setattr("app.cli.running_panel", lambda: None)


@pytest.fixture(scope="session")
def migrated_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An empty database at the head revision, built once per test process (each xdist worker has its own)."""
    from app.db.session import run_migrations_sync

    folder = tmp_path_factory.mktemp("migrated")
    run_migrations_sync(Settings(_env_file=None, data_dir=folder, logs_dir=folder / "logs", database_url=""))  # type: ignore[call-arg]
    return folder / "panel.db"


@pytest.fixture(autouse=True)
def fast_migrations(monkeypatch: pytest.MonkeyPatch, migrated_db: Path) -> None:
    """Runtime.create() of a test starts on a new database: it gets a copy of `migrated_db` instead of running every
    migration again (0,8 s per test, the bulk of the suite). A database that already exists — the migration tests
    build one at an older revision — still goes through `alembic upgrade`."""
    from app.db import session as db_session

    real = db_session.run_migrations

    async def run_migrations(settings: Settings) -> None:
        target = settings.db_path
        if target is not None and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(migrated_db, target)
            return
        await real(settings)

    monkeypatch.setattr("app.runtime.run_migrations", run_migrations)


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class MemoryTokenStore:
    def __init__(self, token: str | None = "tok-1", next_token: str | None = "tok-2") -> None:
        self.token = token
        self.next_token = next_token
        self.refresh_calls = 0
        self.failures: list[str] = []
        self.app_token = "app-tok"
        self.app_token_calls: list[bool] = []

    async def get_access_token(self) -> str | None:
        return self.token

    async def refresh(self) -> str | None:
        self.refresh_calls += 1
        self.token = self.next_token
        return self.token

    async def on_auth_failure(self, value: str) -> None:
        self.failures.append(value)

    async def get_app_token(self, *, force: bool = False) -> str | None:
        self.app_token_calls.append(force)
        return self.app_token


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        hh_client_id="cid",
        hh_client_secret="csecret",
        hh_user_agent="TestApp/1.0 (t@test.local)",
        hh_redirect_uri="http://localhost:8000/api/auth/hh/callback",
        allowed_hosts="test",  # httpx test clients use base_url http://test (the Host check of main.local_only)
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "logs",
        hh_rps=1000.0,
        hh_backoff_factor=0.0,
        humanizer_check=False,  # the scanner spawns a python process per letter; enabled explicitly in its own test
        # a background re-evaluation after every criteria change would race the tests' own writes (SQLite has one
        # writer); the feature is switched on explicitly in its own test
        reprocess_on_criteria_change=False,
    )


@pytest.fixture
def fake_sleep() -> FakeSleep:
    return FakeSleep()


@pytest.fixture
def token_store() -> MemoryTokenStore:
    return MemoryTokenStore()


@pytest.fixture
def captcha_hits() -> list[object]:
    return []


@pytest.fixture
async def client(settings: Settings, fake_sleep: FakeSleep, token_store: MemoryTokenStore, captcha_hits: list[object]):
    async def on_captcha(err: object) -> None:
        captcha_hits.append(err)

    http = httpx.AsyncClient(verify=ssl_context())
    # a huge burst so the limiter never sleeps in tests (fake_sleep is asserted for retry delays only)
    limiter = TokenBucket(1000.0, burst=100_000, sleep=FakeSleep())
    c = HHClient(
        settings, token_provider=token_store, http=http, limiter=limiter, sleep=fake_sleep, on_captcha=on_captcha
    )
    yield c
    await http.aclose()
