"""`doctor`: what is installed, configured and running — without a single secret.

Written for the person (or the Claude agent) setting the panel up on a new machine: every check says what is
missing and what to do, secrets are reported only as «задан / не задан». `python -m app.cli doctor --json`
gives the same as machine-readable JSON for `/setup`.
"""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import ROOT_DIR, Settings
from app.net import ssl_context

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
MIN_PYTHON = (3, 11)
MIN_NODE = 20
TASK_NAME = "JobSearchPanel"
SERVICE_NAME = "jobsearchpanel"  # systemd unit on a Linux server (deploy/linux/jobsearchpanel.service)


@dataclass
class Check:
    id: str
    status: str
    label: str
    detail: str = ""
    hint: str = ""
    human: bool = False  # only the user can do this step (secrets, browser login, external services)


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, *args: Any, **kwargs: Any) -> None:
        self.checks.append(Check(*args, **kwargs))

    @property
    def ready(self) -> bool:
        return not any(c.status == FAIL for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "next_steps": [asdict(c) for c in self.checks if c.status in (FAIL, WARN)],
            "checks": [asdict(c) for c in self.checks],
        }


# ---- helpers ---------------------------------------------------------------------------


def _version_of(cmd: str) -> str | None:
    exe = shutil.which(cmd)
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr).strip() else None


def _major(version: str | None) -> int | None:
    if not version:
        return None
    digits = version.lstrip("v").split(".")[0]
    return int(digits) if digits.isascii() and digits.isdigit() else None  # «¹».isdigit() is True, int() fails


def _alembic_versions(settings: Settings) -> tuple[str | None, str | None]:
    """(current, head) revisions; current is None without a database."""
    from alembic.script import ScriptDirectory

    from app.db.session import alembic_config

    head = ScriptDirectory.from_config(alembic_config(settings)).get_current_head()
    path = settings.db_path
    if path is None or not path.exists():
        return None, head
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as conn:
            row = conn.execute("select version_num from alembic_version").fetchone()
    except sqlite3.Error:
        return None, head
    return (str(row[0]) if row else None), head


def _port_state(host: str, port: int) -> str:
    """free | panel | busy"""
    try:
        resp = httpx.get(f"http://{host}:{port}/api/health", timeout=2.0, verify=ssl_context())
        if resp.status_code == 200 and "version" in resp.text:
            return "panel"
        return "busy"
    except httpx.HTTPError:
        pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return "busy" if s.connect_ex((host, port)) == 0 else "free"


def _autostart_state() -> str | None:
    """Windows scheduled task; None when not applicable (other OS or schtasks missing)."""
    if sys.platform != "win32" or not shutil.which("schtasks"):
        return None
    try:
        out = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return "set" if out.returncode == 0 else "missing"


