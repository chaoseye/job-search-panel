"""`doctor`: the readiness report for a fresh machine — statuses, hints, no secrets, JSON for /setup."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from app.config import Settings
from app.services.doctor import FAIL, INFO, OK, WARN, collect, render


async def test_doctor_on_a_fresh_machine(settings: Settings) -> None:
    fresh = settings.model_copy(
        update={
            "hh_client_id": "",
            "hh_client_secret": "",
            "hh_user_agent": "JobSearchPanel/1.0 (your-email@your-domain.com)",
            "telegram_bot_token": "",
            "app_port": 8099,
        }
    )
    report = await collect(fresh)
    by_id = {c.id: c for c in report.checks}
    assert by_id["python"].status == OK
    assert by_id["hh_app"].status == FAIL and by_id["hh_app"].human and "dev.hh.ru" in by_id["hh_app"].hint
    assert by_id["user_agent"].status == FAIL and "bad_user_agent" in by_id["user_agent"].hint
    assert by_id["database"].status == WARN  # tmp data dir: no panel.db yet
    assert by_id["hh_login"].status == WARN and by_id["hh_login"].human
    assert by_id["telegram"].status == WARN and "BotFather" in by_id["telegram"].hint
    assert by_id["panel"].detail == "не запущена"
    assert report.ready is False
    text = render(report)
    assert "✗ Приложение hh.ru" in text and "Готово к работе: нет" in text and "делает человек" in text


async def test_doctor_with_a_configured_panel(settings: Settings) -> None:
    from app.db.session import run_migrations_sync

    configured = settings.model_copy(
        update={
            "hh_client_id": "cid-secret-value",
            "hh_client_secret": "csecret-value",
            "telegram_bot_token": "123456:TOKENVALUE",
            "telegram_chat_id": "42",
            "imap_host": "imap.test",
            "imap_user": "user@test.local",
            "imap_password": "imap-secret",
            "app_port": 8099,
        }
    )
    run_migrations_sync(configured)
    report = await collect(configured)
    by_id = {c.id: c for c in report.checks}
    assert by_id["hh_app"].status == OK and by_id["user_agent"].status == OK
    assert by_id["database"].status == OK and "0029" in by_id["database"].detail
    assert by_id["telegram"].status == OK and by_id["imap"].status == OK
    assert by_id["channels"].detail == "не добавлены (необязательно)"
    assert by_id["hh_login"].status == WARN and by_id["profile"].status == WARN and by_id["searches"].status == WARN
    assert by_id["frontend_build"].status in (OK, FAIL)  # depends on the checkout, never a crash
    dumped = json.dumps(report.to_dict(), ensure_ascii=False)
    for secret in ("cid-secret-value", "csecret-value", "TOKENVALUE", "imap-secret"):
        assert secret not in dumped
    assert [c["id"] for c in report.to_dict()["next_steps"]] and report.to_dict()["ready"] in (True, False)


async def test_doctor_reports_the_systemd_service_of_a_server(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the Linux server (deploy/linux) the autostart line is the systemd unit, not the Windows scheduled task."""
    import subprocess

    from app.services import doctor

    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        out = {"is-enabled": "enabled\n", "is-active": ""}[args[1]]  # a missing answer reads as not-found
        return subprocess.CompletedProcess(args, 0 if out else 3, stdout=out, stderr="")

    with monkeypatch.context() as m:
        m.setattr(doctor.sys, "platform", "linux")
        m.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
        m.setattr(doctor.subprocess, "run", fake_run)
        assert doctor._systemd_state() == ("enabled", "not-found")
        assert calls == [["systemctl", "is-enabled", "jobsearchpanel"], ["systemctl", "is-active", "jobsearchpanel"]]
        m.setattr(doctor.sys, "platform", "win32")
        assert doctor._systemd_state() is None

    monkeypatch.setattr(doctor, "_autostart_state", lambda: None)
    for state, status, hint in (
        (("enabled", "active"), OK, ""),
        (("enabled", "failed"), WARN, "journalctl -u jobsearchpanel"),
        (("not-found", "inactive"), INFO, "README"),
    ):
        monkeypatch.setattr(doctor, "_systemd_state", lambda state=state: state)
        report = await collect(settings.model_copy(update={"app_port": 8099}))
        line = {c.id: c for c in report.checks}["autostart"]
        assert line.status == status and hint in line.hint and "systemd jobsearchpanel" in line.label


def test_doctor_cli(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import cli

    monkeypatch.setattr(cli, "get_settings", lambda: settings.model_copy(update={"hh_client_id": "", "app_port": 8099}))
    res = CliRunner().invoke(cli.app, ["doctor", "--json"])
    assert res.exit_code == 1  # ✗ items → not ready
    data = json.loads(res.output)
    assert data["ready"] is False and any(c["id"] == "hh_app" for c in data["next_steps"])
    res = CliRunner().invoke(cli.app, ["doctor"])
    assert res.exit_code == 1 and "Готово к работе" in res.output
