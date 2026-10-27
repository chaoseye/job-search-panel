"""Bridge to the `humanizer-ru` skill scanner (.claude/skills/humanizer-ru/scripts/scan.py): a deterministic
check of Russian text for AI-text markers. The panel runs it on imported and edited cover letters and stores the
verdict as letter warnings; the letter itself is never rewritten here — that is Claude's job in /hh-batch.

Requires `razdel` and `pymorphy3` in the backend venv (installed with the dev extras); without them or without
the skill folder the check is silently skipped.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import structlog

from app.config import ROOT_DIR

log = structlog.get_logger("humanizer")

SKILL_DIR = ROOT_DIR / ".claude" / "skills" / "humanizer-ru"
SCAN_SCRIPT = SKILL_DIR / "scripts" / "scan.py"
MIN_SCORE = 70
SCAN_TIMEOUT_SEC = 60
# a scan is a Python process that loads the morphology dictionaries (1–2 s); a few run at once
PRESCAN_CONCURRENCY = 3
_CYRILLIC = re.compile(r"[а-яё]", re.IGNORECASE)
# warnings scanned ahead by prescan() for the current import (text → warnings); letter_warnings() reads it first
_PRESCANNED: ContextVar[dict[str, list[str]] | None] = ContextVar("humanizer_prescanned", default=None)


def available() -> bool:
    return SCAN_SCRIPT.exists()


def is_russian(text: str) -> bool:
    letters = re.findall(r"[a-zа-яё]", text, re.IGNORECASE)
    if not letters:
        return False
    return len(_CYRILLIC.findall(text)) / len(letters) > 0.5


async def scan_text(text: str, *, timeout: float = SCAN_TIMEOUT_SEC) -> dict[str, Any] | None:
    """Returns {"score", "band", "hard_bans": [names], "marker_count", "markers": [...]} or None when unavailable."""
    if not available() or not text.strip():
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(SCAN_SCRIPT),
            "-",
            "--json",
            cwd=str(SKILL_DIR),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", **_safe_env()},
        )
        out, err = await asyncio.wait_for(proc.communicate(text.encode("utf-8")), timeout=timeout)
    except (OSError, TimeoutError) as exc:
        log.info("humanizer_scan_failed", error=str(exc))
        return None
    # exit code 1 means "text failed the check", the JSON verdict is still on stdout
    try:
        data = json.loads(out.decode("utf-8", "replace"))
    except ValueError:
        log.info(
            "humanizer_scan_error",
            code=proc.returncode,
            stderr=(err or b"").decode("utf-8", "replace")[-300:],
        )
        return None
    score = data.get("score") if isinstance(data.get("score"), dict) else {}
    return {
        "score": int(score.get("score") or 0),
        "band": str(score.get("band") or ""),
        "hard_bans": [str(item[0]) for item in data.get("hard_bans") or [] if isinstance(item, list) and item],
        "marker_count": int(data.get("marker_count") or 0),
        "markers": [
            {"category": str(m[0]), "text": str(m[1]), "count": int(m[2])}
            for m in data.get("markers") or []
            if isinstance(m, list) and len(m) >= 3
        ],
    }


def _safe_env() -> dict[str, str]:
    import os

    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "HOME", "LANG", "PYTHONPATH", "VIRTUAL_ENV")
    return {k: v for k, v in os.environ.items() if k.upper() in keep}


def warnings_for(result: dict[str, Any] | None) -> list[str]:
    """Letter warnings from a scan: hard bans always, a low score when below MIN_SCORE."""
    if not result:
        return []
    out: list[str] = []
    if result["hard_bans"]:
        out.append("humanizer: жёсткие запреты — " + "; ".join(result["hard_bans"]))
    if result["score"] < MIN_SCORE:
        out.append(f"humanizer: чистота {result['score']}/100 ({result['band']})")
    return out


def _key(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


async def _scan_warnings(text: str) -> list[str]:
    try:
        return warnings_for(await scan_text(text))
    except Exception as exc:  # noqa: BLE001
        log.info("humanizer_unexpected", error=str(exc))
        return []


async def letter_warnings(text: str) -> list[str]:
    """Convenience for import/edit paths: scan Russian text only, never raise. A text scanned ahead by prescan() in
    the current scope is not scanned again."""
    if not is_russian(text):
        return []
    cached = _PRESCANNED.get()
    if cached is not None and _key(text) in cached:
        return list(cached[_key(text)])
    return await _scan_warnings(_key(text))


@contextmanager
def prescan_scope() -> Iterator[None]:
    """A scope (the import of one result file) where prescan() results are remembered."""
    token = _PRESCANNED.set({})
    try:
        yield
    finally:
        _PRESCANNED.reset(token)


async def prescan(texts: Iterable[str]) -> int:
    """Scans the Russian texts ahead, PRESCAN_CONCURRENCY at a time, into the current prescan_scope(). Callers do it
    before they write: SQLite has one writer, and an import that scanned 24 letters inside its write transaction kept
    every other write of the panel waiting for a minute. Returns how many texts were scanned."""
    cached = _PRESCANNED.get()
    if cached is None:
        return 0
    todo = [k for k in dict.fromkeys(_key(t) for t in texts if t) if k and k not in cached and is_russian(k)]
    semaphore = asyncio.Semaphore(PRESCAN_CONCURRENCY)

    async def one(text: str) -> tuple[str, list[str]]:
        async with semaphore:
            return text, await _scan_warnings(text)

    for text, warnings in await asyncio.gather(*(one(t) for t in todo)):
        cached[text] = warnings
    return len(todo)
