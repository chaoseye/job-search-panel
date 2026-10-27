# shellcheck shell=bash
# shellcheck disable=SC2034
# (the constants are used by the scripts that source this file, not here)
# Shared paths and helpers of the Job Search Panel server scripts (install.sh, update.sh, remote.sh, health.sh,
# restore-test.sh) — README. Sourced, never run on its own; everything here runs as root.

readonly BASE=/srv/jobsearchpanel
readonly APP=$BASE/app              # the git checkout, root-owned: the service cannot change its own code
readonly DATA=$APP/data             # base, exchange with Claude, backups (jobpanel)
readonly LOGS=$APP/logs             # jobpanel
readonly PANEL_HOME=$BASE/home      # home of the service user: Claude Code and its login (jobpanel, 0700)
readonly INCOMING=$BASE/incoming    # deploy payloads from the laptop (root, 0700)
readonly REMOTE_HOME=$BASE/remote   # home of jobremote: authorized_keys of the tunnel and backup keys (root-owned)
readonly VENV=$APP/backend/.venv    # root-owned, wheels only
readonly UNIT=jobsearchpanel
readonly SERVICE_USER=jobpanel
readonly REMOTE_USER=jobremote
readonly PORT=8000                  # 127.0.0.1 only; registered in /srv/README
readonly HEALTH_URL="http://127.0.0.1:$PORT/api/health"
readonly DEPLOY_LOG=$BASE/deploy.log
readonly PANEL_PATH="$PANEL_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

# The sandbox of jobsearchpanel.service, for transient units that must behave like it (restore-test.sh, the
# `claude-check` of remote.sh). Keep in step with the unit: restore-test.sh compares the names. Without RemoveIPC:
# a transient unit of the same user would clear the IPC of the running service when it stops.
SANDBOX_PROPS=(
  -p UMask=0027
  -p NoNewPrivileges=yes
  -p PrivateTmp=yes
  -p PrivateDevices=yes
  -p ProtectSystem=strict
  -p ProtectHome=yes
  -p ProtectKernelTunables=yes
  -p ProtectKernelModules=yes
  -p ProtectKernelLogs=yes
  -p ProtectControlGroups=yes
  -p ProtectClock=yes
  -p ProtectHostname=yes
  -p ProtectProc=invisible
  -p RestrictNamespaces=yes
  -p RestrictRealtime=yes
  -p RestrictSUIDSGID=yes
  -p LockPersonality=yes
  -p "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK"
  -p CapabilityBoundingSet=
  -p AmbientCapabilities=
  -p SystemCallArchitectures=native
  -p SystemCallFilter=@system-service
  -p "SystemCallFilter=~@privileged @resources"
  -p SystemCallErrorNumber=EPERM
  -p "InaccessiblePaths=-$INCOMING"
  -p MemoryHigh=1200M
  -p MemoryMax=1600M
  -p CPUQuota=200%
  -p CPUWeight=50
  -p IOWeight=50
  -p Nice=5
  -p TasksMax=512
  -p LimitNOFILE=8192
  -p LimitCORE=0
)

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >&2; }
die() {
  log "ОШИБКА: $*"
  exit 1
}

# a command as the service user with its own HOME and PATH (Claude Code lives in ~/.local/bin), from / so that the
# caller's working directory (often /root) does not matter
as_panel() {
  (cd / && setpriv --reuid="$SERVICE_USER" --regid="$SERVICE_USER" --init-groups --reset-env -- \
    env HOME="$PANEL_HOME" PATH="$PANEL_PATH" TZ=Europe/Moscow PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1 "$@")
}

# grep without -q reads to the end: an early exit would SIGPIPE curl, and pipefail would turn a good answer into a failure
health_ok() { curl -fsS --max-time 15 "$HEALTH_URL" 2>/dev/null | grep '"ok":true' >/dev/null; }

wait_health() { # wait_health [seconds] — 0 once /api/health answers, 1 on timeout or a failed unit
  local deadline=$((SECONDS + ${1:-180}))
  until health_ok; do
    ((SECONDS < deadline)) || return 1
    systemctl is-failed --quiet "$UNIT" && return 1
    sleep 3
  done
}

running_jobs() { # names of the panel jobs running now; empty when idle or not running
  curl -fsS --max-time 15 "$HEALTH_URL" 2>/dev/null |
    python3 -c 'import json, sys; print(" ".join(json.load(sys.stdin).get("running_jobs") or []))' 2>/dev/null || true
}

safe_daemon_reload() {
  # a reload also loads other services' unit files that were edited on disk but not loaded yet — not our call to make
  local pending
  pending=$(systemctl show --property=Id,NeedDaemonReload '*' 2>/dev/null |
    awk -F= '/^Id=/ {id = $2} /^NeedDaemonReload=yes/ && id !~ /^jobsearchpanel/ {print id}' | tr '\n' ' ')
  [ -z "${pending// /}" ] || die "systemd ждёт daemon-reload для чужих юнитов: $pending— согласуйте с их владельцем"
  systemctl daemon-reload
}

