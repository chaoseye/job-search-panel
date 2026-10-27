"""Job Search Panel on the server, from the laptop — README. Called by run.ps1 as `.\\run.ps1 remote <command>`.

Settings: .panel-remote.json at the project root (not in git). Three SSH keys with narrow rights:
  tunnel_key  jobremote@server, only the forwarding to 127.0.0.1:<port>          → open, close
  deploy_key  root@server, only the verbs of deploy/linux/remote.sh            → deploy, status, logs, … , move-*
  backup_key  jobremote@server, only the newest backup (backup-export.sh)       → pull-backup
`panel_on_server: true` in the settings means the working panel is the server's: run.ps1 then refuses to start a
local one (two panels would revoke each other's hh.ru app token and fight over the bot's getUpdates).

Standard library only: it runs with backend/.venv (or any Python 3.11+) and must work when the panel itself is broken.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".panel-remote.json"
# not %LOCALAPPDATA%: inside a packaged app (the Claude desktop app) it points into the package's own container, and a
# scheduled task would look elsewhere
STATE_DIR = Path.home() / ".jobsearchpanel"
PANEL_TASK = "JobSearchPanel"
BACKUP_TASK = "JobSearchPanel-backup-pull"
REMOTE_APP = "/srv/jobsearchpanel/app"
DEFAULTS: dict[str, Any] = {
    "host": "",
    "port": 8000,
    "remote_user": "jobremote",
    "deploy_user": "root",
    "tunnel_key": "~/.ssh/jobpanel_tunnel",
    "deploy_key": "~/.ssh/jobpanel_deploy",
    "backup_key": "~/.ssh/jobpanel_backup",
    "branch": "origin/master",
    "backups_dir": "~/JobSearchPanel-backups",
    "backups_keep": 14,
    "panel_on_server": False,
}
WINDOWS = sys.platform == "win32"
# local requests never go through a system proxy (a VPN client may set one): the tunnel listens on 127.0.0.1
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Fail(Exception):
    pass


def say(text: str) -> None:
    print(text, flush=True)


# ---- settings, ssh ------------------------------------------------------------------------------------------------


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    if not cfg["host"]:
        raise Fail(f"нет адреса сервера: заполните {CONFIG.name} (пример — README)")
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def key_path(cfg: dict[str, Any], name: str) -> Path:
    path = Path(os.path.expanduser(str(cfg[name])))
    if not path.exists():
        raise Fail(f"нет ключа {path} — `.\\run.ps1 remote keys` создаст ключи (README)")
    return path


def openssh(tool: str) -> str:
    """The OpenSSH client of Windows first: the same one in a console, a scheduled task (no Git in its PATH) and Git
    Bash; elsewhere the one in PATH."""
    if WINDOWS:
        system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / f"{tool}.exe"
        if system.exists():
            return str(system)
    found = shutil.which(tool)
    if not found:
        raise Fail(f"нет {tool} (Windows: «Дополнительные компоненты» → «Клиент OpenSSH»)")
    return found


def ssh_base(cfg: dict[str, Any], key: str, user: str, *options: str) -> list[str]:
    exe = openssh("ssh")
    return [
        exe,
        "-i",
        str(key_path(cfg, key)),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=20",
        *options,
        f"{user}@{cfg['host']}",
    ]


def server(cfg: dict[str, Any], *verb: str, data: bytes | None = None, out: Any = None, check: bool = True) -> int:
    """A verb of deploy/linux/remote.sh; its messages go to this console."""
    cmd = ssh_base(cfg, "deploy_key", cfg["deploy_user"]) + list(verb)
    proc = subprocess.run(cmd, input=data, stdout=out, check=False)
    if check and proc.returncode != 0:
        raise Fail(f"сервер ответил ошибкой на «{' '.join(verb)}» (код {proc.returncode})")
    return proc.returncode


# ---- the local panel and the tunnel ---------------------------------------------------------------------------


def local_request(cfg: dict[str, Any], path: str, body: dict[str, Any] | None = None, timeout: float = 5) -> Any:
    url = f"http://127.0.0.1:{cfg['port']}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with LOCAL.open(req, timeout=timeout) as resp:
        return json.load(resp)


def health(cfg: dict[str, Any], timeout: float = 4) -> dict[str, Any] | None:
    try:
        answer = local_request(cfg, "/api/health", timeout=timeout)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return answer if isinstance(answer, dict) and answer.get("ok") else None


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def powershell(script: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and proc.returncode != 0:
        raise Fail(f"PowerShell: {proc.stderr.strip()[:400]}")
    return proc.stdout


def _listener(port: int) -> str:
    return (
        f"$c = Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue"
        " | Select-Object -First 1;"
    )


def listener_process(port: int) -> str:
    """Name of the process listening on 127.0.0.1:<port> (empty when free)."""
    if not WINDOWS:
        return ""
    out = powershell(_listener(port) + " if ($c) { (Get-Process -Id $c.OwningProcess).ProcessName }", check=False)
    return out.strip()


def kill_listener(port: int) -> None:
    powershell(_listener(port) + " if ($c) { Stop-Process -Id $c.OwningProcess -Force }", check=False)
    for _ in range(20):
        if not port_busy(port):
            return
        time.sleep(0.5)
    raise Fail(f"порт {port} не освободился")


def open_tunnel(cfg: dict[str, Any]) -> None:
    port = int(cfg["port"])
    if health(cfg):
        return
    if port_busy(port):
        raise Fail(f"порт {port} занят ({listener_process(port) or 'другой программой'}), но панель на нём не отвечает")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = ssh_base(
        cfg,
        "tunnel_key",
        cfg["remote_user"],
        "-N",
        "-L",
        f"127.0.0.1:{port}:127.0.0.1:{port}",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
    )
    log = (STATE_DIR / "tunnel.log").open("ab")
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if WINDOWS else 0
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log, creationflags=flags)
    for _ in range(40):
        time.sleep(0.5)
        if health(cfg):
            return
    log_path = STATE_DIR / "tunnel.log"
    raise Fail(f"туннель не поднялся за 20 с — см. {log_path}; состояние панели — `.\\run.ps1 remote status`")


def close_tunnel(cfg: dict[str, Any]) -> None:
    port = int(cfg["port"])
    name = listener_process(port)
    if not name:
        return
    if name.lower() != "ssh":
        raise Fail(f"порт {port} слушает {name}, а не туннель — не трогаю")
    kill_listener(port)


# ---- commands --------------------------------------------------------------------------------------------------


def cmd_open(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    open_tunnel(cfg)
    url = f"http://localhost:{cfg['port']}"
    if not args.no_browser:
        webbrowser.open(url)
    say(f"Панель сервера открыта через туннель: {url}")


def cmd_close(cfg: dict[str, Any], _args: argparse.Namespace) -> None:
    close_tunnel(cfg)
    say("Туннель закрыт")


def cmd_passthrough(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    server(cfg, args.command, *args.rest)


def git(*args: str, cwd: Path = ROOT, capture: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=capture, text=True, check=False)
    if proc.returncode != 0:
        raise Fail(f"git {' '.join(args)}: {(proc.stderr or '').strip()[:400]}")
    return (proc.stdout or "").strip()


def ci_state(sha: str) -> str:
    """Conclusion of the CI run of the commit on GitHub (gh), `unknown` without gh."""
    gh = shutil.which("gh")
    if not gh:
        return "unknown"
    proc = subprocess.run(
        [gh, "run", "list", "--commit", sha, "--workflow", "ci.yml", "--json", "status,conclusion", "--limit", "5"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return "unknown"
    runs = json.loads(proc.stdout or "[]")
    if any(r.get("conclusion") == "success" for r in runs):
        return "success"
    if any(r.get("status") != "completed" for r in runs):
        return "running"
    return runs[0].get("conclusion", "none") if runs else "none"


def build_interface(sha: str) -> bytes:
    """frontend/dist of the commit, built in a worktree of its own; BUILD_COMMIT inside. Returns dist.tar.gz."""
    build = STATE_DIR / "deploy-build"
    if not (build / ".git").exists():
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        git("worktree", "add", "--force", "--detach", str(build), sha)
    else:
        git("checkout", "--quiet", "--detach", "--force", sha, cwd=build)
        git("clean", "-fdq", "-e", "frontend/node_modules", cwd=build)
    frontend = build / "frontend"
    npm = shutil.which("npm") or "npm"
    lock = hashlib.sha256((frontend / "package-lock.json").read_bytes()).hexdigest()
    stamp = frontend / "node_modules" / ".deploy-stamp"
    if not stamp.exists() or stamp.read_text(encoding="utf-8").strip() != lock:
        say("npm ci …")
        subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=frontend, check=True)
        stamp.write_text(lock, encoding="utf-8")
    say("сборка интерфейса …")
    subprocess.run([npm, "run", "build"], cwd=frontend, check=True)
    dist = frontend / "dist"
    (dist / "BUILD_COMMIT").write_text(sha + "\n", encoding="utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(dist.rglob("*")):
            tar.add(path, arcname=path.relative_to(dist).as_posix(), recursive=False)
    return buf.getvalue()


def make_bundle(sha: str) -> bytes:
    ref = "refs/jobpanel-deploy/target"
    git("update-ref", ref, sha)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repo.bundle"
            git("bundle", "create", str(path), ref)
            return path.read_bytes()
    finally:
        git("update-ref", "-d", ref)


def cmd_deploy(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    say("git fetch …")
    git("fetch", "--quiet", "origin")
    ref = args.ref or cfg["branch"]
    sha = git("rev-parse", "--verify", f"{ref}^{{commit}}")
    if not args.force:
        base = cfg["branch"]
        if subprocess.run(["git", "merge-base", "--is-ancestor", sha, base], cwd=ROOT, check=False).returncode != 0:
            raise Fail(f"{sha[:12]} нет в {base}: на сервер идёт только слитый код (--force — без этой проверки)")
        state = ci_state(sha)
        if state != "success":
            raise Fail(f"CI коммита {sha[:12]}: {state} — дождитесь зелёного прогона (--force — без проверки)")
    say(f"развёртывание {sha[:12]} ({git('log', '-1', '--format=%s', sha)})")
    dist = build_interface(sha)
    bundle = make_bundle(sha)
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as tar:
        for name, blob in (("repo.bundle", bundle), ("dist.tar.gz", dist)):
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(blob))
    server(cfg, "deploy", sha, *(["--now"] if args.now else []), data=payload.getvalue())


def check_base(path: Path) -> dict[str, Any]:
    con = sqlite3.connect(path)
    try:
        result = con.execute("pragma integrity_check").fetchone()[0]
        if result != "ok":
            raise Fail(f"{path.name}: integrity_check — {result}")
        counts = {t: con.execute(f"select count(*) from {t}").fetchone()[0] for t in ("vacancies", "applications")}
        counts["revision"] = con.execute("select version_num from alembic_version").fetchone()[0]
        return counts
    finally:
        con.close()


def cmd_pull_backup(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    dest = Path(os.path.expanduser(str(cfg["backups_dir"])))
    dest.mkdir(parents=True, exist_ok=True)
    base = ssh_base(cfg, "backup_key", cfg["remote_user"])
    listing = subprocess.run(base + ["list"], capture_output=True, text=True, check=False)
    names = [n.strip() for n in listing.stdout.splitlines() if n.strip()]
    if listing.returncode != 0 or not names:
        raise Fail(f"список копий не получен: {listing.stderr.strip()[:300] or 'копий нет'}")
    if (dest / f"{names[0]}.gz").exists() and not args.force:
        say(f"свежая копия уже здесь: {dest / (names[0] + '.gz')}")
        return
    tmp = dest / ".incoming.gz"
    with tmp.open("wb") as fh:
        proc = subprocess.run(base + ["latest"], stdout=fh, stderr=subprocess.PIPE, check=False)
    err = proc.stderr.decode("utf-8", "replace")
    name = next((ln.split(":", 1)[1].strip() for ln in err.splitlines() if ln.startswith("name:")), "")
    if proc.returncode != 0 or not name.startswith("panel_") or "/" in name or "\\" in name:
        tmp.unlink(missing_ok=True)
        raise Fail(f"копия не получена: {err.strip()[:300]}")
    with tempfile.TemporaryDirectory() as work:
        db = Path(work) / name
        with gzip.open(tmp, "rb") as src, db.open("wb") as out:
            shutil.copyfileobj(src, out)
        counts = check_base(db)
    target = dest / f"{name}.gz"
    os.replace(tmp, target)
    keep = int(cfg["backups_keep"])
    for old in sorted(dest.glob("panel_*.db.gz"), reverse=True)[keep:]:
        old.unlink()
    line = f"{datetime.now():%Y-%m-%d %H:%M} {name}: {target.stat().st_size // 1024} КБ, {counts}"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "pull-backup.log").open("a", encoding="utf-8") as log:
        log.write(line + "\n")
    say(f"копия проверена и сохранена: {target}\n{line}")


def cmd_backup_task(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    if args.action == "remove":
        powershell(f"Unregister-ScheduledTask -TaskName '{BACKUP_TASK}' -Confirm:$false")
        say(f"задача «{BACKUP_TASK}» удалена")
        return
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else Path(sys.executable)
    script = Path(__file__).resolve()
    powershell(
        f"$a = New-ScheduledTaskAction -Execute '{runner}' -Argument '\"{script}\" pull-backup'"
        f" -WorkingDirectory '{ROOT}';"
        " $daily = New-ScheduledTaskTrigger -Daily -At 12:00;"
        " $logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; $logon.Delay = 'PT10M';"
        " $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries"
        " -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew;"
        f" Register-ScheduledTask -TaskName '{BACKUP_TASK}' -Action $a -Trigger $daily,$logon -Settings $s -Force"
        " | Out-Null"
    )
    say(f"задача «{BACKUP_TASK}»: каждый день в 12:00 и через 10 минут после входа; пропущенный запуск — при включении")


def cmd_keys(cfg: dict[str, Any], _args: argparse.Namespace) -> None:
    """Creates the three keys (when missing) and prints the lines for the server (README, «Ключи»)."""
    keygen = openssh("ssh-keygen")
    host = socket.gethostname()
    pubs = {}
    for name, comment in (("tunnel_key", "tunnel"), ("deploy_key", "deploy"), ("backup_key", "backup")):
        path = Path(os.path.expanduser(str(cfg[name])))
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [keygen, "-q", "-t", "ed25519", "-N", "", "-C", f"jobpanel-{comment}@{host}", "-f", str(path)],
                check=True,
            )
            say(f"создан ключ {path}")
        pubs[name] = Path(str(path) + ".pub").read_text(encoding="utf-8").strip()
    port = int(cfg["port"])
    say("\n# /srv/jobsearchpanel/remote/.ssh/authorized_keys (пользователь jobremote)")
    say(f'restrict,port-forwarding,permitopen="127.0.0.1:{port}",command="/usr/sbin/nologin" {pubs["tunnel_key"]}')
    say(f'restrict,command="{REMOTE_APP}/deploy/linux/backup-export.sh" {pubs["backup_key"]}')
    say("\n# /root/.ssh/authorized_keys2 (root)")
    say(f'restrict,command="{REMOTE_APP}/deploy/linux/remote.sh" {pubs["deploy_key"]}')


def wait_idle(cfg: dict[str, Any], minutes: int) -> None:
    deadline = time.monotonic() + minutes * 60
    while True:
        h = health(cfg)
        jobs = (h or {}).get("running_jobs") or []
        if not jobs:
            return
        if time.monotonic() > deadline:
            raise Fail(f"задачи панели не закончились за {minutes} мин: {', '.join(jobs)}")
        say(f"ждём окончания задач: {', '.join(jobs)}")
        time.sleep(15)


def pack_data() -> tuple[bytes, str]:
    """data/panel.db (checkpointed), exchange, uploads, the probe report and .env as tar.gz; old backups stay here."""
    db = ROOT / "data" / "panel.db"
    con = sqlite3.connect(db, timeout=60)
    try:
        con.execute("pragma wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    counts = check_base(db)
    say(f"база проверена: {counts}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(db, arcname="data/panel.db")
        for extra in ("data/exchange", "data/uploads", "data/probe_report.json"):
            if (ROOT / extra).exists():
                tar.add(ROOT / extra, arcname=extra)
        tar.add(ROOT / ".env", arcname=".env")
    blob = buf.getvalue()
    return blob, hashlib.sha256(blob).hexdigest()


def stop_local_panel(cfg: dict[str, Any]) -> None:
    if WINDOWS:
        powershell(f"Disable-ScheduledTask -TaskName '{PANEL_TASK}' -ErrorAction SilentlyContinue | Out-Null", False)
    name = listener_process(int(cfg["port"]))
    if name and name.lower() == "ssh":
        raise Fail("на порту панели открыт туннель — закройте его (`.\\run.ps1 remote close`)")
    if name:
        kill_listener(int(cfg["port"]))


def start_local_panel(cfg: dict[str, Any]) -> None:
    powershell(
        f"Enable-ScheduledTask -TaskName '{PANEL_TASK}' -ErrorAction SilentlyContinue | Out-Null;"
        f" Start-ScheduledTask -TaskName '{PANEL_TASK}' -ErrorAction SilentlyContinue",
        check=False,
    )
    for _ in range(60):
        if health(cfg):
            return
        time.sleep(1)
    say("локальная панель не ответила за минуту — запустите ярлыком или `.\\run.ps1 open`")


def cmd_move_to_server(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    if not args.yes:
        raise Fail("переезд останавливает панель на ноутбуке — повторите с --yes (порядок — README)")
    if cfg["panel_on_server"]:
        raise Fail("по настройкам панель уже на сервере")
    env, db = ROOT / ".env", ROOT / "data" / "panel.db"
    if not env.exists() or not db.exists():
        raise Fail("на ноутбуке нет .env или data/panel.db — переносить нечего")
    say("1. состояние сервера:")
    server(cfg, "status")
    local = health(cfg)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    if local:
        say("2. пауза фоновых задач панели на ноутбуке и ожидание текущих")
        local_request(cfg, "/api/jobs/pause", {"reason": "переезд на сервер"})
        wait_idle(cfg, args.wait_min)
    say("3. остановка панели на ноутбуке (задача планировщика выключена, не удалена)")
    stop_local_panel(cfg)
    moved = False
    try:
        blob, digest = pack_data()
        say(f"4. на сервер: {len(blob) // 1024} КБ, sha256 {digest[:16]}…")
        server(cfg, "import-data", digest, data=blob)
        say("5. на ноутбуке .env и база убраны из рабочего места (откат — `.\\run.ps1 remote move-back --yes`)")
        env.rename(ROOT / f".env.moved-to-server-{stamp}")
        db.rename(db.with_name(f"panel.db.moved-to-server-{stamp}"))
        for side in ("panel.db-wal", "panel.db-shm"):
            (ROOT / "data" / side).unlink(missing_ok=True)
        moved = True
        cfg["panel_on_server"] = True
        save_config(cfg)
        say("6. запуск на сервере")
        server(cfg, "start")
        open_tunnel(cfg)
        local_request(cfg, "/api/jobs/resume", {}, timeout=30)
        h = health(cfg) or {}
        login = "да" if (h.get("auth") or {}).get("logged_in") else "нет"
        say(f"готово: панель работает на сервере, вход hh — {login}, пауза — {h.get('paused_reason') or 'нет'}")
        say("откройте её ярлыком «Job Search Panel» или `.\\run.ps1 open`")
    except Exception:
        if not moved:
            say("переезд прерван до запуска на сервере — возвращаю панель на ноутбук")
            start_local_panel(cfg)
            if health(cfg):
                local_request(cfg, "/api/jobs/resume", {}, timeout=30)
        raise


def cmd_move_back(cfg: dict[str, Any], args: argparse.Namespace) -> None:
    if not args.yes:
        raise Fail("возврат останавливает и выключает панель на сервере — повторите с --yes")
    close_tunnel(cfg)
    if port_busy(int(cfg["port"])):
        raise Fail(f"порт {cfg['port']} занят — освободите его")
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    with tempfile.TemporaryDirectory() as work:
        archive = Path(work) / "data.tar.gz"
        say("1. сервер: остановка, выключение и выгрузка данных")
        with archive.open("wb") as fh:
            server(cfg, "export-data", "--stop", out=fh)
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
            if "data/panel.db" not in names or ".env" not in names:
                raise Fail("в выгрузке нет data/panel.db или .env")
            if any(not (n == ".env" or n.startswith("data/")) for n in names):
                raise Fail("в выгрузке лишние файлы")
            aside = ROOT / "data" / f"before-move-back-{stamp}"
            aside.mkdir(parents=True)
            local = (".env", "data/panel.db", "data/panel.db-wal", "data/panel.db-shm", "data/exchange", "data/uploads")
            for rel in local:
                if (ROOT / rel).exists():
                    shutil.move(str(ROOT / rel), str(aside / Path(rel).name))
            say(f"2. прежние локальные файлы — в {aside}")
            tar.extractall(ROOT, filter="data")
    say(f"3. база с сервера: {check_base(ROOT / 'data' / 'panel.db')}")
    cfg["panel_on_server"] = False
    save_config(cfg)
    say("4. запуск панели на ноутбуке")
    start_local_panel(cfg)
    say("готово: панель снова на ноутбуке; служба на сервере выключена (данные там остались)")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=".\\run.ps1 remote", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("open", help="туннель к панели сервера и браузер")
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_open)
    sub.add_parser("close", help="закрыть туннель").set_defaults(func=cmd_close)
    for verb, text in (
        ("status", "состояние панели на сервере"),
        ("logs", "хвост logs/app.log и журнала службы"),
        ("restart", "перезапустить панель"),
        ("stop", "остановить панель"),
        ("start", "запустить панель"),
        ("backup", "резервная копия сейчас"),
        ("rollback", "вернуть предыдущую версию кода"),
        ("claude-check", "проверить claude -p в песочнице службы"),
    ):
        p = sub.add_parser(verb, help=text)
        p.add_argument("rest", nargs="*")
        p.set_defaults(func=cmd_passthrough)
    p = sub.add_parser("deploy", help="выложить на сервер коммит (по умолчанию origin/master)")
    p.add_argument("ref", nargs="?")
    p.add_argument("--now", action="store_true", help="не ждать окончания задач панели")
    p.add_argument("--force", action="store_true", help="без проверки master и CI")
    p.set_defaults(func=cmd_deploy)
    p = sub.add_parser("pull-backup", help="забрать свежую резервную копию с сервера")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_pull_backup)
    p = sub.add_parser("backup-task", help="задача Windows: забирать копию каждый день")
    p.add_argument("action", nargs="?", choices=("install", "remove"), default="install")
    p.set_defaults(func=cmd_backup_task)
    sub.add_parser("keys", help="создать ключи и показать строки для сервера").set_defaults(func=cmd_keys)
    p = sub.add_parser("move-to-server", help="перенести работающую панель с ноутбука на сервер")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--wait-min", type=int, default=40, help="сколько ждать окончания задач панели")
    p.set_defaults(func=cmd_move_to_server)
    p = sub.add_parser("move-back", help="вернуть панель с сервера на ноутбук")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_move_back)
    args = parser.parse_args(argv)
    try:
        args.func(load_config(), args)
    except Fail as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        command = " ".join(map(str, exc.cmd))[:300]
        print(f"Ошибка: команда завершилась с кодом {exc.returncode}: {command}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
