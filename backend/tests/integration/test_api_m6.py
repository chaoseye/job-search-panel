"""M6: backups, pause/resume, `claude -p` runner, bot commands, resume publish, reply drafts, CLI smoke."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select
from typer.testing import CliRunner

from app.config import Settings
from app.db.base import utcnow
from app.db.models import ClaudeBatch, Message, Negotiation, ReplyDraft, Resume, Vacancy
from app.main import app
from app.runtime import Runtime
from app.services import claude_cli
from app.services.backup import BackupError, list_backups, make_backup, prune_backups
from app.services.bot_commands import poll_commands
from app.services.resume_publish import publish_allowed
from app.services.state import get_setting, get_system_state, update_capability
from tests.integration.test_api_m4 import BASE, _login

TOKEN = "123456:TESTTOKEN"
TG = f"https://api.telegram.org/bot{TOKEN}"


@pytest.fixture
async def api(settings: Settings):
    settings = settings.model_copy(
        update={"telegram_bot_token": TOKEN, "telegram_chat_id": "42", "batch_auto_create": False, "backup_keep": 3}
    )
    runtime = await Runtime.create(settings)
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, runtime
    await runtime.close()


# ---- backups -------------------------------------------------------------------------------


async def test_backup_create_list_prune(api) -> None:
    client, runtime = api
    s = runtime.settings
    for i in range(4):
        result = await make_backup(s, now=datetime(2026, 9, 1 + i, 3, 30, tzinfo=UTC))
        assert (s.backups_dir / result["name"]).exists() and result["size_bytes"] > 0
    names = [b["name"] for b in list_backups(s)]
    assert len(names) == 3 and names == sorted(names, reverse=True)  # keep=3, newest first
    assert len(result["removed"]) == 1 and result["removed"][0].startswith("panel_20260901")  # 4th call pruned the 1st
    # the backup is a valid SQLite database with the panel tables
    import sqlite3

    conn = sqlite3.connect(s.backups_dir / names[0])
    try:
        tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    finally:
        conn.close()
    assert {"vacancies", "applications", "negotiations"} <= tables
    assert prune_backups(s, keep=1) and len(list_backups(s)) == 1

    r = await client.post("/api/backups")
    assert r.status_code == 200 and r.json()["name"].startswith("panel_")
    listing = (await client.get("/api/backups")).json()
    assert listing["keep"] == 3 and len(listing["items"]) == 2 and listing["daily_at"] == "03:30"
    with pytest.raises(BackupError):
        await make_backup(s.model_copy(update={"database_url": "sqlite+aiosqlite:///nonexistent/x.db"}))


# ---- pause / resume ------------------------------------------------------------------------


async def test_pause_and_resume(api) -> None:
    client, runtime = api
    assert (await client.post("/api/jobs/pause", json={"reason": "user"})).json() == {"paused_reason": "user"}
    assert (await client.get("/api/settings")).json()["paused_reason"] == "user"
    assert (await client.post("/api/jobs/resume")).json() == {"paused_reason": None}
    assert (await client.get("/api/settings")).json()["paused_reason"] is None
    health = (await client.get("/api/health")).json()
    assert health["telegram_queue"] == 0 and health["pending_batches"] == 0 and health["running_jobs"] == []


# ---- claude -p ---------------------------------------------------------------------------------


class FakeProc:
    def __init__(self, code: int, output: bytes, delay: float = 0.0) -> None:
        self.returncode = code
        self._output = output
        self._delay = delay
        self.killed = False

    async def communicate(self) -> tuple[bytes, None]:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._output, None

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


async def test_claude_runner(api, monkeypatch: pytest.MonkeyPatch) -> None:
    client, runtime = api
    # disabled → 409, no subprocess
    r = await client.post("/api/claude/run", json={})
    assert r.status_code == 409 and "CLAUDE_CLI_AUTORUN" in r.json()["detail"]
    runtime.settings = runtime.settings.model_copy(
        update={"claude_cli_autorun": True, "claude_cli_timeout_sec": 1, "claude_cli_model": "claude-opus-5"}
    )
    monkeypatch.setattr(claude_cli, "claude_executable", lambda: r"C:\tools\claude.cmd")
    status = (await client.get("/api/claude/status")).json()
    assert status["enabled"] and status["available"] and status["pending"] == [] and status["model"] == "claude-opus-5"

    calls: list[list[str]] = []

    async def fake_exec(*args: str, **kwargs: Any) -> FakeProc:
        calls.append(list(args))
        assert kwargs["cwd"] == str(claude_cli.ROOT_DIR)
        return FakeProc(0, "разобрано 3 задачи\n".encode())

    monkeypatch.setattr(claude_cli.asyncio, "create_subprocess_exec", fake_exec)
    # nothing pending → skipped without running
    result = await claude_cli.run_claude_batch(runtime)
    assert result == {"skipped": "нет пакетов в pending/"} and calls == []
    pending = runtime.settings.exchange_dir / "pending"
    (pending / "batch_x.json").write_text("{}", encoding="utf-8")
    result = await claude_cli.run_claude_batch(runtime, packet="batch_x.json")
    assert result["returncode"] == 0 and "разобрано" in result["output_tail"]
    assert calls[-1][:3] == [r"C:\tools\claude.cmd", "-p", "/hh-batch batch_x.json"]
    # print mode cannot answer prompts and the packet text is untrusted: nothing is auto-accepted, writes only into
    # done/ and tmp/, the only command is the humanizer scanner, .env and the database are not readable
    args = calls[-1]
    assert args[args.index("--permission-mode") + 1] == "default" and "acceptEdits" not in args
    allowed = args[args.index("--allowedTools") + 1 : args.index("--disallowedTools")]
    assert "Edit(./data/exchange/done/**)" in allowed and not any(a in ("Write", "Edit") for a in allowed)
    assert [a for a in allowed if a.startswith("Bash(")] == [
        "Bash(backend/.venv/Scripts/python.exe .claude/skills/humanizer-ru/scripts/scan.py:*)",
        "Bash(backend/.venv/bin/python .claude/skills/humanizer-ru/scripts/scan.py:*)",
    ]
    assert "Read(./.env)" in args and "Read(./data/panel.db*)" in args
    assert calls[-1][-2:] == ["--model", "claude-opus-5"]  # the batch model is the panel's, not the CLI default
    assert Path(result["log"]).read_text(encoding="utf-8").count("=====") >= 2
    with pytest.raises(claude_cli.ClaudeCliError):
        await claude_cli.run_claude_batch(runtime, packet="../evil.json")

    async def failing_exec(*args: str, **kwargs: Any) -> FakeProc:
        return FakeProc(2, b"boom")

    monkeypatch.setattr(claude_cli.asyncio, "create_subprocess_exec", failing_exec)
    with pytest.raises(claude_cli.ClaudeCliError, match="кодом 2"):
        await claude_cli.run_claude_batch(runtime)

    slow = FakeProc(0, b"", delay=5)

    async def slow_exec(*args: str, **kwargs: Any) -> FakeProc:
        return slow

    monkeypatch.setattr(claude_cli.asyncio, "create_subprocess_exec", slow_exec)
    with pytest.raises(claude_cli.ClaudeCliError, match="не завершился"):
        await claude_cli.run_claude_batch(runtime)
    assert slow.killed
    # through the job runner
    monkeypatch.setattr(claude_cli.asyncio, "create_subprocess_exec", fake_exec)
    r = await client.post("/api/claude/run", json={})
    assert r.status_code == 200
    await runtime.jobs.wait("claude_run")
    runs = (await client.get("/api/jobs")).json()["runs"]
    assert runs[0]["job"] == "claude_run" and runs[0]["status"] == "ok"

    # the background tick: runs only with packets waiting, backs off after a failure, stops after 3 in a row
    from app.scheduler import claude_tick

    (pending / "batch_x.json").unlink(missing_ok=True)
    assert await claude_cli.should_autorun(runtime) == (False, "нет пакетов")
    (pending / "batch_y.json").write_text("{}", encoding="utf-8")
    assert await claude_cli.should_autorun(runtime) == (True, "есть пакеты")
    runtime.settings = runtime.settings.model_copy(update={"claude_cli_tick_min": 0})
    assert (await claude_cli.should_autorun(runtime))[0] is False
    runtime.settings = runtime.settings.model_copy(update={"claude_cli_tick_min": 5})
    before = len(calls)
    await claude_tick(runtime)
    await runtime.jobs.wait("claude_run")
    assert len(calls) == before + 1 and calls[-1][2] == "/hh-batch"
    # a failed run → retry only after CLAUDE_CLI_RETRY_MIN; three failures → stop
    monkeypatch.setattr(claude_cli.asyncio, "create_subprocess_exec", failing_exec)
    for _ in range(3):
        runtime.jobs.failures["claude_run"] = runtime.jobs.failures.get("claude_run", 0)
        r = await client.post("/api/claude/run", json={})
        assert r.status_code == 200
        await runtime.jobs.wait("claude_run")
    ok, reason = await claude_cli.should_autorun(runtime)
    assert ok is False and "неудач" in reason
    runtime.jobs.failures["claude_run"] = 1
    ok, reason = await claude_cli.should_autorun(runtime)
    assert ok is False and "ждём" in reason
    # once the retry interval has passed (and failures were reset) the tick runs again
    from app.db.models import JobRun

    async with runtime.session_factory() as session:
        for run in (await session.execute(select(JobRun).where(JobRun.job == "claude_run"))).scalars():
            run.finished_at = utcnow() - timedelta(minutes=45)
        await session.commit()
    runtime.jobs.failures["claude_run"] = 0
    assert (await claude_cli.should_autorun(runtime))[0] is True


# ---- bot commands ------------------------------------------------------------------------------


def _update(uid: int, chat: int, text: str) -> dict[str, Any]:
    return {"update_id": uid, "message": {"message_id": uid, "chat": {"id": chat, "type": "private"}, "text": text}}


@respx.mock(assert_all_called=False)
async def test_bot_commands(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    sent: list[dict[str, Any]] = []
    polled: list[dict[str, Any]] = []

    def updates_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        polled.append(body)
        if body.get("offset"):
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    _update(10, 99, "/pause"),  # stranger: ignored
                    _update(11, 42, "/status"),
                    _update(12, 42, "hello"),  # not a command
                    _update(13, 42, "/pause"),
                    _update(14, 42, "/batch"),
                    _update(15, 42, "/digest"),
                    _update(16, 42, "/resume@ExamplePanelBot"),
                    _update(17, 42, "/whatever"),
                ],
            },
        )

    respx_mock.post(f"{TG}/getUpdates").mock(side_effect=updates_handler)

    def send_handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

    respx_mock.post(f"{TG}/sendMessage").mock(side_effect=send_handler)
    summary = await poll_commands(runtime)
    assert summary["updates"] == 8 and summary["commands"] == 6 and summary["ignored"] == 2
    texts = [s["text"] for s in sent]
    assert all(s["chat_id"] == "42" for s in sent) and len(texts) == 6
    assert texts[0].startswith("📋 <b>Job Search Panel</b>") and "нет входа" in texts[0]
    assert texts[1].startswith("⏸") and "профиль" in texts[2].lower()  # /batch without profile → PacketError text
    assert texts[3].startswith("📊 <b>Сводка за") and texts[4].startswith("▶️") and "Неизвестная команда" in texts[5]
    async with runtime.session_factory() as session:
        assert await get_setting(session, "telegram_update_offset") == 18
        assert (await get_system_state(session)).paused_reason is None  # /pause then /resume
    # second poll uses the stored offset and finds nothing
    summary = await poll_commands(runtime)
    assert summary["updates"] == 0 and polled[-1]["offset"] == 18
    # disabled → no polling at all
    runtime.settings = runtime.settings.model_copy(update={"telegram_commands": False})
    before = len(polled)
    await poll_commands(runtime)
    assert len(polled) == before


# ---- resume publish ------------------------------------------------------------------------------


def test_publish_allowed_rules() -> None:
    now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    r = Resume(source="hh", hh_resume_id="a" * 38, can_publish_or_update=True, next_publish_at=None)
    assert publish_allowed(r, now) == (True, "")
    r.next_publish_at = now + timedelta(hours=2)
    ok, why = publish_allowed(r, now)
    assert not ok and "после" in why
    r.can_publish_or_update = False
    assert not publish_allowed(r, now)[0]
    r.next_publish_at = now - timedelta(minutes=1)
    assert publish_allowed(r, now)[0]


@respx.mock(assert_all_called=False)
async def test_resume_publish_flow(api, respx_mock: respx.MockRouter) -> None:
    client, runtime = api
    await _login(client, respx_mock)
    rid = "0123456789abcdef0123456789abcdef012345"
    from tests.unit.test_resume_norm import load_fixture

    raw = load_fixture()
    raw["id"] = rid
    raw["can_publish_or_update"] = True
    raw["next_publish_at"] = None
    respx_mock.get(f"{BASE}/resumes/{rid}").mock(return_value=httpx.Response(200, json=raw))
    r = await client.post("/api/profile/hh/import", json={"resume_id": rid})
    assert r.status_code == 200, r.text
    async with runtime.session_factory() as session:
        resume = (await session.execute(select(Resume).where(Resume.hh_resume_id == rid))).scalar_one()
        pk = resume.id
    # capability unknown → 409 unless force
    r = await client.post(f"/api/profile/resumes/{pk}/publish")
    assert r.status_code == 409 and "Зонд" in r.json()["detail"]
    publish = respx_mock.post(f"{BASE}/resumes/{rid}/publish").mock(return_value=httpx.Response(204))
    r = await client.post(f"/api/profile/resumes/{pk}/publish", params={"force": "true"})
    assert r.status_code == 200 and r.json()["ok"] and publish.call_count == 1
    assert r.json()["next_publish_at"] is not None
    async with runtime.session_factory() as session:
        from app.services.state import read_capabilities

        assert read_capabilities(await get_system_state(session))["resume_publish"] == "available"
    # too early now
    r = await client.post(f"/api/profile/resumes/{pk}/publish")
    assert r.status_code == 409 and "рано" in r.json()["detail"]
    assert (await client.get(f"/api/profile/resumes/{pk}/publish")).json()["allowed"] is False
    # hh refuses → capability unavailable
    async with runtime.session_factory() as session:
        resume = await session.get(Resume, pk)
        resume.next_publish_at = None
        await session.commit()
    publish.mock(return_value=httpx.Response(403, json={"errors": [{"type": "forbidden", "value": "no_scope"}]}))
    r = await client.post(f"/api/profile/resumes/{pk}/publish")
    assert r.status_code == 502
    async with runtime.session_factory() as session:
        assert read_capabilities(await get_system_state(session))["resume_publish"] == "unavailable"
    r = await client.post(f"/api/profile/resumes/{pk}/publish", params={"force": "true"})
    assert r.status_code == 409 and "недоступно" in r.json()["detail"]


# ---- reply drafts through the packet exchange -----------------------------------------------------


async def test_reply_draft_roundtrip(api) -> None:
    client, runtime = api
    s = runtime.settings
    # profile is required for packets
    r = await client.post(
        "/api/profile/resumes/upload", files={"file": ("cv.txt", "Иван, Python".encode(), "text/plain")}
    )
    assert r.status_code == 200
    async with runtime.session_factory() as session:
        v = Vacancy(hh_id="v1", name="Python dev", employer_name="Emp", pipeline_status="sent", first_seen_at=utcnow())
        session.add(v)
        await session.flush()
        n = Negotiation(hh_id="n1", vacancy_hh_id="v1", vacancy_id=v.id, state_id="invitation", hh_created_at=utcnow())
        session.add(n)
        await session.flush()
        session.add(
            Message(
                negotiation_id=n.id,
                hh_message_id="m1",
                author_type="employer",
                author_name="HR",
                text="Когда удобно созвониться?",
                hh_created_at=utcnow(),
            )
        )
        await session.commit()
    r = await client.post("/api/negotiations/n1/reply-draft", json={"user_intent": "предложить завтра 11:00"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["draft"]["status"] == "sent_in_batch" and body["batch_id"]
    packet_path = s.exchange_dir / "pending" / f"batch_{body['batch_id']}.json"
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    tasks = [t for t in packet["tasks"] if t["type"] == "reply_draft"]
    assert (
        len(tasks) == 1 and tasks[0]["vacancy"]["id"] == "v1" and tasks[0]["user_intent"] == "предложить завтра 11:00"
    )
    assert tasks[0]["thread"] == [
        {"author": "employer", "name": "HR", "text": "Когда удобно созвониться?", "at": tasks[0]["thread"][0]["at"]}
    ]
    # a second request replaces the queued one? (the first is already in a batch → stays; new one queued)
    r = await client.post("/api/negotiations/n1/reply-draft", json={"user_intent": None})
    assert r.json()["draft"]["status"] in ("queued", "sent_in_batch")

    result = {
        "protocol_version": 1,
        "batch_id": body["batch_id"],
        "profile_version": packet["profile_version"],
        "results": [
            {
                "vacancy_id": "v1",
                "reply": {
                    "text": "Добрый день! Удобно завтра в 11:00.",
                    "intent": "answer_question",
                    "needs_clarification": [],
                },
            }
        ],
    }
    done = s.exchange_dir / "done" / packet_path.name
    done.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    r = await client.post("/api/batches/import")
    assert r.status_code == 200, r.text
    outcome = r.json()["outcomes"][0]
    assert outcome["status"] == "imported" and outcome["replies"] == 1
    detail = (await client.get("/api/negotiations/n1")).json()
    drafts = detail["reply_drafts"]
    done_drafts = [d for d in drafts if d["status"] == "done"]
    assert (
        done_drafts
        and done_drafts[0]["text"].startswith("Добрый день")
        and done_drafts[0]["intent"] == "answer_question"
    )
    async with runtime.session_factory() as session:
        batch = (
            await session.execute(select(ClaudeBatch).where(ClaudeBatch.batch_id == body["batch_id"]))
        ).scalar_one()
        assert batch.status == "imported"
        statuses = sorted(d.status for d in (await session.execute(select(ReplyDraft))).scalars())
        assert "done" in statuses


# ---- CLI smoke ---------------------------------------------------------------------------------------


def test_cli_smoke(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import cli
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr("app.runtime.get_settings", lambda: settings)
    runner = CliRunner()
    res = runner.invoke(cli.app, ["backup"])
    assert res.exit_code == 0, res.output
    assert "panel_" in res.output
    res = runner.invoke(cli.app, ["profile-md"])
    assert res.exit_code == 2 and "Профиль не собран" in res.output
    res = runner.invoke(cli.app, ["stats", "--period", "7d"])
    assert res.exit_code == 0 and '"sent": 0' in res.output
    res = runner.invoke(cli.app, ["reset-token"])
    assert res.exit_code == 0 and "сброшены" in res.output
    res = runner.invoke(cli.app, ["batch"])
    assert res.exit_code == 2  # no profile


async def test_capability_gate_for_auto_publish(api) -> None:
    client, runtime = api
    from app.services.resume_publish import auto_publish_tick

    assert (await auto_publish_tick(runtime)) == {"skipped": "disabled"}
    runtime.settings = runtime.settings.model_copy(update={"resume_auto_publish": True})
    assert (await auto_publish_tick(runtime))["skipped"] == "not logged in"
    async with runtime.session_factory() as session:
        await update_capability(session, "resume_publish", "available")
        await session.commit()
