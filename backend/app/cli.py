"""CLI: python -m app.cli <command>.

Next to a running panel (it answers on APP_PORT) a command never starts a second full Runtime: its own hh
application token (client_credentials) would make hh revoke the panel's one — the panel's public calls then fail
for minutes —, its start would mark the panel's running jobs «interrupted», its writes would race the panel's. So
the commands that call hh.ru or change the data (probe, logout, scan, sync, batch, import, apply, backup,
profile-md --rebuild) go through the panel's API and wait for its job; the reading ones (status, stats, export,
profile-md) and reset-token open the database «attached» (no migrations, no job marks, logs to logs/cli.log);
migrate and serve refuse. Without a running panel everything runs in-process as before.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import typer
from pydantic import ValidationError

from app.config import describe_settings_error, get_settings
from app.net import error_text, ssl_context

if TYPE_CHECKING:  # pragma: no cover
    from app.runtime import Runtime

app = typer.Typer(help="Job Search Panel CLI", no_args_is_help=True)

PANEL_WAIT_POLL_SEC = 1.0
NOT_LOGGED_IN = "Сначала войдите в hh.ru через панель (Настройки → Войти)."


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


@app.callback()
def _check_settings() -> None:
    # a bad value in .env (APPLY_WINDOW=25:00-…) → «KEY: what is wrong» instead of a traceback, for every command
    try:
        get_settings()
    except ValidationError as exc:
        _utf8_console()
        typer.echo(describe_settings_error(exc), err=True)
        raise typer.Exit(code=2) from None


def _run(coro: Any) -> Any:
    _utf8_console()
    return asyncio.run(coro)


# ---- the running panel --------------------------------------------------------------------------------------------


def running_panel() -> str | None:
    """Base URL of the panel answering on APP_PORT, None when it is not running (the same test as doctor)."""
    base = f"http://127.0.0.1:{get_settings().app_port}"
    try:
        resp = httpx.get(f"{base}/api/health", timeout=2.0, verify=ssl_context())
    except httpx.HTTPError:
        return None
    return base if resp.status_code == 200 and "version" in resp.text else None


def _panel(method: str, base: str, path: str, *, timeout: float = 600.0, **kwargs: Any) -> Any:
    """A request to the running panel. Its refusal (4xx/5xx) is printed and ends the command with code 2."""
    try:
        resp = httpx.request(method, f"{base}{path}", timeout=timeout, verify=ssl_context(), **kwargs)
    except httpx.HTTPError as exc:
        typer.echo(f"Панель не ответила: {error_text(exc)}", err=True)
        raise typer.Exit(code=2) from None
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = resp.text[:300]
        text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        typer.echo(f"Панель отказала ({resp.status_code}): {text}", err=True)
        raise typer.Exit(code=2)
    return resp.json() if "json" in resp.headers.get("content-type", "") else resp.text


def _wait_job(base: str, run_id: int) -> dict[str, Any]:
    """Waits for a job run of the panel and returns it. Ctrl+C stops waiting; the job goes on in the panel."""
    typer.echo(f"Задача #{run_id} запущена в панели, жду завершения (Ctrl+C — не ждать, она продолжится)…", err=True)
    try:
        while True:
            runs = _panel("GET", base, "/api/jobs", params={"limit": 50})["runs"]
            run = next((r for r in runs if r["id"] == run_id), None)
            if run is not None and run["status"] != "running":
                return dict(run)
            if run is None and runs and min(int(r["id"]) for r in runs) > run_id:
                return {"id": run_id, "status": "unknown", "stats": None, "error": None}  # pushed out of the list
            time.sleep(PANEL_WAIT_POLL_SEC)
    except KeyboardInterrupt:
        typer.echo("Перестал ждать; задача продолжается в панели (вкладка «Журнал»).", err=True)
        raise typer.Exit(code=130) from None


def _print_run(run: dict[str, Any]) -> None:
    if run["status"] == "unknown":
        typer.echo(f"Задача #{run['id']} завершилась; итог — во вкладке «Журнал» панели.")
        return
    typer.echo(json.dumps(run.get("stats") or {}, ensure_ascii=False, indent=2, default=str))
    if run["status"] != "ok":
        typer.echo(f"Задача завершилась с ошибкой: {run.get('error')}", err=True)
        raise typer.Exit(code=1)


async def _runtime(panel: str | None) -> Runtime:
    """The in-process runtime; «attached» (no migrations, no job marks) when the panel runs next to it."""
    from app.runtime import Runtime

    return await Runtime.create(get_settings(), attached=panel is not None, log_file="cli.log")


# ---- commands -----------------------------------------------------------------------------------------------------


@app.command()
def migrate() -> None:
    """Apply database migrations."""
    from app.db.session import run_migrations_sync

    if running_panel():
        typer.echo(
            "Панель запущена и применила миграции при старте. Для ручного запуска остановите её: .\\run.ps1 stop"
        )
        raise typer.Exit(code=2)
    run_migrations_sync(get_settings())
    typer.echo("migrations applied")


@app.command()
def status() -> None:
    """Show auth and system state."""

    async def _main() -> None:
        from app.services.state import auth_public_state, get_hh_auth, get_setting, get_system_state

        rt = await _runtime(running_panel())
        try:
            async with rt.session_factory() as session:
                auth = auth_public_state(await get_hh_auth(session))
                me = await get_setting(session, "hh_me")
                state = await get_system_state(session)
                typer.echo(
                    json.dumps(
                        {
                            "auth": auth,
                            "me": me,
                            "apply_mode": state.apply_mode,
                            "paused_reason": state.paused_reason,
                            "capabilities": json.loads(state.capabilities_json) if state.capabilities_json else None,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
        finally:
            await rt.close()

    _run(_main())


def _print_probe(data: dict[str, Any], path: Path) -> None:
    typer.echo(f"{'шаг':<22} {'метод':<7} {'статус':<7} {'итог':<12} примечание")
    for step in data.get("steps") or []:
        typer.echo(
            f"{step.get('key', ''):<22} {step.get('method', ''):<7} {str(step.get('status') or '-'):<7} "
            f"{step.get('capability', ''):<12} {step.get('note', '')}"
        )
    typer.echo("")
    typer.echo(f"Режим отклика: {data.get('apply_mode_effective')} (предложен: {data.get('apply_mode_suggested')})")
    typer.echo(f"Отчёт: {path}")


@app.command()
def probe(
    allow_publish: bool = typer.Option(False, help="Разрешить POST /resumes/{id}/publish"),
    allow_blacklist: bool = typer.Option(False, help="Разрешить PUT/DELETE /vacancies/blacklisted/{id}"),
    resume_id: str | None = typer.Option(None, help="resume_id вручную (из URL резюме)"),
) -> None:
    """Run the hh.ru capability probe and print the report."""
    path = get_settings().data_dir / "probe_report.json"
    base = running_panel()
    if base:
        body = {"allow_publish": allow_publish, "allow_blacklist": allow_blacklist, "resume_id": resume_id}
        _utf8_console()
        _print_probe(_panel("POST", base, "/api/probe/run", json=body), path)
        return

    async def _main() -> None:
        from app.db.base import utcnow
        from app.hh.probe import ProbeOptions, run_probe
        from app.services.audit import audit
        from app.services.state import (
            auth_public_state,
            effective_apply_mode,
            get_hh_auth,
            get_system_state,
            set_setting,
        )

        rt = await _runtime(None)
        try:
            async with rt.session_factory() as session:
                if not auth_public_state(await get_hh_auth(session))["logged_in"]:
                    typer.echo(NOT_LOGGED_IN)
                    raise typer.Exit(code=2)
            report = await run_probe(
                rt.hh_client(),
                ProbeOptions(allow_publish=allow_publish, allow_blacklist=allow_blacklist, resume_id=resume_id),
            )
            data = report.to_dict()
            async with rt.session_factory() as session:
                state = await get_system_state(session)
                state.capabilities_json = json.dumps(report.capabilities, ensure_ascii=False)
                state.probe_at = utcnow()
                state.apply_mode = effective_apply_mode(rt.settings, state, report.apply_mode_suggested)
                data["apply_mode_effective"] = state.apply_mode
                data["apply_mode_suggested"] = report.apply_mode_suggested
                await set_setting(session, "probe_report", data)
                if report.resume_ids:
                    await set_setting(session, "resumes_hint", report.resume_ids)
                await audit(session, "probe", f"Зонд (CLI): режим {state.apply_mode}")
                await session.commit()
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            _print_probe(data, path)
        finally:
            await rt.close()

    _run(_main())


@app.command()
def logout() -> None:
    """Invalidate the hh.ru token and clear local tokens."""
    base = running_panel()
    if base:
        _panel("POST", base, "/api/auth/logout")
        typer.echo("logged out")
        return

    async def _main() -> None:
        from app.hh.auth import revoke_token
        from app.services.state import clear_tokens, get_hh_auth

        rt = await _runtime(None)
        try:
            async with rt.session_factory() as session:
                row = await get_hh_auth(session)
                if row is not None and row.access_token:
                    await revoke_token(rt.http, rt.settings, row.access_token)
                await clear_tokens(session)
                await session.commit()
            typer.echo("logged out")
        finally:
            await rt.close()

    _run(_main())


@app.command()
def scan(
    search_id: int | None = typer.Option(None, "--search", help="Только один поиск (id)"),
    only_due: bool = typer.Option(False, help="Только поиски, у которых подошёл интервал"),
) -> None:
    """Scan saved searches and process new vacancies (rules + prescore)."""
    base = running_panel()
    if base:
        _utf8_console()
        if only_due:
            typer.echo("Панель запущена и сама сканирует поиски по расписанию — `scan --only-due` не нужен.")
            return
        path = f"/api/searches/{search_id}/run" if search_id else "/api/jobs/scan"
        _print_run(_wait_job(base, int(_panel("POST", base, path)["job_run_id"])))
        return

    async def _main() -> None:
        from app.services.scanner import run_scan
        from app.services.state import auth_public_state, get_hh_auth

        rt = await _runtime(None)
        try:
            async with rt.session_factory() as session:
                if not auth_public_state(await get_hh_auth(session))["logged_in"]:
                    typer.echo(NOT_LOGGED_IN)
                    raise typer.Exit(code=2)
            stats = await rt.jobs.run_inline(
                "scan", lambda: run_scan(rt, search_ids=[search_id] if search_id else None, only_due=only_due)
            )
            typer.echo(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
        finally:
            await rt.close()

    _run(_main())


@app.command()
def sync(full: bool = typer.Option(False, help="Полный проход status=all (иначе инкремент от last_sync_at)")) -> None:
    """Synchronize negotiations (отклики) with hh.ru: statuses, messages, external applications."""
    base = running_panel()
    if base:
        _utf8_console()
        run_id = int(_panel("POST", base, "/api/jobs/sync", params={"full": full})["job_run_id"])
        _print_run(_wait_job(base, run_id))
        return

    async def _main() -> None:
        from app.services.state import auth_public_state, get_hh_auth
        from app.services.sync import run_sync

        rt = await _runtime(None)
        try:
            async with rt.session_factory() as session:
                if not auth_public_state(await get_hh_auth(session))["logged_in"]:
                    typer.echo(NOT_LOGGED_IN)
                    raise typer.Exit(code=2)
            stats = await rt.jobs.run_inline("sync", lambda: run_sync(rt, full=full))
            typer.echo(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
        finally:
            await rt.close()

    _run(_main())


@app.command()
def stats(
    period: str = typer.Option("7d", help="today | 7d | 30d | all | custom"),
    date_from: str | None = typer.Option(None, "--from", help="YYYY-MM-DD для custom"),
    date_to: str | None = typer.Option(None, "--to", help="YYYY-MM-DD для custom"),
) -> None:
    """Print statistics for the period (same numbers as the dashboard)."""

    async def _main() -> None:
        from app.services.stats import compute_stats

        rt = await _runtime(running_panel())
        try:
            async with rt.session_factory() as session:
                data = await compute_stats(
                    session, rt.settings, period_key=period, date_from=date_from, date_to=date_to
                )
            typer.echo(
                json.dumps(
                    {"period": data["period"], "metrics": data["metrics"], "funnel": data["funnel"]},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            await rt.close()

    _run(_main())


@app.command()
def export(
    kind: str = typer.Option("applications", help="applications | aggregates"),
    period: str = typer.Option("all", help="today | 7d | 30d | all | custom"),
    out: str | None = typer.Option(None, help="Файл для записи (по умолчанию stdout)"),
    date_from: str | None = typer.Option(None, "--from"),
    date_to: str | None = typer.Option(None, "--to"),
) -> None:
    """Export applications or aggregates as CSV (UTF-8 with BOM, `;` separated)."""

    async def _main() -> None:
        from app.services.stats import export_aggregates_csv, export_applications_csv

        rt = await _runtime(running_panel())
        try:
            fn = export_applications_csv if kind == "applications" else export_aggregates_csv
            async with rt.session_factory() as session:
                text = await fn(session, rt.settings, period_key=period, date_from=date_from, date_to=date_to)
            if out:
                Path(out).write_text(text, encoding="utf-8")
                typer.echo(f"written: {out}")
            else:
                typer.echo(text)
        finally:
            await rt.close()

    _run(_main())


@app.command()
def batch(
    limit: int | None = typer.Option(None, "--limit", help="Не больше N вакансий (остальные — в следующий раз)"),
) -> None:
    """Create packets for /hh-batch from current candidates (and queued revisions / reply drafts)."""

    async def _main() -> None:
        from sqlalchemy import or_, select

        from app.db.base import utcnow
        from app.db.models import Vacancy
        from app.exchange.packets import PacketError, build_batches

        base = running_panel()
        rt = await _runtime(base)
        try:
            async with rt.session_factory() as session:
                ids = None
                if limit:
                    rows = (
                        (
                            await session.execute(
                                select(Vacancy.id)
                                .where(
                                    Vacancy.pipeline_status == "prescored",
                                    Vacancy.hidden.is_(False),
                                    or_(Vacancy.snoozed_until.is_(None), Vacancy.snoozed_until <= utcnow()),
                                )
                                .order_by(Vacancy.prescore.desc(), Vacancy.id.desc())
                                .limit(limit)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    ids = list(rows)
                    if not ids:
                        typer.echo("Кандидатов нет.")
                        return
                if base:  # the packet builder of the panel: no second writer of the same packets
                    created = _panel("POST", base, "/api/batches/create", json={"vacancy_ids": ids})["created"]
                else:
                    try:
                        batches = await build_batches(session, rt.settings, vacancy_ids=ids)
                    except PacketError as exc:
                        typer.echo(str(exc))
                        raise typer.Exit(code=2) from exc
                    await session.commit()
                    created = [
                        {"batch_id": b.batch_id, "tasks_count": b.tasks_count, "packet_path": b.packet_path}
                        for b in batches
                    ]
            for b in created:
                typer.echo(f"{b['batch_id']}: задач {b['tasks_count']} → {b['packet_path']}")
            if not created:
                typer.echo("Пакеты не созданы: нет кандидатов и запросов.")
        finally:
            await rt.close()

    _run(_main())


@app.command(name="import")
def import_results(
    file: str | None = typer.Argument(None, help="Конкретный файл результата (иначе все из done/)"),
) -> None:
    """Import Claude results from data/exchange/done (or one file)."""
    base = running_panel()
    if base:  # the panel's importer: its watcher of done/ would otherwise import the same file at the same time
        _utf8_console()
        if file:
            text = Path(file).read_text(encoding="utf-8-sig")
            outcomes = [_panel("POST", base, "/api/batches/import-text", json={"text": text})["outcome"]]
        else:
            outcomes = _panel("POST", base, "/api/batches/import")["outcomes"]
        typer.echo(json.dumps(outcomes, ensure_ascii=False, indent=2))
        return

    async def _main() -> None:
        from app.exchange.importer import import_pending_results, import_result_file

        rt = await _runtime(None)
        try:
            async with rt.session_factory() as session:
                if file:
                    outcomes = [await import_result_file(session, rt.settings, Path(file))]
                else:
                    outcomes = await import_pending_results(session, rt.settings, require_stable=False)
                await session.commit()
            typer.echo(json.dumps([o.to_dict() for o in outcomes], ensure_ascii=False, indent=2))
        finally:
            await rt.close()

    _run(_main())


@app.command()
def apply(
    dry_run: bool = typer.Option(True, "--dry-run/--real", help="Сухой прогон (по умолчанию) или реальная отправка"),
    max_items: int | None = typer.Option(None, "--max", help="Не больше N откликов за запуск"),
) -> None:
    """Send queued applications (mode api). Without --real the run never calls hh.ru."""
    base = running_panel()
    if base:
        _utf8_console()
        params: dict[str, Any] = {"dry_run": dry_run}
        if max_items:
            params["max_items"] = max_items
        _print_run(_wait_job(base, int(_panel("POST", base, "/api/applications/run", params=params)["job_run_id"])))
        return

    async def _main() -> None:
        from app.services.applier import run_apply
        from app.services.state import auth_public_state, get_hh_auth

        rt = await _runtime(None)
        try:
            async with rt.session_factory() as session:
                if not auth_public_state(await get_hh_auth(session))["logged_in"]:
                    typer.echo(NOT_LOGGED_IN)
                    raise typer.Exit(code=2)
            stats = await rt.jobs.run_inline("apply", lambda: run_apply(rt, max_items=max_items, dry_run=dry_run))
            typer.echo(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
        finally:
            await rt.close()

    _run(_main())


@app.command()
def backup() -> None:
    """Copy panel.db into data/backups/ (keeps the last BACKUP_KEEP copies)."""
    base = running_panel()
    if base:
        _utf8_console()
        typer.echo(json.dumps(_panel("POST", base, "/api/backups"), ensure_ascii=False, indent=2))
        return

    async def _main() -> None:
        from app.services.backup import BackupError, backup_job

        rt = await _runtime(None)
        try:
            try:
                result = await backup_job(rt)
            except BackupError as exc:
                typer.echo(str(exc))
                raise typer.Exit(code=2) from exc
            typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            await rt.close()

    _run(_main())


@app.command(name="reset-token")
def reset_token() -> None:
    """Forget hh.ru tokens locally (without calling hh) — the panel will ask to log in again."""

    async def _main() -> None:
        from app.services.audit import audit
        from app.services.state import clear_tokens

        # a running panel reads the tokens from the database on every call: clearing them there is enough
        rt = await _runtime(running_panel())
        try:
            async with rt.session_factory() as session:
                await clear_tokens(session)
                await audit(session, "auth", "Токены hh.ru сброшены командой reset-token")
                await session.commit()
            typer.echo("токены сброшены; войдите заново через панель")
        finally:
            await rt.close()

    _run(_main())


@app.command(name="profile-md")
def profile_md(rebuild: bool = typer.Option(False, help="Пересобрать profile.md перед выводом")) -> None:
    """Print data/exchange/profile.md (optionally rebuilding it first)."""
    base = running_panel()
    if base:
        _utf8_console()
        if rebuild:
            _panel("POST", base, "/api/profile/rebuild")
        typer.echo(_panel("GET", base, "/api/profile/md"))
        return

    async def _main() -> None:
        from app.services.profile import ProfileError, active_profile, rebuild_profile

        rt = await _runtime(None)
        try:
            async with rt.session_factory() as session:
                if rebuild:
                    try:
                        await rebuild_profile(session, rt.settings, force=True)
                    except ProfileError as exc:
                        typer.echo(str(exc))
                        raise typer.Exit(code=2) from exc
                    await session.commit()
                profile = await active_profile(session)
            if profile is None:
                typer.echo("Профиль не собран: добавьте резюме на вкладке «Профиль».")
                raise typer.Exit(code=2)
            typer.echo(profile.profile_md)
        finally:
            await rt.close()

    _run(_main())


@app.command()
def doctor(as_json: bool = typer.Option(False, "--json", help="машинно-читаемый отчёт (для /setup)")) -> None:
    """Что установлено, настроено и запущено — без секретов; подсказки, что делать дальше."""
    from app.services.doctor import collect, render, render_json

    report = _run(collect(get_settings()))
    typer.echo(render_json(report) if as_json else render(report))
    raise typer.Exit(code=0 if report.ready else 1)


@app.command(name="demo-seed")
def demo_seed() -> None:
    """Наполнить пустую базу выдуманными демо-данными (для знакомства с панелью и скриншотов)."""
    from app.runtime import Runtime
    from app.services.demo import DemoRefused, seed_demo

    if running_panel():
        typer.echo("Панель запущена: остановите её, демо-данные пишутся в базу напрямую.", err=True)
        raise typer.Exit(code=2)

    async def _main() -> dict[str, int]:
        rt = await Runtime.create(get_settings(), log_file="cli.log")
        try:
            async with rt.session_factory() as session:
                counts = await seed_demo(session, rt.settings)
                await session.commit()
            return counts
        finally:
            await rt.close()

    try:
        counts = _run(_main())
    except DemoRefused as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    typer.echo(f"Демо-данные в базе {get_settings().db_path}:")
    typer.echo(json.dumps(counts, ensure_ascii=False, indent=2))


@app.command()
def serve(reload: bool = typer.Option(False, help="autoreload (dev)")) -> None:
    """Run the API server."""
    import uvicorn

    base = running_panel()
    if base:
        typer.echo(f"Панель уже запущена: {base}")
        return
    s = get_settings()
    uvicorn.run("app.main:app", host=s.app_host, port=s.app_port, reload=reload)


if __name__ == "__main__":
    app()
