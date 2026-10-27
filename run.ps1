param(
    [Parameter(Position = 0)]
    [string]$Command = "start",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$Venv = Join-Path $Backend ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$TaskName = "JobSearchPanel"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Ensure-Env {
    $envFile = Join-Path $Root ".env"
    if (-not (Test-Path $envFile)) {
        Copy-Item (Join-Path $Root ".env.example") $envFile
        Write-Host "Создан .env из .env.example. Заполните HH_CLIENT_ID, HH_CLIENT_SECRET и почту в HH_USER_AGENT." -ForegroundColor Yellow
    }
}

function Get-Port {
    # APP_PORT из .env (по умолчанию 8000); Redirect URI приложения hh должен совпадать с портом
    $envFile = Join-Path $Root ".env"
    if (Test-Path $envFile) {
        $line = Get-Content $envFile | Where-Object { $_ -match '^\s*APP_PORT\s*=\s*(\d+)' } | Select-Object -Last 1
        if ($line -and ($line -match '^\s*APP_PORT\s*=\s*(\d+)')) { return [int]$Matches[1] }
    }
    return 8000
}

function Get-PanelProcess {
    # процесс, который слушает порт панели (None, если панель не запущена)
    $port = Get-Port
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) { return Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue }
    return $null
}

function Ensure-Backend {
    if (-not (Test-Path $Py)) {
        Write-Host "Создаю виртуальное окружение backend/.venv ..."
        python -m venv $Venv
    }
    # pip install (~20 с) только когда зависимости могли измениться: нет отметки или pyproject.toml новее неё
    $stamp = Join-Path $Venv ".deps-stamp"
    $pyproject = Join-Path $Backend "pyproject.toml"
    if (-not (Test-Path $stamp) -or ((Get-Item $pyproject).LastWriteTimeUtc -gt (Get-Item $stamp).LastWriteTimeUtc)) {
        # тайм-аут 60 с вместо 15: 22.10 установка в свежем worktree упала на ReadTimeoutError до PyPI, а отметка
        # всё равно встала, и следующие запуски установку пропускали; теперь отметка — только после успешного pip
        & $Py -m pip install --quiet --disable-pip-version-check --timeout 60 --retries 5 -e "$Backend[dev]"
        if ($LASTEXITCODE -ne 0) { throw "pip install не прошёл (код $LASTEXITCODE): проверьте сеть и запустите команду ещё раз" }
        Set-Content -Path $stamp -Value (Get-Date -Format o)
    }
}

function Ensure-Frontend {
    # npm install, когда node_modules нет или package.json / package-lock.json новее отметки (после git pull с новыми
    # пакетами сборка иначе падала бы на отсутствующем модуле)
    $modules = Join-Path $Frontend "node_modules"
    $stamp = Join-Path $modules ".deps-stamp"
    $stale = -not (Test-Path $stamp)
    if (-not $stale) {
        $stampTime = (Get-Item $stamp).LastWriteTimeUtc
        foreach ($name in @("package.json", "package-lock.json")) {
            $file = Join-Path $Frontend $name
            if ((Test-Path $file) -and (Get-Item $file).LastWriteTimeUtc -gt $stampTime) { $stale = $true }
        }
    }
    if ($stale) {
        Write-Host "Устанавливаю зависимости frontend ..."
        Push-Location $Frontend
        try { npm install --no-audit --no-fund } finally { Pop-Location }
        if ($LASTEXITCODE -ne 0) { throw "npm install завершился с кодом $LASTEXITCODE" }
        Set-Content -Path $stamp -Value (Get-Date -Format o)
    }
}

function Build-Frontend {
    Push-Location $Frontend
    try { npm run build } finally { Pop-Location }
}

function Get-RemoteConfig {
    # .panel-remote.json (not in git): the server of the panel; `panel_on_server` — the working panel runs there
    $file = Join-Path $Root ".panel-remote.json"
    if (-not (Test-Path $file)) { return $null }
    try { return Get-Content -Raw -Encoding UTF8 $file | ConvertFrom-Json } catch { return $null }
}

function Test-PanelOnServer {
    $cfg = Get-RemoteConfig
    return ($cfg -and $cfg.panel_on_server -and $env:JOBPANEL_LOCAL -ne "1")
}