def _systemd_state() -> tuple[str, str] | None:
    """(is-enabled, is-active) of the systemd service of a Linux server (deploy/linux); None on other systems."""
    if not sys.platform.startswith("linux") or not shutil.which("systemctl"):
        return None

    def ask(verb: str) -> str:
        try:
            out = subprocess.run(["systemctl", verb, SERVICE_NAME], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        return out.stdout.strip() or "not-found"  # a unit that does not exist answers on stderr only

    return ask("is-enabled"), ask("is-active")


# ---- the report -----------------------------------------------------------------------------


async def collect(settings: Settings) -> Report:
    r = Report()
    py = sys.version_info
    r.add(
        "python",
        OK if py >= MIN_PYTHON else FAIL,
        "Python",
        f"{py.major}.{py.minor}.{py.micro} ({sys.executable})",
        "" if py >= MIN_PYTHON else "нужен Python 3.11+",
    )
    node = _version_of("node")
    node_major = _major(node)
    r.add(
        "node",
        OK if node_major and node_major >= MIN_NODE else WARN,
        "Node.js",
        node or "не найден",
        "" if node_major and node_major >= MIN_NODE else "нужен только для сборки интерфейса: Node.js 20+",
    )
    dist = ROOT_DIR / "frontend" / "dist" / "index.html"
    r.add(
        "frontend_build",
        OK if dist.exists() else FAIL,
        "Сборка интерфейса",
        "frontend/dist есть" if dist.exists() else "frontend/dist нет",
        ""
        if dist.exists()
        else "выполните .\\run.ps1 build (Linux/macOS: cd frontend && npm install && npm run build)",
    )
    env_file = ROOT_DIR / ".env"
    r.add(
        "env_file",
        OK if env_file.exists() else FAIL,
        "Файл .env",
        "есть" if env_file.exists() else "нет",
        "" if env_file.exists() else "скопируйте .env.example в .env и заполните ключи",
    )
    hh_app = bool(settings.hh_client_id and settings.hh_client_secret)
    r.add(
        "hh_app",
        OK if hh_app else FAIL,
        "Приложение hh.ru (HH_CLIENT_ID, HH_CLIENT_SECRET)",
        "заданы" if hh_app else "не заданы",
        ""
        if hh_app
        else f"зарегистрируйте приложение на https://dev.hh.ru/admin с Redirect URI {settings.hh_redirect_uri}",
        human=not hh_app,
    )
    ua_ok = not settings.user_agent_is_placeholder
    r.add(
        "user_agent",
        OK if ua_ok else FAIL,
        "HH_USER_AGENT",
        "с реальной почтой" if ua_ok else "заглушка",
        "" if ua_ok else "впишите JobSearchPanel/1.0 (ваша@почта) — hh.ru отвечает 400 bad_user_agent на заглушку",
        human=not ua_ok,
    )
    try:
        ZoneInfo(settings.timezone)
        r.add("timezone", OK, "TIMEZONE", settings.timezone)
    except (KeyError, ValueError):
        r.add("timezone", FAIL, "TIMEZONE", settings.timezone, "неизвестный часовой пояс, например Europe/Moscow")

    current, head = _alembic_versions(settings)
    if current is None:
        r.add(
            "database",
            WARN,
            "База данных",
            "ещё не создана",
            "создаётся при первом запуске панели или .\\run.ps1 migrate",
        )
    elif current == head:
        r.add("database", OK, "База данных", f"миграции применены ({current})")
    else:
        r.add(
            "database",
            WARN,
            "База данных",
            f"миграция {current}, нужна {head}",
            "перезапустите панель или .\\run.ps1 migrate",
        )

    logged_in = False
    apply_mode: str | None = None
    caps_known = False
    searches = 0
    channels = 0
    chat_id_set = bool(settings.telegram_chat_id)
    if current is not None:
        engine = create_async_engine(settings.db_url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                from app.db.models import SavedSearch
                from app.services.state import auth_public_state, get_hh_auth, get_setting, get_system_state

                logged_in = bool(auth_public_state(await get_hh_auth(session))["logged_in"])
                state = await get_system_state(session)
                apply_mode = state.apply_mode
                caps_known = bool(state.capabilities_json)
                searches = int(
                    (
                        await session.execute(select(func.count(SavedSearch.id)).where(SavedSearch.enabled.is_(True)))
                    ).scalar()
                    or 0
                )
                chat_id_set = chat_id_set or bool(await get_setting(session, "telegram_chat_id"))
                if current == head:
                    from app.services.channels import enabled_channels_count

                    channels = await enabled_channels_count(session)
        finally:
            await engine.dispose()
    r.add(
        "hh_login",
        OK if logged_in else WARN,
        "Вход в hh.ru",
        "выполнен" if logged_in else "нет",
        "" if logged_in else "Настройки → «Войти через hh.ru» (в браузере, под своей учётной записью)",
        human=not logged_in,
    )
    r.add(
        "probe",
        OK if caps_known else WARN,
        "Зонд возможностей hh",
        f"выполнен, режим отклика {apply_mode}" if caps_known else "не выполнен",
        "" if caps_known else "Настройки → «Запустить зонд» после входа",
    )
    profile = settings.exchange_dir / "profile.md"
    r.add(
        "profile",
        OK if profile.exists() else WARN,
        "Профиль для Claude (data/exchange/profile.md)",
        "собран" if profile.exists() else "нет",
        "" if profile.exists() else "Профиль → загрузите резюме (PDF-экспорт hh.ru) и заполните анкету",
        human=not profile.exists(),
    )
    r.add(
        "searches",
        OK if searches else WARN,
        "Поисковые запросы",
        f"включено {searches}" if searches else "нет",
        "" if searches else "Поиски → создайте 1–5 запросов и нажмите «Запустить»",
    )
    tg_token = bool(settings.telegram_bot_token)
    r.add(
        "telegram",
        OK if tg_token and chat_id_set else WARN,
        "Telegram",
        "бот и chat_id заданы"
        if tg_token and chat_id_set
        else ("токен задан, chat_id нет" if tg_token else "не настроен"),
        ""
        if tg_token and chat_id_set
        else (
            "напишите боту /start и нажмите «Определить chat_id» в Настройках"
            if tg_token
            else "создайте бота у @BotFather, впишите TELEGRAM_BOT_TOKEN в .env"
        ),
        human=not (tg_token and chat_id_set),
    )
    imap = bool(settings.imap_host and settings.imap_user and settings.imap_password)
    r.add(
        "imap",
        OK if imap else INFO,
        "Письма hh.ru → журнал (IMAP)",
        "настроен" if imap else "не настроен (необязательно)",
        "" if imap else "IMAP_USER и IMAP_PASSWORD (пароль приложения) в .env — журнал ответов заполнится сам",
        human=not imap,
    )
    r.add(
        "channels",
        INFO,
        "Telegram-каналы с вакансиями",
        f"включено {channels}" if channels else "не добавлены (необязательно)",
        "" if channels else "страница «Каналы»: добавьте публичные каналы с вакансиями, новые посты придут в бота",
    )
    from app.services.claude_cli import claude_executable

    claude = claude_executable()
    r.add(
        "claude_cli",
        OK if claude else WARN,
        "Claude Code (claude в PATH)",
        claude or "не найден",
        ""
        if claude
        else "нужен для автономного /hh-batch (CLAUDE_CLI_AUTORUN); вручную разбор идёт из сеанса Claude Code",
    )
    r.add(
        "claude_autorun",
        INFO,
        "Автономный разбор",
        "включён" if settings.claude_cli_autorun else "выключен (CLAUDE_CLI_AUTORUN=false)",
        "" if settings.claude_cli_autorun else "включите CLAUDE_CLI_AUTORUN=true после `claude` → /login",
    )
    port = _port_state(settings.app_host, settings.app_port)
    r.add(
        "panel",
        OK if port == "panel" else (WARN if port == "busy" else INFO),
        f"Панель на http://localhost:{settings.app_port}",
        {"panel": "работает", "busy": "порт занят другой программой", "free": "не запущена"}[port],
        {
            "panel": "",
            "busy": "смените APP_PORT в .env и Redirect URI приложения hh",
            "free": "запустите .\\run.ps1 start"
            if sys.platform == "win32"
            else f"на сервере: sudo systemctl start {SERVICE_NAME} (иначе — команда uvicorn из README, «Установка»)",
        }[port],
    )
    autostart = _autostart_state()
    if autostart is not None:
        r.add(
            "autostart",
            OK if autostart == "set" else INFO,
            "Автозапуск (планировщик Windows)",
            "настроен" if autostart == "set" else "не настроен",
            "" if autostart == "set" else ".\\run.ps1 autostart — панель будет стартовать при входе в Windows",
        )
    service = _systemd_state()
    if service is not None:
        enabled, active = service
        r.add(
            "autostart",
            OK if enabled == "enabled" and active == "active" else (WARN if enabled == "enabled" else INFO),
            f"Служба systemd {SERVICE_NAME}",
            f"{enabled}, {active}",
            ""
            if enabled == "enabled" and active == "active"
            else (
                f"sudo systemctl status {SERVICE_NAME}; журнал — logs/app.log и journalctl -u {SERVICE_NAME}"
                if enabled == "enabled"
                else "установка на сервер — README (deploy/linux/install.sh)"
            ),
        )
    return r


ICONS = {OK: "✓", WARN: "!", FAIL: "✗", INFO: "·"}


def render(report: Report) -> str:
    lines = [f"{ICONS[c.status]} {c.label}: {c.detail}" + (f" — {c.hint}" if c.hint else "") for c in report.checks]
    lines.append("")
    lines.append("Готово к работе: " + ("да" if report.ready else "нет (исправьте пункты с ✗)"))
    human = [c for c in report.checks if c.human and c.status != OK]
    if human:
        lines.append("Шаги, которые делает человек: " + "; ".join(c.label for c in human))
    return "\n".join(lines)


def render_json(report: Report) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
