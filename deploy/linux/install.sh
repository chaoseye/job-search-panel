#!/usr/bin/env bash
# First installation of Job Search Panel on a Linux server (Ubuntu 24.04+, systemd) — README. As root:
#   install.sh <sha> <payload dir>     the payload holds repo.bundle and dist.tar.gz (as for update.sh)
# Creates the users and directories, the code checkout, the venv, the interface, the unit files, the logrotate rule
# and /srv/README. Starts nothing: data/ and .env come with the move (README, «Переезд»), then
# `systemctl enable --now jobsearchpanel jobsearchpanel-health.timer`. Safe to run again.
{
  set -euo pipefail
  here=$(dirname "$(readlink -f "$0")")
  . "$here/lib.sh"

  [ "$(id -u)" = 0 ] || die "нужен root"
  sha=${1:-}
  payload=${2:-}
  [[ $sha =~ ^[0-9a-f]{40}$ ]] && [ -s "$payload/repo.bundle" ] && [ -s "$payload/dist.tar.gz" ] ||
    die "использование: install.sh <sha> <каталог с repo.bundle и dist.tar.gz>"
  command -v systemctl >/dev/null || die "нужен systemd"
  python3 -c 'import sys, venv, ensurepip; sys.exit(sys.version_info < (3, 11))' 2>/dev/null ||
    die "нужен Python 3.11+ с модулем venv (Ubuntu: apt install python3-venv)"
  for tool in git curl setpriv flock logrotate gzip tar; do command -v "$tool" >/dev/null || die "нет $tool"; done

  # users: jobpanel runs the panel; jobremote only forwards the tunnel to 127.0.0.1:8000 and hands out the newest backup
  # (both through restricted keys; jobremote needs a shell for the forced command and has no password)
  getent group "$SERVICE_USER" >/dev/null || groupadd --system "$SERVICE_USER"
  id -u "$SERVICE_USER" >/dev/null 2>&1 ||
    useradd --system --gid "$SERVICE_USER" --home-dir "$PANEL_HOME" --no-create-home --shell /usr/sbin/nologin \
      --comment "Job Search Panel service" "$SERVICE_USER"
  id -u "$REMOTE_USER" >/dev/null 2>&1 ||
    useradd --system --user-group --groups "$SERVICE_USER" --home-dir "$REMOTE_HOME" --no-create-home \
      --shell /bin/sh --comment "Job Search Panel tunnel and backup export" "$REMOTE_USER"
  usermod --password '*' "$REMOTE_USER" # no password at all; «!» would read as a locked account to sshd

  install -d -m 0755 -o root -g root "$BASE"
  install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$PANEL_HOME"
  install -d -m 0700 -o root -g root "$INCOMING"
  install -d -m 0755 -o root -g root "$REMOTE_HOME" "$REMOTE_HOME/.ssh"
  [ -f "$REMOTE_HOME/.ssh/authorized_keys" ] || install -m 0644 -o root -g root /dev/null "$REMOTE_HOME/.ssh/authorized_keys"

  # the code comes only as bundles from the laptop: no remote, no GitHub credentials on the server
  if [ ! -d "$APP/.git" ]; then git init --quiet "$APP"; fi
  git -C "$APP" fetch --quiet "$payload/repo.bundle" "+refs/*:refs/deploy/*"
  git -C "$APP" checkout --quiet --detach --force "$sha"
  chmod 0755 "$APP"
  # data/ and logs/ belong to the service (a repeated install leaves their contents alone)
  install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA" "$LOGS"
  find "$APP/deploy/linux" -name '*.sh' -exec chmod 0755 {} +

  if deps_stale; then install_python_deps; fi
  install_dist "$payload/dist.tar.gz" "$sha"
  install_units

  if [ ! -e /srv/README ]; then
    install -m 0644 -o root -g root "$APP/deploy/linux/srv-README.md" /srv/README
    log "соглашение для сервисов записано в /srv/README"
  else
    log "/srv/README уже есть — строку Job Search Panel в нём проверьте вручную"
  fi
  if [ ! -e /etc/motd.d/50-srv-readme ]; then
    install -d -m 0755 /etc/motd.d
    printf 'Сервисы этого сервера, их каталоги, пользователи и порты: /srv/README\n' >/etc/motd.d/50-srv-readme
    chmod 0644 /etc/motd.d/50-srv-readme
  fi

  printf '%s install %s ok\n' "$(date -Is)" "$sha" >>"$DEPLOY_LOG"
  log "установлено ${sha:0:12}; служба не запущена. Дальше — README: Claude Code, ключи, переезд"
  exit 0
}
