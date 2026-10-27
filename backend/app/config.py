"""Application settings loaded from .env in the project root."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT_DIR / "backend"
_HHMM = re.compile(r"\s*([01]?\d|2[0-3]):([0-5]\d)\s*")
_WINDOW = re.compile(r"\s*([01]?\d|2[0-3]):([0-5]\d)\s*-\s*([01]?\d|2[0-3]):([0-5]\d)\s*")
WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # weekday numbers mon=0 … sun=6 (resume_bump too)
_CHOICES = {
    "apply_mode": ("auto", "api", "assist"),
    "direct_policy": ("auto", "skip", "keep"),
    "hh_public_auth": ("app", "user"),
}
# counts, intervals and thresholds where a negative number has no meaning (0 usually means «off»)
_NON_NEGATIVE = (
    "hh_backoff_factor",
    "app_port",
    "scan_default_interval_min",
    "scan_first_run_period_days",
    "scan_max_new_per_run",
    "scan_tick_min",
    "prescore_min_for_claude",
    "dictionaries_ttl_days",
    "batch_desc_max_chars",
    "claude_cli_tick_min",
    "claude_cli_retry_min",
    "probe_interval_days",
    "exchange_import_tick_min",
    "apply_min_score_autopilot",
    "apply_max_per_day",
    "apply_max_per_hour",
    "apply_min_delay_sec",
    "apply_max_delay_sec",
    "apply_max_per_employer_7d",
    "sync_interval_min",
    "telegram_letters_min_score",
    "backup_keep",
    "journal_keep_days",
    "studios_daily_limit",
    "studios_hh_details_per_run",
    "studios_contacts_per_run",
    "hot_max_responses",
    "hot_max_age_days",
    "hot_min_score",
    "freshness_stale_days",
    "imap_port",
    "imap_sync_min",
    "imap_since_days",
    "claude_cli_timeout_sec",
    "channel_sync_min",
    "sites_sync_min",
    "ws_tenders_sync_min",
    "ws_tenders_details_per_run",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # hh.ru
    hh_client_id: str = ""
    hh_client_secret: str = ""
    hh_redirect_uri: str = "http://localhost:8000/api/auth/hh/callback"
    hh_user_agent: str = "JobSearchPanel/1.0 (your-email@your-domain.com)"
    hh_host: str = "hh.ru"
    hh_rps: float = 1.0
    # multiplier for retry back-off delays (tests set 0)
    hh_backoff_factor: float = 1.0
    hh_api_base: str = "https://api.hh.ru"
    hh_oauth_base: str = "https://hh.ru"
    # which token opens the public methods (vacancy search/details, dictionaries, suggests): app | user.
    # Verified 2026-09-11: for a fresh application the user token gets 403 on /vacancies, the
    # client_credentials token works.
    hh_public_auth: str = "app"

    # panel
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    frontend_url: str = ""
    # Host names the panel answers to besides 127.0.0.1 / localhost / ::1 (and APP_HOST, FRONTEND_URL): requests with
    # another Host header are refused (DNS rebinding), state-changing requests from another site's page too (CSRF).
    # Comma-separated; `*` turns both checks off
    allowed_hosts: str = ""
    # The public HTTPS address of the panel behind a reverse proxy on the same machine (README, «Вход из
    # браузера»), e.g. https://panel.example.com; empty = off. When set, `python -m app.server` also listens on
    # 127.0.0.1:PANEL_PUBLIC_PORT for that proxy, and everything that comes there needs a session from the bot's
    # one-time link (/open). The tunnel listener (APP_HOST:APP_PORT) never answers to this name
    panel_public_url: str = ""
    panel_public_port: int = 8100
    # how long a browser stays signed in after the link from /open
    panel_session_hours: int = 12
    log_level: str = "INFO"
    timezone: str = "Europe/Moscow"
    data_dir: Path = Field(default=ROOT_DIR / "data")
    logs_dir: Path = Field(default=ROOT_DIR / "logs")
    database_url: str = ""

    # scanner / scoring (M2)
    scan_default_interval_min: int = 30
    scan_first_run_period_days: int = 7
    scan_max_new_per_run: int = 150
    scan_tick_min: int = 5
    prescore_min_for_claude: int = 35
    score_threshold_suitable: int = 70
    score_threshold_maybe: int = 50
    # direct (external) vacancies: auto = skip in api mode, keep in assist mode
    direct_policy: str = "auto"  # auto | skip | keep
    dictionaries_ttl_days: int = 7

    # exchange with Claude Code (M3)
    batch_size: int = 12
    batch_desc_max_chars: int = 4000
    batch_auto_create: bool = True
    claude_cli_autorun: bool = False
    # background run of `claude -p /hh-batch` whenever packets wait in pending/ (0 = only by the button)
    claude_cli_tick_min: int = 5
    # after a failed run wait this long before the next automatic attempt; 3 failures in a row → stop until
    # the user runs it by hand (JobRunner reports the third failure to Telegram)
    claude_cli_retry_min: int = 30
    # weekly safe re-probe of hh capabilities (0 = off): notices if hh reopens the applicant methods
    probe_interval_days: int = 7
    exchange_import_tick_min: int = 2

    # applying (M4)
    apply_mode: str = "auto"  # auto | api | assist
    apply_autopilot: bool = False
    apply_min_score_autopilot: int = 80
    apply_max_per_day: int = 30
    apply_max_per_hour: int = 8
    apply_min_delay_sec: int = 60
    apply_max_delay_sec: int = 240
    apply_window: str = "09:00-21:00"
    apply_max_per_employer_7d: int = 2
    apply_dry_run: bool = False

    # sync (M5)
    sync_interval_min: int = 15

    # telegram (M5)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_daily_summary_at: str = "21:00"
    telegram_events: str = (
        "digest,batch_ready,batch_imported,applied,invitation,discard,message,captcha,auth,limit,job_error,daily,"
        "capability,resume_bump,hot,channel_post,letter,site_vacancy,search_new,studio_reply,ws_tender"
    )
    # event `letter`: every evaluated vacancy with a cover letter and a score at least this high goes to the bot as its
    # own message — vacancy, evaluation summary, the letter text, the apply link
    telegram_letters_min_score: int = 50
    # bot commands via long polling (/status, /pause, /resume, /digest, /batch)
    telegram_commands: bool = True

    # maintenance (M6)
    backup_at: str = "03:30"
    backup_keep: int = 14
    # after the nightly copy: routine journal rows, the event feed and job runs older than this go (records about a
    # vacancy, unsent Telegram messages and the last run of every job stay); 0 = keep everything
    journal_keep_days: int = 90

    # studios: letters sent per local day above this get a warning in the panel
    # (a mailbox writing to many strangers a day looks like a spammer to the mail services)
    studios_daily_limit: int = 20
    # one «Найти студии» run: hh.ru employer cards fetched at most (search pages are extra, 1–2 per keyword)
    studios_hh_details_per_run: int = 150
    # one «Проверить контакты» run: studio sites read at most (up to 4 pages each, a pause between requests)
    studios_contacts_per_run: int = 40
    # touch the primary hh resume when hh allows it (POST /resumes/{id}/publish), only if the probe confirmed it
    resume_auto_publish: bool = False
    # hh closed the publish method for the application (403), so instead the panel reminds the user to press
    # «Поднять в поиске» on hh.ru: local times `08:45,13:00,17:15` (empty = off) on `mon-fri` / `mon,wed` / `*`.
    # hh counts its 4-hour cooldown from the actual click, so keep the slots a little more than 4 hours apart.
    resume_bump_reminder_at: str = ""
    resume_bump_reminder_days: str = "mon-fri"
    # morning digest of «hot» vacancies (fresh, few responses, good score) at this local time; empty = off.
    # Thresholds are shared with the «Горячие» block of the Analytics page
    hot_digest_at: str = ""
    hot_max_responses: int = 50
    hot_max_age_days: int = 3
    hot_min_score: int = 50
    # closed-vacancy check: candidates no search has returned for N days are re-fetched once a day at this time
    # (archived / gone → «закрыта на hh.ru»); 0 days = off
    freshness_check_at: str = "04:10"
    freshness_stale_days: int = 2
    # hh.ru notification e-mails → the manual journal (IMAP, read-only; an app password, only here in .env).
    # Empty host/user/password = off. IMAP_FROM filters the sender, IMAP_SINCE_DAYS bounds the first run
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_from: str = "hh.ru"
    imap_sync_min: int = 10
    imap_since_days: int = 30
    # queue an interview preparation for Claude as soon as an invitation is recorded (by hand, e-mail or sync)
    interview_prep_auto: bool = True
    # queue a reply draft for Claude as soon as an employer message arrives by e-mail
    reply_draft_auto: bool = True
    # re-run rules and prescore over «ниже порога» / «отфильтровано» right after the criteria change
    reprocess_on_criteria_change: bool = True
    claude_cli_timeout_sec: int = 600
    # model for the background `claude -p` (passed as --model); empty = the CLI's own default (ANTHROPIC_MODEL in the
    # user's ~/.claude/settings.json), e.g. claude-opus-5-5 to keep the batch runs off a pricier default
    claude_cli_model: str = ""
    # run the humanizer-ru scanner (.claude/skills/humanizer-ru) on imported/edited Russian letters
    humanizer_check: bool = True
    # public Telegram channels with vacancies (page «Каналы»): the web preview t.me/s/<channel> is read every
    # N minutes, new posts go to the feed and to the bot (event channel_post); 0 = only by the button
    channel_sync_min: int = 30
    # languages of channel posts worth showing (ru, en, uk); posts in other languages are never vacancies.
    # Empty = any language
    channel_languages: str = "ru,en"
    # career sites of chosen companies (page «Сайты компаний», services/company_sites.ADAPTERS): read every N minutes,
    # a vacancy matches when it is in one of SITES_CITIES (office/hybrid) or remote; 0 = only by the button
    sites_sync_min: int = 60
    sites_cities: str = "Москва"
    # tenders of workspace.ru (page «Тендеры»): the first page of the tender list and of
    # the watched sections is read every N minutes (robots.txt allows only the first page), new tenders that suit the
    # profile go to the bot (event ws_tender); 0 = only by the button. Tender cards read at most per run
    ws_tenders_sync_min: int = 60
    ws_tenders_details_per_run: int = 30

    # A value the scheduler or the limits cannot use is refused at start instead of silently becoming a default
    # («25:00-21:00» used to take /api/applications/status down with a 500; maybe > suitable made «maybe» unreachable)
    @field_validator("apply_window")
    @classmethod
    def _check_window(cls, value: str) -> str:
        if not _WINDOW.fullmatch(value or ""):
            raise ValueError(
                f"нужен интервал ЧЧ:ММ-ЧЧ:ММ в пределах 00:00–23:59, например 09:00-21:00; сейчас «{value}»"
            )
        return value

    @field_validator("telegram_daily_summary_at", "backup_at", "hot_digest_at", "freshness_check_at")
    @classmethod
    def _check_hhmm(cls, value: str) -> str:
        if value.strip() and not _HHMM.fullmatch(value):  # empty: the default time / the job is off
            raise ValueError(f"нужно время ЧЧ:ММ в пределах 00:00–23:59, например 21:00; сейчас «{value}»")
        return value

    @field_validator("resume_bump_reminder_at")
    @classmethod
    def _check_reminder_slots(cls, value: str) -> str:
        bad = [p for p in re.split(r"[\s,;]+", value.strip()) if p and not _HHMM.fullmatch(p)]
        if bad:  # the reminder used to drop them silently
            raise ValueError(f"нужны времена ЧЧ:ММ через запятую, например 08:45,13:00; не разобрано: {', '.join(bad)}")
        return value

    @field_validator("resume_bump_reminder_days")
    @classmethod
    def _check_reminder_days(cls, value: str) -> str:
        text = value.strip().lower()
        if not text or text in ("*", "all", "daily", "every day"):
            return value
        bad = [
            p
            for p in re.split(r"[\s,;]+", text)
            if p and not all(d[:3] in WEEKDAY_NAMES for d in (p.split("-", 1) if "-" in p else [p]))
        ]
        if bad:  # «bogus» used to switch the reminders off without a word
            raise ValueError(f"нужны дни mon…sun: «mon-fri», «mon,wed,fri» или «*»; не разобрано: {', '.join(bad)}")
        return value

    @field_validator("apply_mode", "direct_policy", "hh_public_auth")
    @classmethod
    def _check_choice(cls, value: str, info: ValidationInfo) -> str:
        allowed = _CHOICES[str(info.field_name)]
        choice = value.strip().lower()  # «API» used to fall back to auto silently
        if choice not in allowed:
            raise ValueError(f"допустимо {' | '.join(allowed)}; сейчас «{value}»")
        return choice

    @field_validator(*_NON_NEGATIVE)
    @classmethod
    def _check_non_negative(cls, value: float) -> float:
        if value < 0:
            raise ValueError(f"нужно число не меньше 0; сейчас {value}")
        return value

    @field_validator("batch_size", "hh_rps")
    @classmethod
    def _check_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError(f"нужно число больше 0; сейчас {value}")
        return value

    @field_validator("panel_public_url")
    @classmethod
    def _check_public_url(cls, value: str) -> str:
        """`https://host[:port]` without a path; plain http only for this machine's own names (a local check of the
        two listeners). Normalized to `scheme://host[:port]` in lower case."""
        text = value.strip()
        if not text:
            return ""
        hint = f"нужен адрес вида https://panel.example.com без пути; сейчас «{value}»"
        try:
            parts = urlsplit(text)
            port = parts.port
        except ValueError:
            raise ValueError(hint) from None
        host = (parts.hostname or "").lower()
        local = host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")
        if (
            parts.scheme not in ("https", "http")
            or (parts.scheme == "http" and not local)
            or not host
            or parts.username is not None
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            raise ValueError(hint)
        shown = f"[{host}]" if ":" in host else host
        # the form of the browser's Origin header: the default port is not written
        default_port = {"https": 443, "http": 80}[parts.scheme]
        return f"{parts.scheme}://{shown}" + (f":{port}" if port and port != default_port else "")

    @field_validator("panel_public_port")
    @classmethod
    def _check_public_port(cls, value: int) -> int:
        if not 0 < value < 65536:
            raise ValueError(f"нужен порт от 1 до 65535; сейчас {value}")
        return value

    @field_validator("panel_session_hours")
    @classmethod
    def _check_session_hours(cls, value: int) -> int:
        if not 1 <= value <= 720:
            raise ValueError(f"нужно число часов от 1 до 720; сейчас {value}")
        return value

    @model_validator(mode="after")
    def _check_thresholds(self) -> Settings:
        maybe, suitable = self.score_threshold_maybe, self.score_threshold_suitable
        if not 0 <= maybe <= suitable <= 100:
            raise ValueError(
                "пороги оценки: нужно 0 ≤ SCORE_THRESHOLD_MAYBE ≤ SCORE_THRESHOLD_SUITABLE ≤ 100; "
                f"сейчас {maybe} и {suitable}"
            )
        return self

    @model_validator(mode="after")
    def _check_public_listener(self) -> Settings:
        if not self.panel_public_url:
            return self
        if self.panel_public_port == self.app_port:
            raise ValueError(
                f"PANEL_PUBLIC_PORT должен отличаться от APP_PORT: туннель и публичный адрес — разные входы; "
                f"сейчас оба {self.app_port}"
            )
        # the tunnel listener trusts its callers; the public name or «any name» there would let a request relayed by a
        # misconfigured proxy past the sign-in
        extra = {h.strip().lower() for h in self.allowed_hosts.split(",") if h.strip()}
        if "*" in extra or self.public_host in extra:
            raise ValueError(
                "ALLOWED_HOSTS: при заданном PANEL_PUBLIC_URL нельзя «*» и домен публичного адреса — "
                "публичный адрес обслуживает отдельный вход панели (PANEL_PUBLIC_PORT) со входом по ссылке"
            )
        return self

    @property
    def user_agent_is_placeholder(self) -> bool:
        ua = self.hh_user_agent.lower()
        return "your-email@" in ua or "you@example.com" in ua or "@example." in ua or "@" not in ua

    @property
    def allowed_host_names(self) -> frozenset[str] | None:
        """Host names accepted in Host/Origin headers; None = no check (ALLOWED_HOSTS=*)."""
        extra = {h.strip().lower() for h in self.allowed_hosts.split(",") if h.strip()}
        if "*" in extra:
            return None
        names = {"127.0.0.1", "localhost", "::1"} | extra
        if self.app_host.strip() not in ("", "0.0.0.0", "::"):
            names.add(self.app_host.strip().lower())
        front = urlsplit(self.frontend_url).hostname if self.frontend_url else None
        if front:
            names.add(front.lower())
        return frozenset(names)

    @property
    def public_host(self) -> str | None:
        """Host name of PANEL_PUBLIC_URL (None = the public address is off)."""
        return urlsplit(self.panel_public_url).hostname if self.panel_public_url else None

    @property
    def public_url_secure(self) -> bool:
        return self.panel_public_url.startswith("https://")

    @property
    def public_user_agent(self) -> str:
        """User-Agent for everything that is not hh.ru (t.me previews, company sites): the panel name without the
        contact e-mail hh.ru asks for — the e-mail is not sent to unrelated services."""
        return re.sub(r"\s*\(.*\)\s*$", "", self.hh_user_agent).strip() or "JobSearchPanel/1.0"

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{(self.data_dir / 'panel.db').as_posix()}"

    @property
    def sync_db_url(self) -> str:
        return self.db_url.replace("sqlite+aiosqlite", "sqlite")

    @property
    def exchange_dir(self) -> Path:
        return self.data_dir / "exchange"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def db_path(self) -> Path | None:
        """Path of the SQLite file (None for non-sqlite URLs)."""
        url = self.sync_db_url
        if not url.startswith("sqlite:///"):
            return None
        return Path(url[len("sqlite:///") :])

    def frontend_path(self, path: str) -> str:
        base = self.frontend_url.rstrip("/")
        return f"{base}{path}" if base else path


@lru_cache
def get_settings() -> Settings:
    return Settings()


def describe_settings_error(exc: ValidationError) -> str:
    """A ValidationError of Settings → lines «KEY: what is wrong» for the console, doctor and logs/app.log."""
    lines = ["Ошибка в .env — панель не запустится, пока значение не исправлено:"]
    for err in exc.errors():
        key = str(err["loc"][0]).upper() if err.get("loc") else "НАСТРОЙКИ"
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        lines.append(f"  {key}: {msg}" if err.get("loc") else f"  {msg}")
    return "\n".join(lines)
