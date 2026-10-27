#!/usr/bin/env bash
# The reverse proxy of the public address of Job Search Panel (Caddy) — README («Вход из браузера»). As root:
#   proxy.sh install <domain>   our site → /etc/caddy/sites/jobsearchpanel.caddy (and the shared Caddyfile when Caddy
#                               still has the package's default one), the limits of caddy.service and the pin of the
#                               Caddy repository in apt (deploy/linux/caddy), validate, restart Caddy, wait for the
#                               certificate
#   proxy.sh status             Caddy, its limits and pin, what listens on 80/443/8100, the certificate, the panel
#                               behind the proxy
#   proxy.sh remove             our site out; Caddy is stopped when no other site is left
# The Caddy package, the DNS record and 443/tcp in ufw are separate steps with the owner's consent (README): this
# script installs no packages and touches neither ufw nor the sites of other services; of apt only the pin of the Caddy
# repository. The domain must equal PANEL_PUBLIC_URL in .env.
{
  set -euo pipefail
  . "$(dirname "$(readlink -f "$0")")/lib.sh"

  readonly CADDY_DIR=/etc/caddy
  readonly SITES=$CADDY_DIR/sites
  readonly SITE=$SITES/jobsearchpanel.caddy
  readonly PUBLIC_PORT=8100 # PANEL_PUBLIC_PORT of jobsearchpanel.service; registered in /srv/README
  readonly TEMPLATES=$APP/deploy/linux/caddy
  readonly DROPIN_DIR=/etc/systemd/system/caddy.service.d
  readonly DROPIN=$DROPIN_DIR/10-jobsearchpanel.conf          # the limits of caddy.service: caddy-hardening.conf
  readonly APT_LIST=/etc/apt/sources.list.d/caddy-stable.list  # Caddy from its own repository (README, «Вход из браузера»)
  readonly APT_PIN=/etc/apt/preferences.d/caddy-cloudsmith     # that repository gives caddy only: caddy-cloudsmith.pref
  readonly CAP_BIND=0000000000000400                           # CAP_NET_BIND_SERVICE alone, as in /proc/<pid>/status

  public_url() { # PANEL_PUBLIC_URL of the panel's .env (the last one wins, as in python-dotenv), without quotes
    { sed -n 's/^[[:space:]]*PANEL_PUBLIC_URL[[:space:]]*=[[:space:]]*//p' "$APP/.env" 2>/dev/null || true; } |
      tail -n 1 | tr -d "\"'\r" | sed 's:/*$::'
  }

  package_default() { # the Caddyfile of the package: one static site on :80 (or no Caddyfile at all)
    [ ! -e "$CADDY_DIR/Caddyfile" ] ||
      [ "$(sed -e 's/#.*//' "$CADDY_DIR/Caddyfile" | tr -d '[:space:]')" = ':80{root*/usr/share/caddyfile_server}' ]
  }

  validate() {
    local out
    if ! out=$(caddy validate --config "$CADDY_DIR/Caddyfile" --adapter caddyfile 2>&1); then
      printf '%s\n' "$out" | tail -n 20 >&2
      return 1
    fi
  }

  probe() { # probe <domain> — the panel's /login through Caddy on this machine, with the real certificate checked
    curl -sS -o /dev/null -w '%{http_code}' --max-time 15 --resolve "$1:443:127.0.0.1" "https://$1/login" 2>/dev/null ||
      true
  }

  waiting_for_reload() { # units of other services waiting for a daemon-reload: ours would reload them too (/srv/README,
    #                      rule 4)
    systemctl show -p Id,NeedDaemonReload '*' 2>/dev/null |
      awk -F= '/^Id=/ {id = $2} /^NeedDaemonReload=yes/ && id != "caddy.service" {printf "%s%s", sep, id; sep = ", "}'
  }

  harden() { # the limits of caddy.service and the pin of the Caddy repository. DROPIN_OLD — what the drop-in replaced
    #          now: a copy of the previous one or "none"; empty when the drop-in did not change
    DROPIN_OLD=""
    if ! cmp -s "$TEMPLATES/caddy-hardening.conf" "$DROPIN"; then
      local waiting
      waiting=$(waiting_for_reload)
      [ -z "$waiting" ] ||
        die "daemon-reload ждут юниты других сервисов ($waiting): спросите их владельцев (/srv/README, п. 4)"
      if [ -e "$DROPIN" ]; then DROPIN_OLD=$(mktemp) && cp -a "$DROPIN" "$DROPIN_OLD"; else DROPIN_OLD=none; fi
      install -d -m 0755 -o root -g root "$DROPIN_DIR"
      install -m 0644 -o root -g root "$TEMPLATES/caddy-hardening.conf" "$DROPIN"
      systemctl daemon-reload
      log "ограничения caddy.service записаны: $DROPIN"
    fi
    if [ -e "$APT_LIST" ] && ! cmp -s "$TEMPLATES/caddy-cloudsmith.pref" "$APT_PIN"; then
      install -m 0644 -o root -g root "$TEMPLATES/caddy-cloudsmith.pref" "$APT_PIN"
      log "репозиторий Caddy закреплён за пакетом caddy: $APT_PIN"
    fi
  }

  limits() { # what of caddy-hardening.conf is in force: the drop-in, the capabilities of the running Caddy, the score
    if [ ! -e "$DROPIN" ]; then
      echo "нет ($DROPIN): у Caddy права и песочница пакета, с CAP_NET_ADMIN"
      return
    fi
    local note="как в шаблоне" pid caps exposure restart memory
    cmp -s "$TEMPLATES/caddy-hardening.conf" "$DROPIN" || note="ОТЛИЧАЕТСЯ от шаблона"
    systemctl show caddy -p DropInPaths --value 2>/dev/null | grep -qF "$DROPIN" ||
      note="$note, systemd его ещё не прочёл (daemon-reload, restart caddy)"
    pid=$(systemctl show caddy -p MainPID --value 2>/dev/null || true)
    if [ "${pid:-0}" = 0 ]; then
      caps="Caddy не запущен"
    else
      caps=$(awk '$1 == "CapAmb:" {a = $2} $1 == "CapBnd:" {b = $2} END {print a "/" b}' \
        "/proc/$pid/status" 2>/dev/null || true)
      if [ "$caps" = "$CAP_BIND/$CAP_BIND" ]; then
        caps="у процесса только CAP_NET_BIND_SERVICE"
      else
        caps="у процесса CapAmb/CapBnd ${caps:-?} (ждали $CAP_BIND/$CAP_BIND: restart caddy)"
      fi
    fi
    exposure=$(systemd-analyze security caddy --no-pager 2>/dev/null |
      sed -n 's/.*caddy\.service: *\([0-9.]*\) *\([A-Z]*\).*/\1 \2/p' || true)
    restart=$(systemctl show caddy -p Restart --value 2>/dev/null || true)
    memory=$(systemctl show caddy -p MemoryMax --value 2>/dev/null || true)
    if [[ $memory =~ ^[0-9]+$ ]]; then memory="$((memory / 1048576))M"; fi
    printf '%s; %s; Restart=%s, MemoryMax=%s; systemd-analyze security: %s\n' "$note" "$caps" "${restart:-?}" \
      "${memory:-?}" "${exposure:-?}"
  }

  pin() { # whether the repository of Caddy gives this server the caddy package only
    if [ ! -e "$APT_LIST" ]; then
      echo "не нужно: Caddy не из своего репозитория"
      return
    fi
    if [ ! -e "$APT_PIN" ]; then
      echo "нет ($APT_PIN): из репозитория Caddy apt и unattended-upgrades возьмут любой пакет"
      return
    fi
    local note="как в шаблоне" others own
    cmp -s "$TEMPLATES/caddy-cloudsmith.pref" "$APT_PIN" || note="ОТЛИЧАЕТСЯ от шаблона"
    # the repository's own priority: its line in «Package files»; the one of caddy: the line of the candidate version
    # in the version table (the source line under it shows the repository's -1 again)
    others=$(LC_ALL=C apt-cache policy 2>/dev/null | awk '/dl\.cloudsmith\.io/ {print $1; exit}' || true)
    own=$(LC_ALL=C apt-cache policy caddy 2>/dev/null |
      awk '$1 == "Candidate:" {c = $2; next}
        c != "" && ($1 == c || ($1 == "***" && $2 == c)) {print c " — " $NF; exit}' || true)
    printf '%s; пакеты репозитория — приоритет %s, кандидат caddy %s\n' "$note" "${others:-?}" "${own:-?}"
  }

  [ "$(id -u)" = 0 ] || die "нужен root"
  case "${1:-}" in
    install)
      domain=${2:-}
      [[ $domain =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] ||
        die "install <домен>, например install panel.example.com"
      command -v caddy >/dev/null || die "Caddy не установлен (README, «Вход из браузера»)"
      [ "$(public_url)" = "https://$domain" ] ||
        die "в $APP/.env нужен PANEL_PUBLIC_URL=https://$domain и перезапуск панели — иначе за прокси пусто"
      if ! grep -E '^[[:space:]]*import[[:space:]]+sites/\*\.caddy' "$CADDY_DIR/Caddyfile" >/dev/null 2>&1; then
        package_default ||
          die "$CADDY_DIR/Caddyfile уже настроен под другой сервис: добавьте в него строку «import sites/*.caddy» сами"
        if [ -e "$CADDY_DIR/Caddyfile" ]; then cp -a "$CADDY_DIR/Caddyfile" "$CADDY_DIR/Caddyfile.package-default"; fi
        install -m 0644 -o root -g root "$TEMPLATES/Caddyfile" "$CADDY_DIR/Caddyfile"
        log "общий Caddyfile записан (прежний — Caddyfile.package-default)"
      fi
      install -d -m 0755 -o root -g root "$SITES"
      previous=""
      if [ -e "$SITE" ]; then previous=$(mktemp) && cp -a "$SITE" "$previous"; fi
      sed -e "s/__DOMAIN__/$domain/g" -e "s/__PORT__/$PUBLIC_PORT/g" "$TEMPLATES/jobsearchpanel.caddy" >"$SITE.new"
      chmod 0644 "$SITE.new"
      mv -f "$SITE.new" "$SITE"
      if ! validate; then
        if [ -n "$previous" ]; then mv -f "$previous" "$SITE"; else rm -f "$SITE"; fi
        die "Caddy не принял конфигурацию — прежняя возвращена"
      fi
      rm -f "${previous:-/nonexistent}"
      harden
      systemctl enable caddy >/dev/null
      if ! systemctl restart caddy; then
        if [ -n "$DROPIN_OLD" ]; then
          # the new limits are the likely cause: Caddy back as it was, and the run fails
          if [ "$DROPIN_OLD" = none ]; then
            rm -f "$DROPIN"
            rmdir "$DROPIN_DIR" 2>/dev/null || true
            back="ограничения сняты"
          else
            mv -f "$DROPIN_OLD" "$DROPIN"
            back="возвращены прежние ограничения"
          fi
          systemctl daemon-reload
          # (Restart=on-failure has put the failed Caddy in auto-restart: this restart takes the place of that one)
          systemctl restart caddy &&
            die "Caddy не запустился с новыми ограничениями: $back, Caddy работает; journalctl -u caddy -n 50"
        fi
        die "Caddy не запустился: journalctl -u caddy -n 50"
      fi
      if [ -n "$DROPIN_OLD" ] && [ "$DROPIN_OLD" != none ]; then rm -f "$DROPIN_OLD"; fi
      log "Caddy перезапущен; жду сертификат для $domain (Let's Encrypt, проверка на 443)"
      code=""
      for _ in $(seq 1 40); do
        code=$(probe "$domain")
        [ "$code" = 200 ] && break
        sleep 3
      done
      if [ "$code" = 200 ]; then
        log "готово: https://$domain/login отвечает через Caddy с действующим сертификатом"
      else
        log "https://$domain пока не отвечает (код: ${code:-нет}); journalctl -u caddy -n 50 и proxy.sh status"
        exit 1
      fi
      ;;
    status)
      printf 'caddy: %s (%s)\n' "$(systemctl is-active caddy 2>/dev/null || true)" \
        "$(systemctl is-enabled caddy 2>/dev/null || echo 'не установлен')"
      printf 'ограничения caddy.service: %s\n' "$(limits)"
      printf 'репозиторий Caddy в apt: %s\n' "$(pin)"
      ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E ':(80|443|8100)$' | sort -u | sed 's/^/слушает: /' || true
      url=$(public_url)
      if [ -z "$url" ]; then
        echo "PANEL_PUBLIC_URL в .env не задан: публичного входа у панели нет"
        exit 0
      fi
      domain=${url#https://}
      printf 'PANEL_PUBLIC_URL: %s; сайт Caddy: %s\n' "$url" "$([ -e "$SITE" ] && echo есть || echo нет)"
      printf 'https://%s/login через Caddy: %s\n' "$domain" "$(probe "$domain")"
      if command -v openssl >/dev/null; then
        openssl s_client -connect 127.0.0.1:443 -servername "$domain" </dev/null 2>/dev/null |
          openssl x509 -noout -issuer -enddate 2>/dev/null | sed 's/^/сертификат: /' || true
      fi
      ;;
    remove)
      rm -f "$SITE"
      if compgen -G "$SITES/*.caddy" >/dev/null; then
        validate || die "без сайта панели Caddy не принимает конфигурацию — посмотрите $SITES"
        systemctl restart caddy
        log "сайт панели убран, Caddy перезапущен с сайтами других сервисов"
      else
        systemctl disable --now caddy >/dev/null 2>&1 || true
        log "сайт панели убран; других сайтов нет — Caddy остановлен и выключен" \
          "(его ограничения и закрепление в apt оставлены: README, «Ограничения Caddy»)"
      fi
      ;;
    *) die "использование: proxy.sh install <домен> | status | remove" ;;
  esac
  exit 0
}
