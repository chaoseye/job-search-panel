#!/usr/bin/env bash
# Deploy a commit of Job Search Panel on the server — README («Обновление и откат»). As root:
#   update.sh <sha> [--now]   the payload is in /srv/jobsearchpanel/incoming/<sha>/: repo.bundle (git bundle with the
#                             commit) and dist.tar.gz (the interface built on the laptop from that commit);
#                             the laptop sends both with `.\run.ps1 remote deploy`
#   update.sh --rollback      back to the commit before the last successful deploy (its payload is kept)
# Steps: wait until the panel is idle (--now: do not wait) → a snapshot of the base → checkout → Python deps if
# pyproject.toml changed → interface → unit files → restart → /api/health. A failed check puts the previous commit
# back (and the base snapshot, when migrations changed). A stopped service stays stopped: the code is updated only.
# The whole script is one block: the checkout replaces this file while it runs.
{
  set -euo pipefail
  . "$(dirname "$(readlink -f "$0")")/lib.sh"

  readonly WAIT_IDLE_MIN=20
  readonly BUSY_JOBS="claude_run apply import backup reprocess scan sync freshness studios_find studio_contacts"

  wait_idle() {
    local deadline=$((SECONDS + WAIT_IDLE_MIN * 60)) busy job
    while :; do
      busy=""
      for job in $(running_jobs); do
        if [[ " $BUSY_JOBS " == *" $job "* ]]; then busy+="$job "; fi
      done
      [ -z "$busy" ] && return 0
      ((SECONDS < deadline)) || die "панель занята ($busy) дольше $WAIT_IDLE_MIN мин — повторите позже или с --now"
      log "ждём окончания задач панели: $busy"
      sleep 15
    done
  }

  checkout() { # checkout <sha> — tracked files only; data/, logs/, .env, the venv and dist are not tracked
    git -C "$APP" checkout --quiet --detach --force "$1"
    chown -R root:root "$APP/.git"
  }

  deploy() { # deploy <sha> <now>
    local target=$1 now=$2 payload current was_active migrations_changed=0 snapshot=""
    payload=$INCOMING/$target
    [ -s "$payload/repo.bundle" ] && [ -s "$payload/dist.tar.gz" ] || die "нет пакета развёртывания $payload"
    git -C "$APP" bundle verify --quiet "$payload/repo.bundle" 2>/dev/null || die "пакет кода повреждён или неполон"
    git -C "$APP" fetch --quiet "$payload/repo.bundle" "+refs/*:refs/deploy/*"
    git -C "$APP" cat-file -e "$target^{commit}" 2>/dev/null || die "в пакете нет коммита $target"
    current=$(git -C "$APP" rev-parse HEAD)
    if [ "$current" = "$target" ] && [ "$(tr -d '[:space:]' <"$APP/frontend/dist/BUILD_COMMIT" 2>/dev/null)" = "$target" ]; then
      log "уже развёрнут ${target:0:12}"
      return 0
    fi
    git -C "$APP" diff --quiet "$current" "$target" -- backend/app/db/migrations || migrations_changed=1
    was_active=0
    if systemctl is-active --quiet "$UNIT"; then
      was_active=1
      [ "$now" = 1 ] || wait_idle
      snapshot="predeploy_${current:0:12}_$(date +%Y%m%d_%H%M%S)"
      db_snapshot "$snapshot" || die "не удалось снять копию базы перед обновлением"
      log "копия базы перед обновлением: data/backups/$snapshot.db"
    fi

    log "обновление ${current:0:12} → ${target:0:12}"
    checkout "$target"
    if deps_stale; then install_python_deps; fi
    install_dist "$payload/dist.tar.gz" "$target"
    install_units

    if ((was_active)); then
      systemctl restart "$UNIT"
      if wait_health 240; then
        printf '%s deploy %s -> %s ok\n' "$(date -Is)" "$current" "$target" >>"$DEPLOY_LOG"
        log "готово: панель отвечает на ${target:0:12}"
      else
        log "панель не ответила после обновления — откатываю на ${current:0:12}"
        systemctl stop "$UNIT" || true
        checkout "$current"
        if deps_stale; then install_python_deps; fi
        if [ -d "$APP/frontend/dist.prev" ]; then
          rm -rf "$APP/frontend/dist"
          mv "$APP/frontend/dist.prev" "$APP/frontend/dist"
        fi
        install_units
        if ((migrations_changed)) && [ -n "$snapshot" ]; then db_restore "$DATA/backups/$snapshot.db"; fi
        systemctl start "$UNIT"
        wait_health 240 || log "и старая версия не отвечает: journalctl -u $UNIT -n 50"
        printf '%s deploy %s -> %s rolled-back\n' "$(date -Is)" "$current" "$target" >>"$DEPLOY_LOG"
        die "обновление на ${target:0:12} отменено, работает ${current:0:12}"
      fi
    else
      printf '%s deploy %s -> %s ok (служба не запущена)\n' "$(date -Is)" "$current" "$target" >>"$DEPLOY_LOG"
      log "код обновлён; служба не запущена и не запускается"
    fi
    # keep the payloads of the last five deploys (--rollback needs the previous one) and three base snapshots; before
    # the first start there is no data/backups yet (a failing find under pipefail would fail the whole deploy)
    find "$INCOMING" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -rn | tail -n +6 | cut -d' ' -f2- |
      xargs -r rm -rf
    if [ -d "$DATA/backups" ]; then
      find "$DATA/backups" -maxdepth 1 -name 'predeploy_*.db' -printf '%T@ %p\n' | sort -rn | tail -n +4 |
        cut -d' ' -f2- | xargs -r rm -f
    fi
  }

  main() {
    [ "$(id -u)" = 0 ] || die "нужен root"
    exec 9>/run/jobsearchpanel-update.lock
    flock -n 9 || die "уже идёт другое обновление"
    local now=0 target=""
    for arg in "$@"; do
      case "$arg" in
        --now) now=1 ;;
        --rollback)
          target=$(awk '$2 == "deploy" && $NF == "ok" {prev = $3} END {print prev}' "$DEPLOY_LOG" 2>/dev/null)
          [ -n "$target" ] || die "в $DEPLOY_LOG нет успешного обновления, откатываться не на что"
          log "откат на ${target:0:12}"
          ;;
        *) [[ $arg =~ ^[0-9a-f]{40}$ ]] && target=$arg || die "нужен полный sha коммита или --rollback" ;;
      esac
    done
    [ -n "$target" ] || die "использование: update.sh <sha> [--now] | --rollback"
    deploy "$target" "$now"
  }

  main "$@"
  exit $?
}
