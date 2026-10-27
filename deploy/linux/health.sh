#!/usr/bin/env bash
# Liveness check of Job Search Panel (jobsearchpanel-health.timer, every 2 minutes, as root) — README.
# - a running service whose /api/health failed FAILS_TO_RESTART times in a row is restarted, the bot is told;
# - a crash that systemd restarted by itself (NRestarts grew) is reported once;
# - a stopped service is left alone: it was stopped on purpose (systemctl stop, update.sh, the move to the laptop);
#   a service that gave up is reported by jobsearchpanel-alert.service (OnFailure=).
# Optional: a dead man's switch URL (for example healthchecks.io) in /srv/jobsearchpanel/health-ping.url is pinged
# after every good check, so that a dead server is noticed from outside too.
set -uo pipefail
. "$(dirname "$(readlink -f "$0")")/lib.sh"

readonly FAILS_TO_RESTART=3
readonly STATE=${STATE_DIRECTORY:-/var/lib/jobsearchpanel-health}
readonly PING_URL_FILE=$BASE/health-ping.url
mkdir -p "$STATE"

notify() { # the message goes as jobpanel: notify.py reads .env and the base, which belong to it
  setpriv --reuid="$SERVICE_USER" --regid="$SERVICE_USER" --init-groups --reset-env -- \
    /usr/bin/python3 "$APP/deploy/linux/notify.py" "$1" || log "сообщение в бота не ушло"
}

counter() { cat "$STATE/$1" 2>/dev/null || echo 0; }

state=$(systemctl show -p ActiveState --value "$UNIT")
restarts=$(systemctl show -p NRestarts --value "$UNIT")
if [[ $restarts =~ ^[0-9]+$ ]]; then
  if ((restarts > $(counter nrestarts))); then
    notify "панель упала и была перезапущена systemd (перезапусков с последнего ручного запуска: $restarts). Журнал: journalctl -u $UNIT -n 50"
  fi
  echo "$restarts" >"$STATE/nrestarts"
fi

if [[ $state != active ]]; then # activating, deactivating, inactive, failed: nothing to check now
  echo 0 >"$STATE/fails"
  exit 0
fi

if health_ok; then
  echo 0 >"$STATE/fails"
  if [[ -s $PING_URL_FILE ]]; then
    curl -fsS --max-time 15 --retry 2 -o /dev/null "$(head -n1 "$PING_URL_FILE")" || log "внешний пинг не прошёл"
  fi
  exit 0
fi

fails=$(($(counter fails) + 1))
echo "$fails" >"$STATE/fails"
log "/api/health не ответил ($fails из $FAILS_TO_RESTART)"
if ((fails >= FAILS_TO_RESTART)); then
  echo 0 >"$STATE/fails"
  systemctl restart "$UNIT"
  if wait_health 240; then
    notify "панель не отвечала около $((fails * 2)) минут и была перезапущена; теперь отвечает"
  else
    notify "панель не отвечает и после перезапуска. Журнал: journalctl -u $UNIT -n 50 и logs/app.log"
  fi
fi
exit 0
