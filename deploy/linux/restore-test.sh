#!/usr/bin/env bash
# Restore drill of Job Search Panel — README («Резервные копии»). As root:
#   restore-test.sh [backup.db] [--keep]     default: the newest data/backups/panel_*.db
# A copy of the backup without hh_auth (the hh.ru tokens) and paused starts as a second panel on 127.0.0.1:8765 from
# a copy of the code with its own .env without secrets (no hh.ru application, no bot, no mail, no claude, no
# channels/sites/tenders): it cannot touch hh.ru tokens, the bot's getUpdates or the production base. The run uses
# the sandbox of the service (SANDBOX_PROPS, compared with the unit file here). The figures of the API are checked
# against the base, then the drill is removed (--keep leaves it in /srv/jobsearchpanel/restore-test).
{
  set -euo pipefail
  . "$(dirname "$(readlink -f "$0")")/lib.sh"

  readonly DRILL=$BASE/restore-test
  readonly DRILL_PORT=8765
  readonly DRILL_UNIT=jobsearchpanel-restore-test
  src="" keep=0
  for arg in "$@"; do
    case "$arg" in
      --keep) keep=1 ;;
      *) src=$arg ;;
    esac
  done
  [ "$(id -u)" = 0 ] || die "нужен root"
  if [ -z "$src" ]; then
    src=$(find "$DATA/backups" -maxdepth 1 -type f -name 'panel_*.db' -printf '%T@ %p\n' 2>/dev/null | sort -rn | sed -n '1s/^[^ ]* //p')
  fi
  [ -f "$src" ] || die "нет резервной копии для проверки"

  # the sandbox of the drill must be the sandbox of the service
  for prop in "${SANDBOX_PROPS[@]}"; do
    [ "$prop" = -p ] && continue
    name=${prop%%=*}
    grep -q "^$name=" "/etc/systemd/system/$UNIT.service" || die "SANDBOX_PROPS ($name) расходится с $UNIT.service"
  done

  cleanup() {
    systemctl stop "$DRILL_UNIT" 2>/dev/null || true
    if ((keep)); then log "песочница оставлена: $DRILL"; else rm -rf "$DRILL"; fi
  }
  trap cleanup EXIT

  rm -rf "$DRILL"
  install -d -m 0755 -o root -g root "$DRILL" "$DRILL/code"
  git -C "$APP" archive HEAD | tar -x -C "$DRILL/code"
  cp -a "$APP/frontend/dist" "$DRILL/code/frontend/dist"
  install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DRILL/code/data" "$DRILL/code/logs" "$DRILL/home"
  cat >"$DRILL/code/.env" <<EOF
# restore drill — no secrets: nothing here can reach hh.ru tokens, the bot or the mailbox
APP_HOST=127.0.0.1
APP_PORT=$DRILL_PORT
TIMEZONE=Europe/Moscow
HH_CLIENT_ID=
HH_CLIENT_SECRET=
HH_USER_AGENT=JobSearchPanel/1.0 (restore-test@example.invalid)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_COMMANDS=false
IMAP_HOST=
IMAP_USER=
IMAP_PASSWORD=
CLAUDE_CLI_AUTORUN=false
BATCH_AUTO_CREATE=false
CHANNEL_SYNC_MIN=0
SITES_SYNC_MIN=0
WS_TENDERS_SYNC_MIN=0
HOT_DIGEST_AT=
RESUME_BUMP_REMINDER_AT=
FRESHNESS_STALE_DAYS=0
PROBE_INTERVAL_DAYS=0
REPROCESS_ON_CRITERIA_CHANGE=false
INTERVIEW_PREP_AUTO=false
REPLY_DRAFT_AUTO=false
EOF
  chown "$SERVICE_USER:$SERVICE_USER" "$DRILL/code/.env"
  chmod 0600 "$DRILL/code/.env"

  log "копия для проверки: $(basename "$src") ($(du -h "$src" | cut -f1))"
  # root reads the source (it may lie outside data/, e.g. a copy brought from the laptop), the drill owns the copy
  install -m 0640 -o "$SERVICE_USER" -g "$SERVICE_USER" "$src" "$DRILL/code/data/panel.db"
  counts=$(as_panel "$VENV/bin/python" - "$DRILL/code/data/panel.db" <<'PY'
import json
import sqlite3
import sys

con = sqlite3.connect(sys.argv[1])
check = con.execute("pragma integrity_check").fetchone()[0]
if check != "ok":
    sys.exit(f"integrity_check: {check}")
con.execute("delete from hh_auth")  # the drill never holds the hh.ru tokens
con.execute("update system_state set paused_reason = 'restore-test'")
con.commit()
rev = con.execute("select version_num from alembic_version").fetchone()[0]
out = {"revision": rev}
for table in ("vacancies", "applications", "saved_searches", "cover_letters", "telegram_channels", "notifications"):
    out[table] = con.execute(f"select count(*) from {table}").fetchone()[0]
con.close()
print(json.dumps(out))
PY
)
  log "в копии: $counts"

  systemd-run --quiet --collect --unit="$DRILL_UNIT" --uid="$SERVICE_USER" --gid="$SERVICE_USER" \
    -p WorkingDirectory="$DRILL/code/backend" -p "ReadWritePaths=$DRILL/code/data $DRILL/code/logs $DRILL/home" \
    "${SANDBOX_PROPS[@]}" -E HOME="$DRILL/home" -E PATH=/usr/local/bin:/usr/bin:/bin -E TZ=Europe/Moscow \
    -E PYTHONUTF8=1 -E PYTHONDONTWRITEBYTECODE=1 \
    "$VENV/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port "$DRILL_PORT" --no-access-log

  url=http://127.0.0.1:$DRILL_PORT
  deadline=$((SECONDS + 180))
  until curl -fsS --max-time 10 "$url/api/health" 2>/dev/null | grep '"ok":true' >/dev/null; do
    ((SECONDS < deadline)) || { journalctl -u "$DRILL_UNIT" -n 40 --no-pager >&2; die "песочница не ответила за 3 минуты"; }
    sleep 3
  done
  health=$(curl -fsS "$url/api/health")
  summary=$(curl -fsS "$url/api/vacancies/summary")
  index=$(curl -fsS -o /dev/null -w '%{http_code}' "$url/")
  python3 - "$counts" "$health" "$summary" "$index" <<'PY'
import json
import sys

counts, health, summary, index = json.loads(sys.argv[1]), json.loads(sys.argv[2]), json.loads(sys.argv[3]), sys.argv[4]
problems = []
if health.get("auth", {}).get("logged_in"):
    problems.append("в песочнице оказался вход в hh.ru")
if health.get("paused_reason") != "restore-test":
    problems.append(f"пауза не та: {health.get('paused_reason')}")
if summary.get("total") != counts["vacancies"]:
    problems.append(f"вакансий в API {summary.get('total')}, в базе {counts['vacancies']}")
if index != "200":
    problems.append(f"интерфейс ответил {index}")
print("API отвечает: версия", health.get("version"), "| база:", health.get("db"), "| миграция:", counts["revision"],
      "| вакансий:", summary.get("total"), "| кандидатов:", summary.get("candidates"), "| интерфейс:", index)
if problems:
    sys.exit("; ".join(problems))
PY
  log "проверка восстановления пройдена: $(basename "$src") поднимается, API и интерфейс отвечают"
  exit 0
}
