#!/usr/bin/env python3
"""A message to the panel's Telegram bot from outside the panel (health.sh, jobsearchpanel-alert.service).

Standard library only and no import of the panel: it reports exactly the cases when the panel or its venv is broken.
The bot token and the chat come from the panel's own .env and base (the chat chosen in the panel wins, as in
services/telegram.effective_chat_id). Neither is printed. Run as jobpanel (the owner of .env).

    python3 notify.py "текст сообщения"
"""

from __future__ import annotations

import json
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # deploy/linux/notify.py → the project root
PREFIX = "🖥 Сервер: "


def env_values(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of .env (comments, `export`, quotes and inline comments as python-dotenv reads them)."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def chat_from_base(db: Path) -> str | None:
    """settings.telegram_chat_id written by the panel («Определить chat_id»). Read-only; `immutable` when the sandbox
    of the caller cannot touch the -shm file of a WAL base (the setting is old, it is in the main file)."""
    for uri in (f"file:{db.as_posix()}?mode=ro", f"file:{db.as_posix()}?immutable=1"):
        try:
            with sqlite3.connect(uri, uri=True, timeout=10) as con:
                row = con.execute("select value_json from settings where key = 'telegram_chat_id'").fetchone()
        except sqlite3.Error:
            continue
        if row:
            try:
                value = json.loads(row[0])
            except ValueError:
                return None
            return str(value).strip() or None
        return None
    return None


def main(argv: list[str]) -> int:
    text = " ".join(argv[1:]).strip()
    if not text:
        print("usage: notify.py <text>", file=sys.stderr)
        return 2
    env = env_values(ROOT / ".env")
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    data_dir = Path(env.get("DATA_DIR") or ROOT / "data")
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    chat = chat_from_base(data_dir / "panel.db") or env.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("notify: TELEGRAM_BOT_TOKEN или chat_id не заданы — сообщение не отправлено", file=sys.stderr)
        return 1
    body = urllib.parse.urlencode({"chat_id": chat, "text": PREFIX + text, "disable_web_page_preview": "true"}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            ok = bool(json.load(resp).get("ok"))
    except urllib.error.HTTPError as exc:  # the URL holds the token: only the status goes out
        print(f"notify: Telegram ответил {exc.code}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"notify: сеть: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("notify: отправлено" if ok else "notify: Telegram не принял сообщение", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
