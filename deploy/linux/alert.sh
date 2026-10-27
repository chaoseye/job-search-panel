#!/usr/bin/env bash
# OnFailure= of jobsearchpanel.service (jobsearchpanel-alert.service, as jobpanel) — README («Мониторинг»).
# systemd 255 triggers it on every failure, also when Restart= is about to bring the service back (a
# kill -9 starts it at once). Such a crash is reported by health.sh («упала и была перезапущена»); here only a service
# that stays down: start limit hit, or failed without a restart.
set -uo pipefail
readonly UNIT=${MONITOR_UNIT:-jobsearchpanel.service}

state=$(systemctl show -p ActiveState --value "$UNIT" 2>/dev/null)
sub=$(systemctl show -p SubState --value "$UNIT" 2>/dev/null)
if [[ $sub == auto-restart || $state == active || $state == activating || $state == reloading ]]; then
  exit 0 # coming back by itself, or already up
fi
exec /usr/bin/python3 /srv/jobsearchpanel/app/deploy/linux/notify.py \
  "Job Search Panel на сервере остановилась и сама не поднимется (${MONITOR_SERVICE_RESULT:-$state/$sub}). Журнал: journalctl -u jobsearchpanel -n 50 и logs/app.log; после исправления — systemctl restart jobsearchpanel"
