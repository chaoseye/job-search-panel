#!/usr/bin/env bash
# Forced command of the Job Search Panel deploy key (/root/.ssh/authorized_keys2, `restrict,command="…/remote.sh"`):
# the laptop (`.\run.ps1 remote …`, deploy/windows/remote.py) deploys and looks after the panel through these verbs
# and gets no shell — README. Every call is written to the journal (journalctl -t jobsearchpanel-remote).
#   status | logs [N] | deploy <sha> [--now] (payload as a tar on stdin) | rollback | restart | stop | start
#   backup | claude-check
#   import-data <sha256>  the move from the laptop (tar.gz on stdin: data/panel.db, exchange, uploads, .env)
#   export-data --stop    the move back to the laptop (the service is stopped and disabled; tar.gz on stdout)
{
  set -euo pipefail
  . "$(dirname "$(readlink -f "$0")")/lib.sh"

  readonly MAX_PAYLOAD=$((200 * 1024 * 1024))
  read -r -a args <<<"${SSH_ORIGINAL_COMMAND:-status}"
  cmd=${args[0]:-status}
  logger -t jobsearchpanel-remote "${SSH_ORIGINAL_COMMAND:-status}" || true

  case "$cmd" in
    status)
      printf 'коммит: %s\n' "$(git -C "$APP" log -1 --format='%h %cd %s' --date=format:'%Y-%m-%d %H:%M' 2>/dev/null || echo '—')"
      enabled=$(systemctl is-enabled "$UNIT" 2>/dev/null || true)
      printf 'служба: %s (%s), с %s\n' "$(systemctl is-active "$UNIT" || true)" "${enabled:-не установлена}" \
        "$(systemctl show -p ActiveEnterTimestamp --value "$UNIT")"
      printf 'проверка живости: %s\n' "$(systemctl is-active jobsearchpanel-health.timer 2>/dev/null || true)"
      public=$({ sed -n 's/^[[:space:]]*PANEL_PUBLIC_URL[[:space:]]*=[[:space:]]*//p' "$APP/.env" 2>/dev/null || true; } |
        tail -n 1 | tr -d "\"'\r")
      if [ -n "$public" ]; then
        printf 'вход из браузера: %s (caddy: %s)\n' "$public" "$(systemctl is-active caddy 2>/dev/null || true)"
      else
        echo "вход из браузера: выключен (PANEL_PUBLIC_URL не задан)"
      fi
      if body=$(curl -fsS --max-time 15 "$HEALTH_URL" 2>/dev/null); then
        python3 -c '
import json, sys
h = json.load(sys.stdin)
auth = h.get("auth") or {}
print("health: ok, версия", h.get("version"), "| вход hh:", "да" if auth.get("logged_in") else "нет",
      "| пауза:", h.get("paused_reason") or "нет", "| задачи:", ", ".join(h.get("running_jobs") or []) or "нет",
      "| очередь Telegram:", h.get("telegram_queue"), "| пакеты:", h.get("pending_batches"))
