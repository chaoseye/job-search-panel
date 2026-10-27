# Сервисы этого сервера

Соглашение, чтобы сервисы на сервере не мешали друг другу. Новый сервис сначала записывается в таблицы ниже,
потом запускается. Файл ведут сервисы на сервере, вручную; Job Search Panel кладёт этот шаблон
(`deploy/linux/srv-README.md`) только туда, где /srv/README ещё нет.

## Сервисы

| Сервис | Пользователи | Каталог | Юниты systemd | Порт (127.0.0.1) | Логи | Резервные копии |
|---|---|---|---|---|---|---|
| Job Search Panel | `jobpanel` (служба), `jobremote` (туннель и выгрузка копии) | `/srv/jobsearchpanel` | `jobsearchpanel`, `jobsearchpanel-health.timer`, `jobsearchpanel-alert`; свой сайт в общем `caddy` | 8000, 8100 (вход для Caddy) | `/srv/jobsearchpanel/app/logs`, `journalctl -u jobsearchpanel` | `app/data/backups` (ночью, 14 шт.) + копия на компьютере владельца (`remote pull-backup`) |

Документация панели: `/srv/jobsearchpanel/app/README.md`, раздел «Работа на сервере».

## Реестр портов (только 127.0.0.1)

| Порт | Кто |
|---|---|
| 8000 | `jobsearchpanel`: туннель с компьютера владельца |
| 8100 | `jobsearchpanel`, публичный вход: сюда ведёт Caddy с `<домен панели>`, без входа по ссылке бота пускает только к странице входа |
| 8765 | `jobsearchpanel`: песочница проверки восстановления (`restore-test.sh`, на время проверки) |

Для новых сервисов порты 8100–8199 (8100 занят): возьмите следующий свободный и запишите сюда до запуска.

## Порты снаружи (все интерфейсы)

| Порт | Кто | Правило ufw |
|---|---|---|
| 22/tcp | sshd | общее |
| 443/tcp | `caddy`, HTTPS-прокси сервера; сайты: `<домен панели>` → 127.0.0.1:8100 (Job Search Panel) | Job Search Panel, если включён вход из браузера |

80/tcp и UDP 443 закрыты: сертификаты Caddy получает проверкой TLS-ALPN на 443, HTTP/3 выключен. Новый публичный сайт —
файлом в `/etc/caddy/sites/`, новый порт снаружи открывается только с согласия владельца сервера.

## Общие компоненты, которые сервисы дополнили своими файлами

| Файл | Сервис | Что делает |
|---|---|---|
| `/etc/ssh/sshd_config.d/10-jobsearchpanel.conf` | Job Search Panel | `Match User jobremote`: только ему и только перенаправление на 127.0.0.1:8000 (туннель с компьютера владельца) |
| `/root/.ssh/authorized_keys2` | Job Search Panel | ключ развёртывания: только команды `app/deploy/linux/remote.sh` (`restrict,command=`) |
| `/etc/logrotate.d/jobsearchpanel` | Job Search Panel | ротация `app/logs/claude_run.log` |
| `/etc/motd.d/50-srv-readme` | Job Search Panel | строка при входе: где этот файл |
| ufw: `443/tcp` (IPv4 и IPv6) | Job Search Panel | HTTPS для Caddy, если включён вход из браузера |
| apt: `caddy` из официального репозитория (`/etc/apt/sources.list.d/caddy-stable.list`, ключ `/usr/share/keyrings/caddy-stable-archive-keyring.gpg`), `/etc/apt/apt.conf.d/52jobsearchpanel-caddy` | Job Search Panel | общий прокси на 443; unattended-upgrades обновляет и его (origin `cloudsmith/caddy/stable`) |
| `/etc/caddy/Caddyfile` | Job Search Panel | без admin API (`admin off`), без порта 80 и HTTP/3; сайты сервисов подключает `import sites/*.caddy`; после правки нужны `caddy validate --config /etc/caddy/Caddyfile` и `systemctl restart caddy` |
| `/etc/caddy/sites/jobsearchpanel.caddy` | Job Search Panel | `<домен панели>` → 127.0.0.1:8100; пишет `app/deploy/linux/proxy.sh install` |
| `/etc/systemd/system/caddy.service.d/10-jobsearchpanel.conf` | Job Search Panel | ограничения службы `caddy`: из прав только `CAP_NET_BIND_SERVICE` (без `CAP_NET_ADMIN` — он для HTTP/3, а HTTP/3 выключен), песочница systemd, запись только в `/var/lib/caddy`; `Restart=on-failure`, `MemoryMax=256M`, `CPUWeight=50`, `CPUQuota=100%`; ставит `app/deploy/linux/proxy.sh install`; после правки — п. 4, `daemon-reload` и `restart caddy` |
| `/etc/apt/preferences.d/caddy-cloudsmith` | Job Search Panel | из репозитория Caddy (`dl.cloudsmith.io`) apt берёт только пакет `caddy` (приоритет 500), остальные его пакеты — -1: их не ставят ни apt, ни unattended-upgrades; ставит `proxy.sh install` |

Перед правкой общего компонента посмотрите эту таблицу и файлы других сервисов: менять чужой файл нельзя,
свой можно только с проверкой (`sshd -t`, `sshd -T -C user=…`, `logrotate -d`) и `reload`, не `restart`.

## Правила

1. Свой системный пользователь без входа (`useradd --system --shell /usr/sbin/nologin`) и свой каталог `/srv/<имя>`.
2. Слушать только 127.0.0.1. Снаружи открыты SSH и 443 (ufw); веб-интерфейсы открываются через SSH-туннель или через общий
   Caddy своим файлом в `/etc/caddy/sites/` и своим портом из реестра.
3. Свой юнит `/etc/systemd/system/<имя>*.service`: `User=`, `Restart=`, `MemoryMax=`, `CPUQuota=`, песочница
   (`ProtectSystem=strict`, `ReadWritePaths=` только для своих данных). Часы процесса задаёт `Environment=TZ=…` по часовому
   поясу владельца; системные часы остаются UTC.
4. Перед `systemctl daemon-reload` проверить, что чужие юниты не ждут перезагрузки:
   `systemctl show -p Id,NeedDaemonReload '*' | grep -B1 'NeedDaemonReload=yes'`; если ждут, спросить их владельца.
5. Общие компоненты (ufw, sshd, journald, logrotate, cron) меняются только отдельным файлом своего сервиса
   (`/etc/logrotate.d/<имя>`, `/etc/ssh/sshd_config.d/<NN>-<имя>.conf`), с проверкой (`logrotate -d`, `sshd -t`) и с
   согласия владельца сервера.
6. Без глобальных `pip install` и `npm -g`: venv и инструменты живут в каталоге сервиса.
7. Секреты хранятся в файле сервиса с правами 600 и владельцем-пользователем сервиса; не в юните, не в журнале, не в git.
8. SSH-ключи для сервисов кладутся отдельными файлами (`/root/.ssh/authorized_keys2`, домашний каталог служебного
   пользователя) и с ограничениями (`restrict`, `command=`, `permitopen=`); `/root/.ssh/authorized_keys` не трогать.
9. Нагрузка: сборки и тесты запускаются с `nice -n 19 ionice -c3`; у фоновых сервисов `CPUWeight=`/`Nice=` ниже, чем у
   чувствительных к задержкам.
