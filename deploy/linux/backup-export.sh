#!/usr/bin/env bash
# Forced command of the off-site backup key (user jobremote, /srv/jobsearchpanel/remote/.ssh/authorized_keys):
# the laptop (`.\run.ps1 remote pull-backup`, a daily Windows task) gets the newest nightly copy and nothing else —
# README («Резервные копии»). The copies are made by the panel itself (SQLite backup API + integrity_check).
#   list     names of the copies, newest first
#   latest   the newest copy, gzip; its name goes to stderr as «name: panel_….db»
set -euo pipefail
readonly DIR=/srv/jobsearchpanel/app/data/backups

newest() { find "$DIR" -maxdepth 1 -type f -name 'panel_*.db' -printf '%T@ %f\n' 2>/dev/null | sort -rn | cut -d' ' -f2-; }

case "${SSH_ORIGINAL_COMMAND:-latest}" in
  list) newest ;;
  latest)
    name=$(newest | sed -n 1p) # sed reads to the end: no SIGPIPE under pipefail
    [ -n "$name" ] || { echo "резервных копий нет" >&2; exit 1; }
    echo "name: $name" >&2
    exec gzip -c -6 -- "$DIR/$name"
    ;;
  *) echo "неизвестная команда: допустимо list или latest" >&2; exit 2 ;;
esac