install_units() { # copies changed unit files and the logrotate rule from the checkout; reloads systemd when needed
  local changed=0 f name
  for f in "$APP"/deploy/linux/jobsearchpanel*.service "$APP"/deploy/linux/jobsearchpanel*.timer; do
    name=$(basename "$f")
    if ! cmp -s "$f" "/etc/systemd/system/$name"; then
      install -m 0644 -o root -g root "$f" "/etc/systemd/system/$name"
      changed=1
      log "юнит обновлён: $name"
    fi
  done
  if ! cmp -s "$APP/deploy/linux/jobsearchpanel.logrotate" /etc/logrotate.d/jobsearchpanel; then
    install -m 0644 -o root -g root "$APP/deploy/linux/jobsearchpanel.logrotate" /etc/logrotate.d/jobsearchpanel
    logrotate --debug /etc/logrotate.d/jobsearchpanel >/dev/null 2>&1 || die "logrotate: ошибка в /etc/logrotate.d/jobsearchpanel"
    log "правило logrotate обновлено"
  fi
  if ((changed)); then safe_daemon_reload; fi
  return 0
}

python_requirements() { # dependencies + the [server] extra of backend/pyproject.toml, one per line
  python3 - "$APP/backend/pyproject.toml" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as fh:
    project = tomllib.load(fh)["project"]
print("\n".join(project["dependencies"] + project.get("optional-dependencies", {}).get("server", [])))
PY
}

deps_stamp() { sha256sum "$APP/backend/pyproject.toml" | cut -d' ' -f1; }
deps_stale() { [ ! -f "$VENV/.deps-stamp" ] || [ "$(cat "$VENV/.deps-stamp")" != "$(deps_stamp)" ]; }

install_python_deps() {
  # wheels only: unpacking a wheel runs no code of the package, so nothing third-party executes as root here. The
  # project itself is not installed: the service runs it from backend/ (WorkingDirectory, `python -m`)
  local req
  [ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
  req=$(mktemp)
  python_requirements >"$req"
  "$VENV/bin/python" -m pip install --disable-pip-version-check --no-input --quiet --only-binary=:all: --upgrade pip
  if ! "$VENV/bin/python" -m pip install --disable-pip-version-check --no-input --quiet --only-binary=:all: -r "$req"; then
    rm -f "$req"
    die "pip не смог поставить зависимости (разрешены только готовые wheel-пакеты)"
  fi
  rm -f "$req"
  deps_stamp >"$VENV/.deps-stamp"
  "$VENV/bin/python" -m pip freeze --disable-pip-version-check >"$BASE/requirements.lock.txt"
  log "зависимости Python поставлены ($("$VENV/bin/python" --version)); список версий — $BASE/requirements.lock.txt"
}

install_dist() { # install_dist <dist.tar.gz> <sha> — the interface built on the laptop from this very commit
  local archive=$1 sha=$2 tmp
  if tar -tzf "$archive" | grep -E '(^/|(^|/)\.\.(/|$))' >/dev/null; then die "в архиве интерфейса недопустимые пути"; fi
  tmp=$(mktemp -d "$APP/frontend/.dist.XXXXXX")
  tar -xzf "$archive" -C "$tmp" --no-same-owner --no-same-permissions
  [ -f "$tmp/index.html" ] || { rm -rf "$tmp"; die "в архиве интерфейса нет index.html"; }
  [ "$(tr -d '[:space:]' <"$tmp/BUILD_COMMIT" 2>/dev/null)" = "$sha" ] || { rm -rf "$tmp"; die "интерфейс в архиве собран не из $sha"; }
  chown -R root:root "$tmp"
  chmod -R u=rwX,go=rX "$tmp"
  rm -rf "$APP/frontend/dist.prev"
  if [ -d "$APP/frontend/dist" ]; then mv "$APP/frontend/dist" "$APP/frontend/dist.prev"; fi
  mv "$tmp" "$APP/frontend/dist"
}

db_snapshot() { # db_snapshot <name> — data/backups/<name>.db: a consistent copy of the live base (SQLite backup API)
  [ -f "$DATA/panel.db" ] || return 0
  as_panel mkdir -p "$DATA/backups"
  as_panel "$VENV/bin/python" - "$DATA/panel.db" "$DATA/backups/$1.db" <<'PY'
import sqlite3
import sys

src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=60)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
check = dst.execute("pragma integrity_check").fetchone()[0]
dst.close()
src.close()
sys.exit(0 if check == "ok" else f"integrity_check: {check}")
PY
}

db_restore() { # db_restore <file.db> — the service must be stopped
  systemctl is-active --quiet "$UNIT" && die "сначала остановите службу"
  as_panel cp "$1" "$DATA/panel.db.restore"
  as_panel rm -f "$DATA/panel.db-wal" "$DATA/panel.db-shm"
  as_panel mv -f "$DATA/panel.db.restore" "$DATA/panel.db"
  log "база восстановлена из $(basename "$1")"
}
