"""Optional local run of `claude -p "/hh-batch"` (F-9.6.1): uses the user's Claude Code subscription, never an API key.

Enabled with CLAUDE_CLI_AUTORUN=true. The command runs in the project root with a timeout; the result file still goes
through the regular import (watcher / exchange tick), so nothing here parses Claude's output.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

import structlog

from app.config import ROOT_DIR, Settings
from app.exchange.packets import pending_dir

log = structlog.get_logger("claude_cli")
COMMAND = "/hh-batch"
SCANNER = ".claude/skills/humanizer-ru/scripts/scan.py"
# Print mode cannot answer permission prompts, and a packet carries untrusted text (vacancy descriptions, employer
# messages and questions) — a prompt injection there must not reach the rest of the machine. So: no acceptEdits;
# writes only into done/ (results) and tmp/ (letters for the scanner), the only command is the humanizer scanner on a
# file, secrets and the database are not readable. Everything else is denied without a prompt.
CLI_FLAGS: tuple[str, ...] = (
    "--permission-mode",
    "default",
    "--allowedTools",
    "Read",
    "Glob",
    "Grep",
    "Edit(./data/exchange/done/**)",  # Edit rules cover Write too (checked with the CLI 2.1.280)
    "Edit(./data/exchange/tmp/**)",
    # only with forward slashes, as /hh-batch prescribes: a rule written with backslashes is not matched by the CLI
    # (checked live with 2.1.280, like the whole set: .env / panel.db / python -c / other paths are refused)
    f"Bash(backend/.venv/Scripts/python.exe {SCANNER}:*)",
    f"Bash(backend/.venv/bin/python {SCANNER}:*)",
    "--disallowedTools",
    "Read(./.env)",
    "Read(./data/panel.db*)",
    "Read(./data/backups/**)",
    "WebFetch",
    "WebSearch",
)
MAX_AUTO_FAILURES = 3


class ClaudeCliError(Exception):
    pass


def claude_executable() -> str | None:
    for name in ("claude", "claude.cmd", "claude.exe"):
        found = shutil.which(name)
        if found:
            return found
    # the native installer puts the CLI into ~/.local/bin, which the PATH of a service (systemd) may lack
    local = Path.home() / ".local" / "bin" / "claude"
    return str(local) if local.is_file() and os.access(local, os.X_OK) else None


def autorun_status(settings: Settings) -> dict[str, Any]:
    exe = claude_executable()
    pending = sorted(p.name for p in pending_dir(settings).glob("*.json")) if pending_dir(settings).exists() else []
    return {
        "enabled": settings.claude_cli_autorun,
        "executable": exe,
        "available": bool(settings.claude_cli_autorun and exe),
        "pending": pending,
        "timeout_sec": settings.claude_cli_timeout_sec,
        "tick_min": settings.claude_cli_tick_min,
        "model": settings.claude_cli_model or None,
        "cwd": str(ROOT_DIR),
    }


async def run_claude_batch(runtime: Any, *, packet: str | None = None) -> dict[str, Any]:
    """Job body `claude_run`: executes `claude -p "/hh-batch [packet]"` in the project root."""
    settings: Settings = runtime.settings
    if not settings.claude_cli_autorun:
        raise ClaudeCliError("Автозапуск выключен: задайте CLAUDE_CLI_AUTORUN=true в .env")
    exe = claude_executable()
    if exe is None:
        raise ClaudeCliError(
            "Команда `claude` не найдена в PATH. Установите Claude Code или запустите /hh-batch вручную"
        )
    pending = pending_dir(settings)
    if packet:
        if "/" in packet or "\\" in packet or not (pending / packet).exists():
            raise ClaudeCliError(f"Пакет {packet} не найден в pending/")
    elif not any(pending.glob("*.json")):
        return {"skipped": "нет пакетов в pending/"}
    prompt = f"{COMMAND} {packet}" if packet else COMMAND
    args = [exe, "-p", prompt, *CLI_FLAGS]
    if settings.claude_cli_model.strip():
        args += ["--model", settings.claude_cli_model.strip()]
    log.info("claude_run_start", packet=packet, cwd=str(ROOT_DIR), model=settings.claude_cli_model or None)
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(ROOT_DIR), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=settings.claude_cli_timeout_sec)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise ClaudeCliError(f"claude не завершился за {settings.claude_cli_timeout_sec} с и был остановлен") from exc
    text = (out or b"").decode("utf-8", errors="replace")
    tail = text[-4000:]
    log_path = _write_log(settings, tail)
    summary = {"returncode": proc.returncode, "output_tail": tail[-1500:], "log": str(log_path), "packet": packet}
    from app.services.audit import audit

    async with runtime.session_factory() as session:
        level = "info" if proc.returncode == 0 else "error"
        await audit(
            session, "batches", f"claude -p {COMMAND}: код выхода {proc.returncode}", level=level, log=str(log_path)
        )
        await session.commit()
    if proc.returncode != 0:
        raise ClaudeCliError(f"claude завершился с кодом {proc.returncode}; см. {log_path.name}")
    return summary


async def should_autorun(runtime: Any) -> tuple[bool, str]:
    """Decision of the background tick: (run now?, reason)."""
    from datetime import timedelta

    from sqlalchemy import desc, select

    from app.db.base import as_utc, utcnow
    from app.db.models import JobRun
    from app.services.state import get_system_state

    settings: Settings = runtime.settings
    if not settings.claude_cli_autorun or settings.claude_cli_tick_min <= 0:
        return False, "выключено"
    if claude_executable() is None:
        return False, "команда claude не найдена"
    pending = pending_dir(settings)
    if not pending.exists() or not any(pending.glob("*.json")):
        return False, "нет пакетов"
    if runtime.jobs.is_running("claude_run") or runtime.jobs.is_running("import"):
        return False, "разбор или импорт уже идёт"
    if runtime.jobs.failures.get("claude_run", 0) >= MAX_AUTO_FAILURES:
        return False, f"{MAX_AUTO_FAILURES} неудачи подряд — запустите вручную"
    async with runtime.session_factory() as session:
        state = await get_system_state(session)
        if state.paused_reason:
            return False, f"панель на паузе: {state.paused_reason}"
        last = (
            await session.execute(select(JobRun).where(JobRun.job == "claude_run").order_by(desc(JobRun.id)).limit(1))
        ).scalar_one_or_none()
    if last is not None and last.status == "error":
        finished = as_utc(last.finished_at) or as_utc(last.started_at)
        if finished and utcnow() - finished < timedelta(minutes=max(1, settings.claude_cli_retry_min)):
            return False, f"после неудачи ждём {settings.claude_cli_retry_min} мин"
    return True, "есть пакеты"


def _write_log(settings: Settings, text: str) -> Path:
    folder = settings.logs_dir
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "claude_run.log"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n===== {COMMAND} =====\n{text}\n")
    return path