function Assert-LocalPanel {
    # the panel moved to the server (README): a second one here would revoke the server's hh.ru app token,
    # take the bot's getUpdates away and could spend the single-use refresh token of hh.ru
    if (Test-PanelOnServer) {
        Write-Host "Панель работает на сервере: открыть — .\run.ps1 open (туннель), управлять — .\run.ps1 remote <команда>." -ForegroundColor Yellow
        Write-Host "Локальный запуск запрещён, чтобы не было двух панелей. Песочница для разработки: `$env:JOBPANEL_LOCAL=1 и своя DATA_DIR (README)." -ForegroundColor Yellow
        exit 1
    }
}

function Invoke-Remote {
    param([string[]]$RemoteArgs)
    $py = if (Test-Path $Py) { $Py } else { "python" }
    & $py (Join-Path $Root "deploy\windows\remote.py") @RemoteArgs
    exit $LASTEXITCODE
}

function Invoke-Cli {
    param([string[]]$CliArgs)
    Ensure-Backend
    Push-Location $Backend
    try { & $Py -m app.cli @CliArgs } finally { Pop-Location }
}

switch ($Command) {
    "dev" {
        Assert-LocalPanel
        Ensure-Env; Ensure-Backend; Ensure-Frontend
        Start-Process -FilePath $Py -ArgumentList "-m", "uvicorn", "app.main:app", "--reload", "--host", "127.0.0.1", "--port", "8000" -WorkingDirectory $Backend
        Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "npm run dev" -WorkingDirectory $Frontend
        Start-Sleep -Seconds 4
        Start-Process "http://localhost:5173/settings"
        Write-Host "Backend: http://127.0.0.1:8000  Frontend (dev): http://localhost:5173" -ForegroundColor Green
        Write-Host "Для OAuth в dev-режиме укажите в .env: FRONTEND_URL=http://localhost:5173" -ForegroundColor Yellow
    }
    "start" {
        Assert-LocalPanel
        Ensure-Env; Ensure-Backend; Ensure-Frontend; Build-Frontend
        $port = Get-Port
        Start-Job -ArgumentList $port -ScriptBlock { param($p) Start-Sleep -Seconds 3; Start-Process "http://localhost:$p/settings" } | Out-Null
        Push-Location $Backend
        try { & $Py -m uvicorn app.main:app --host 127.0.0.1 --port $port } finally { Pop-Location }
    }
    "open" {
        # Ярлык на рабочем столе: панель уже работает → открыть в браузере, иначе запустить (без окна) и открыть;
        # панель на сервере → туннель к ней и браузер
        if (Test-PanelOnServer) { Invoke-Remote @("open") }
        $port = Get-Port
        $url = "http://localhost:$port"
        $alive = $false
        try { $alive = (Invoke-WebRequest -UseBasicParsing -Uri "$url/api/health" -TimeoutSec 2).StatusCode -eq 200 } catch { $alive = $false }
        if (-not $alive) {
            Ensure-Env; Ensure-Backend
            $script = Join-Path $Root "run.ps1"
            Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$script`"", "serve" -WorkingDirectory $Root -WindowStyle Hidden
            foreach ($i in 1..40) {
                Start-Sleep -Seconds 1
                try { $alive = (Invoke-WebRequest -UseBasicParsing -Uri "$url/api/health" -TimeoutSec 2).StatusCode -eq 200 } catch { $alive = $false }
                if ($alive) { break }
            }
            if (-not $alive) { Write-Host "Панель не ответила за 40 с — смотрите logs/app.log" -ForegroundColor Yellow }
        }
        Start-Process $url
    }
    "shortcut" {
        # Ярлык «Job Search Panel» на рабочем столе → .\run.ps1 open (без окна консоли)
        $desktop = [Environment]::GetFolderPath("Desktop")
        $lnk = Join-Path $desktop "Job Search Panel.lnk"
        # WScript.Shell теряет кириллицу в пути («Рабочий стол» в OneDrive): ярлык пишется во временную папку и переносится
        $tmp = Join-Path $env:TEMP "Job Search Panel.lnk"
        $script = Join-Path $Root "run.ps1"
        $icon = Join-Path $Frontend "public\favicon.ico"
        $shell = New-Object -ComObject WScript.Shell
        $sc = $shell.CreateShortcut($tmp)
        $sc.TargetPath = "powershell.exe"
        $sc.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" open"
        $sc.WorkingDirectory = $Root
        $sc.Description = "Job Search Panel — запустить панель и открыть её в браузере"
        if (Test-Path $icon) { $sc.IconLocation = "$icon,0" }
        $sc.Save()
        Move-Item -Force $tmp $lnk
        Write-Host "Ярлык создан: $lnk" -ForegroundColor Green
        Write-Host "Двойной клик: если панель уже работает — откроет её в браузере, иначе запустит без окна и откроет."
    }
    "stop" {
        # Остановить панель, запущенную без окна (ярлыком, `open` или автозапуском)
        $proc = Get-PanelProcess
        if ($null -eq $proc) { Write-Host "Панель не запущена (порт $(Get-Port) свободен)."; break }
        Stop-Process -Id $proc.Id -Force
        Start-Sleep -Seconds 1
        if ($null -eq (Get-PanelProcess)) { Write-Host "Панель остановлена." -ForegroundColor Green } else { Write-Host "Не удалось остановить процесс $($proc.Id)." -ForegroundColor Yellow }
    }
    "serve" {
        Assert-LocalPanel
        # без сборки и без браузера — для автозапуска планировщиком Windows
        Ensure-Env; Ensure-Backend
        $port = Get-Port
        Push-Location $Backend
        try { & $Py -m uvicorn app.main:app --host 127.0.0.1 --port $port } finally { Pop-Location }
    }
    "build" { Ensure-Frontend; Build-Frontend }
    "test" {
        Ensure-Backend
        Push-Location $Backend
        try { & $Py -m pytest -q -n auto @Rest } finally { Pop-Location }  # -n 0 — последовательно
        $code = $LASTEXITCODE
        if (-not $Rest) {
            # с аргументами (-k, путь к тесту, -n 0) — только pytest
            Ensure-Frontend
            Push-Location $Frontend
            try { npm test } finally { Pop-Location }
            if ($code -eq 0) { $code = $LASTEXITCODE }
        }
        exit $code
    }
    "lint" {
        Ensure-Backend
        Push-Location $Backend
        try {
            & $Py -m ruff check app tests
            & $Py -m ruff format --check app tests
            & $Py -m mypy
        } finally { Pop-Location }
        Push-Location $Frontend
        try { npx tsc --noEmit } finally { Pop-Location }
    }
    "check" {
        # the checks of a finished stage in one go — the same as .github/workflows/ci.yml (keep the two in step); every
        # step runs, the failed ones are listed at the end, the exit code is 1 when any failed
        Ensure-Backend; Ensure-Frontend
        $failed = @()
        Push-Location $Frontend
        try {
            npm run build; if ($LASTEXITCODE -ne 0) { $failed += "npm run build (tsc + vite)" }
            npm test; if ($LASTEXITCODE -ne 0) { $failed += "npm test" }
        } finally { Pop-Location }
        Push-Location $Backend
        try {
            & $Py -m ruff check app tests; if ($LASTEXITCODE -ne 0) { $failed += "ruff check" }
            & $Py -m ruff format --check app tests; if ($LASTEXITCODE -ne 0) { $failed += "ruff format --check" }
            & $Py -m mypy; if ($LASTEXITCODE -ne 0) { $failed += "mypy" }
            & $Py -m pytest -q -n auto; if ($LASTEXITCODE -ne 0) { $failed += "pytest" }
        } finally { Pop-Location }
        if ($failed.Count -gt 0) {
            Write-Host "Не прошло: $($failed -join ', ')" -ForegroundColor Red
            exit 1
        }
        Write-Host "Все проверки пройдены: сборка и тесты интерфейса, ruff, mypy, pytest" -ForegroundColor Green
    }
    "precommit" {
        Ensure-Backend
        & $Py -m pre_commit install
        & $Py -m pre_commit run --all-files
    }
    "probe"   { Invoke-Cli (@("probe") + $Rest) }
    "status"  { Invoke-Cli @("status") }
    "doctor"  { Invoke-Cli (@("doctor") + $Rest) }
    "migrate" { Assert-LocalPanel; Invoke-Cli @("migrate") }
    "scan"    { Invoke-Cli (@("scan") + $Rest) }
    "sync"    { Invoke-Cli (@("sync") + $Rest) }
    "batch"   { Invoke-Cli (@("batch") + $Rest) }
    "import"  { Invoke-Cli (@("import") + $Rest) }
    "apply"   { Invoke-Cli (@("apply") + $Rest) }
    "stats"   { Invoke-Cli (@("stats") + $Rest) }
    "export"  { Invoke-Cli (@("export") + $Rest) }
    "backup"  { Invoke-Cli @("backup") }
    "reset-token" { Assert-LocalPanel; Invoke-Cli @("reset-token") }
    "remote"  { Invoke-Remote $Rest }
    "profile-md"  { Invoke-Cli (@("profile-md") + $Rest) }
    "demo-seed"   { Assert-LocalPanel; Invoke-Cli @("demo-seed") }
    "autostart" {
        Assert-LocalPanel
        # Задача планировщика Windows: запуск панели при входе пользователя, без окна
        Ensure-Backend; Ensure-Frontend; Build-Frontend
        $script = Join-Path $Root "run.ps1"
        # через conhost --headless: в Windows 11 консоль по умолчанию открывает Windows Terminal, и -WindowStyle Hidden
        # у процесса планировщика окно не прячет; закрыли это пустое окно — панель погасла (0xC000013A, 27.09 в 14:44)
        $conhost = Join-Path $env:WINDIR "System32\conhost.exe"
        $serve = "-NoProfile -ExecutionPolicy Bypass -File `"$script`" serve"
        if ((Test-Path $conhost) -and [Environment]::OSVersion.Version.Build -ge 17763) {
            $action = New-ScheduledTaskAction -Execute $conhost -Argument "--headless powershell.exe $serve" -WorkingDirectory $Root
        } else {
            $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-WindowStyle Hidden $serve" -WorkingDirectory $Root
        }
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        # ноутбук: без -AllowStartIfOnBatteries/-DontStopIfGoingOnBatteries задача не стартует от батареи и гасит панель,
        # когда отключают зарядку; приоритет 5 — обычный (по умолчанию 7, ниже обычного, и для процессора, и для диска)
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 5
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
        Write-Host "Задача «$TaskName» создана: панель будет запускаться при входе в Windows (http://localhost:8000)." -ForegroundColor Green
        Write-Host "Запустить сейчас: Start-ScheduledTask -TaskName $TaskName; удалить: .\run.ps1 autostart-remove"
    }
    "autostart-remove" {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Задача «$TaskName» удалена." -ForegroundColor Green
    }
    default {
        Write-Host "Использование: .\run.ps1 <команда> [аргументы]"
        Write-Host "  start        собрать frontend и запустить панель на http://localhost:8000"
        Write-Host "  serve        запустить панель без сборки и без открытия браузера (для автозапуска)"
        Write-Host "  open         открыть панель в браузере, запустив её без окна при необходимости (это делает ярлык)"
        Write-Host "  stop         остановить панель, запущенную без окна"
        Write-Host "  shortcut     создать ярлык «Job Search Panel» на рабочем столе"
        Write-Host "  dev          backend с автоперезапуском + Vite на http://localhost:5173"
        Write-Host "  build        собрать frontend"
        Write-Host "  test         тесты backend (pytest, параллельно) и frontend (vitest); с аргументами — только pytest"
        Write-Host "  lint         ruff + mypy + tsc"
        Write-Host "  check        все проверки этапа (как CI): сборка и тесты интерфейса, ruff, mypy, pytest"
        Write-Host "  precommit    установить хуки pre-commit и прогнать их по всем файлам"
        Write-Host "  probe        зонд API hh.ru (--allow-publish, --allow-blacklist, --resume-id ...)"
        Write-Host "  status       состояние авторизации и режима"
        Write-Host "  doctor       что установлено, настроено и запущено (без секретов); --json для агента"
        Write-Host "  scan|sync|batch|import|apply|stats|export|backup|reset-token|profile-md  команды CLI"
        Write-Host "  demo-seed    наполнить пустую базу выдуманными демо-данными (README, «Демо-данные»)"
        Write-Host "  autostart    создать задачу планировщика Windows для запуска при входе; autostart-remove — удалить"
        Write-Host "  remote       панель на сервере (README): open, close, status, logs, deploy, rollback, restart,"
        Write-Host "               backup, pull-backup, backup-task, keys, move-to-server, move-back; remote -h — справка"
    }
}