' <<<"$body"
      else
        echo "health: не отвечает"
      fi
      df -h --output=avail,pcent "$BASE" | tail -1 | awk '{print "диск: свободно " $1 ", занято " $2}'
      ;;
    logs)
      n=${args[1]:-80}
      [[ $n =~ ^[0-9]{1,4}$ ]] || die "logs <число строк>"
      tail -n "$n" "$LOGS/app.log" 2>/dev/null || true
      echo "----- journalctl -u $UNIT"
      journalctl -u "$UNIT" -n 30 --no-pager -o short-iso 2>/dev/null || true
      ;;
    deploy)
      sha=${args[1]:-}
      [[ $sha =~ ^[0-9a-f]{40}$ ]] || die "deploy <полный sha> [--now]"
      flags=()
      if [ "${args[2]:-}" = "--now" ]; then flags=(--now); fi
      install -d -m 0700 -o root -g root "$INCOMING"
      rm -rf "${INCOMING:?}/$sha"
      install -d -m 0700 -o root -g root "$INCOMING/$sha"
      head -c "$MAX_PAYLOAD" | tar -x -C "$INCOMING/$sha" --no-same-owner --no-same-permissions repo.bundle dist.tar.gz
      exec /bin/bash "$APP/deploy/linux/update.sh" "$sha" "${flags[@]}"
      ;;
    rollback) exec /bin/bash "$APP/deploy/linux/update.sh" --rollback ;;
    restart | start)
      systemctl "$cmd" "$UNIT"
      wait_health 240 && echo "панель отвечает" || die "панель не ответила; journalctl -u $UNIT -n 50"
      ;;
    stop)
      systemctl stop "$UNIT"
      echo "остановлена (systemctl start или .\\run.ps1 remote start — запустить)"
      ;;
    backup)
      curl -fsS --max-time 120 -X POST "http://127.0.0.1:$PORT/api/backups"
      echo
      ;;
    claude-check)
      # the claude CLI in the sandbox of the service, as jobpanel, with a one-word question
      systemd-run --quiet --wait --pipe --collect --uid="$SERVICE_USER" --gid="$SERVICE_USER" \
        -p WorkingDirectory="$APP" -p "ReadWritePaths=$DATA $LOGS $PANEL_HOME" "${SANDBOX_PROPS[@]}" \
        -E HOME="$PANEL_HOME" -E PATH="$PANEL_PATH" -E DISABLE_AUTOUPDATER=1 -E TZ=Europe/Moscow \
        "$PANEL_HOME/.local/bin/claude" -p "Ответь одним словом: готово" --model claude-haiku-5-5 \
        --permission-mode default --disallowedTools Bash Edit Write WebFetch WebSearch </dev/null
      ;;
    import-data)
      # the move from the laptop: tar.gz on stdin (data/panel.db, data/exchange, data/uploads, data/probe_report.json,
      # .env) with its sha256 as the argument; a base already here (an earlier move) goes aside, not away
      digest=${args[1]:-}
      [[ $digest =~ ^[0-9a-f]{64}$ ]] || die "import-data <sha256 архива>"
      systemctl is-active --quiet "$UNIT" && die "служба работает — данные работающей панели не заменяю"
      work=$(mktemp -d "$INCOMING/import.XXXXXX")
      trap 'rm -rf "$work"' EXIT
      head -c "$MAX_PAYLOAD" >"$work/data.tar.gz"
      [ "$(sha256sum "$work/data.tar.gz" | cut -d' ' -f1)" = "$digest" ] || die "архив пришёл повреждённым (sha256 не совпал)"
      if tar -tzf "$work/data.tar.gz" | grep -Ev '^(\.env|data/.*)$' >/dev/null; then die "в архиве лишние файлы"; fi
      if tar -tzf "$work/data.tar.gz" | grep -E '(^/|(^|/)\.\.(/|$))' >/dev/null; then die "в архиве недопустимые пути"; fi
      mkdir "$work/x"
      tar -xzf "$work/data.tar.gz" -C "$work/x" --no-same-owner --no-same-permissions
      [ -s "$work/x/data/panel.db" ] && [ -s "$work/x/.env" ] || die "в архиве нет data/panel.db или .env"
      sed -i 's/\r$//' "$work/x/.env"
      chown -R "$SERVICE_USER:$SERVICE_USER" "$work/x"
      chmod -R u=rwX,g=rX,o= "$work/x/data"
      chmod 0600 "$work/x/.env"
      aside=$DATA/before-import-$(date +%Y%m%d_%H%M%S)
      for item in panel.db panel.db-wal panel.db-shm exchange uploads probe_report.json; do
        if [ -e "$DATA/$item" ]; then
          as_panel mkdir -p "$aside"
          mv "$DATA/$item" "$aside/"
        fi
      done
      if [ -e "$APP/.env" ]; then
        as_panel mkdir -p "$aside"
        mv "$APP/.env" "$aside/.env"
      fi
      if [ -d "$aside" ]; then log "прежние данные сервера отложены в $aside"; fi
      for item in "$work/x/data"/*; do mv "$item" "$DATA/"; done
      mv "$work/x/.env" "$APP/.env"
      as_panel "$VENV/bin/python" - "$DATA/panel.db" <<'PY'
import sqlite3
import sys

con = sqlite3.connect(sys.argv[1])
check = con.execute("pragma integrity_check").fetchone()[0]
rev = con.execute("select version_num from alembic_version").fetchone()[0]
vacancies = con.execute("select count(*) from vacancies").fetchone()[0]
con.close()
print(f"база принята: integrity_check {check}, миграция {rev}, вакансий {vacancies}")
sys.exit(0 if check == "ok" else 1)
PY
      systemctl enable "$UNIT" jobsearchpanel-health.timer 2>&1
      systemctl start jobsearchpanel-health.timer
      echo "данные на месте; служба включена в автозапуск, запуск — start"
      ;;
    export-data)
      [ "${args[1]:-}" = "--stop" ] || die "export-data останавливает панель на сервере: повторите с --stop"
      systemctl disable --now "$UNIT" jobsearchpanel-health.timer >&2
      as_panel "$VENV/bin/python" - "$DATA/panel.db" >&2 <<'PY'
import sqlite3
import sys

con = sqlite3.connect(sys.argv[1], timeout=60)
con.execute("pragma wal_checkpoint(TRUNCATE)")
check = con.execute("pragma integrity_check").fetchone()[0]
con.close()
sys.exit(0 if check == "ok" else f"integrity_check: {check}")
PY
      log "служба остановлена и выключена; выгружаю data/ (без резервных копий) и .env"
      cd "$APP"
      members=(data/panel.db .env)
      for extra in data/exchange data/uploads data/probe_report.json; do [ -e "$extra" ] && members+=("$extra"); done
      tar -czf - "${members[@]}"
      ;;
    *) die "неизвестная команда: $cmd (status, logs, deploy, rollback, restart, stop, start, backup, claude-check, import-data, export-data)" ;;
  esac
  exit 0
}
