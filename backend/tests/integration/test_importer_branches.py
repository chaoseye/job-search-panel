"""The importer's less travelled branches.

The packet, the batch and the requests are put into the database directly, so every task type can be driven into the
branch under test: a result with an error, a result without its part, no result at all, a broken file.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    CandidateProfile,
    ClaudeBatch,
    InterviewPrep,
    LetterRevisionRequest,
    QuestionAnswers,
    ReplyDraft,
    ResumeAdvice,
    Vacancy,
)
from app.exchange.importer import import_pending_results, import_result_file, import_result_text, is_stable
from app.exchange.packets import done_dir, imported_dir, pending_dir, rejected_dir
from app.exchange.schemas import Packet, PacketTask
from app.runtime import Runtime

BID = "20260923_1200_ab12"
OTHER_BID = "20260923_1300_cd34"
ERROR = "не хватает данных"


@pytest.fixture
async def rt(settings: Settings):
    runtime = await Runtime.create(settings)
    yield runtime
    await runtime.close()


def _task(kind: str, vid: str) -> PacketTask:
    return PacketTask.model_validate(
        {"type": kind, "vacancy": {"id": vid, "name": f"Вакансия {vid}", "employer": {"name": "Рога и копыта"}}}
    )


async def _batch(
    session: AsyncSession, settings: Settings, tasks: list[PacketTask], batch_id: str = BID
) -> ClaudeBatch:
    folder = pending_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"batch_{batch_id}.json"
    packet = Packet(
        batch_id=batch_id,
        created_at="2026-09-23T12:00:00",
        profile_version=1,
        letter_defaults={"min_chars": 600, "max_chars": 1200},
        tasks=tasks,
    )
    path.write_text(packet.model_dump_json(), encoding="utf-8")
    row = ClaudeBatch(
        batch_id=batch_id,
        packet_path=str(path),
        status="pending",
        tasks_count=len(tasks),
        profile_version=1,
        protocol_version=1,
    )
    session.add(row)
    await session.flush()
    return row


async def _vacancy(
    session: AsyncSession, hh_id: str, *, status: str = "awaiting_claude", batch_id: str | None = BID
) -> Vacancy:
    row = Vacancy(hh_id=hh_id, name=f"Вакансия {hh_id}", employer_name="Рога и копыта", pipeline_status=status)
    row.batch_id = batch_id
    session.add(row)
    await session.flush()
    return row


def _result(items: list[dict[str, Any]], batch_id: str = BID, **extra: Any) -> dict[str, Any]:
    return {"protocol_version": 1, "batch_id": batch_id, "results": items, **extra}


def _done(settings: Settings, payload: object, name: str = f"batch_{BID}.json") -> Path:
    folder = done_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


async def test_errors_close_every_kind_of_request(rt: Runtime) -> None:
    s = rt.settings
    async with rt.session_factory() as session:
        v = {h: await _vacancy(session, h, status="suitable", batch_id=None) for h in ("1", "2", "3", "4")}
        rev = LetterRevisionRequest(vacancy_id=v["1"].id, user_comment="короче", status="sent_in_batch", batch_id=BID)
        reply = ReplyDraft(negotiation_id=1, vacancy_id=v["2"].id, status="sent_in_batch", batch_id=BID)
        prep = InterviewPrep(application_id=1, vacancy_id=v["3"].id, status="sent_in_batch", batch_id=BID)
        qa = QuestionAnswers(vacancy_id=v["4"].id, questions_text="Почему мы?", status="sent_in_batch", batch_id=BID)
        session.add_all([rev, reply, prep, qa])
        kinds = ("letter_revision", "reply_draft", "interview_prep", "answer_questions")
        await _batch(session, s, [_task(k, h) for k, h in zip(kinds, v, strict=True)])
        await session.commit()

        out = await import_result_file(session, s, _done(s, _result([{"vacancy_id": h, "error": ERROR} for h in v])))
        await session.commit()

    assert out.status == "partial"
    assert out.errors == [f"{h}: {ERROR}" for h in v]
    assert out.missing == []
    async with rt.session_factory() as session:
        rev_ = await session.get(LetterRevisionRequest, rev.id)
        reply_ = await session.get(ReplyDraft, reply.id)
        prep_ = await session.get(InterviewPrep, prep.id)
        qa_ = await session.get(QuestionAnswers, qa.id)
        # the revision goes back to the queue, the drafts are closed with Claude's reason
        assert rev_ is not None and (rev_.status, rev_.batch_id) == ("queued", None)
        assert reply_ is not None and (reply_.status, reply_.error) == ("cancelled", ERROR)
        assert prep_ is not None and (prep_.status, prep_.error) == ("cancelled", ERROR)
        assert qa_ is not None and (qa_.status, qa_.error) == ("cancelled", ERROR)
        batch = await session.get(ClaudeBatch, 1)
        assert batch is not None and batch.status == "partial" and ERROR in (batch.error or "")


async def test_results_without_their_part(rt: Runtime) -> None:
    s = rt.settings
    async with rt.session_factory() as session:
        ev = await _vacancy(session, "10")
        v = {h: await _vacancy(session, h, status="suitable", batch_id=None) for h in ("11", "12", "13", "14")}
        rev = LetterRevisionRequest(vacancy_id=v["11"].id, user_comment="теплее", status="sent_in_batch", batch_id=BID)
        reply = ReplyDraft(negotiation_id=1, vacancy_id=v["12"].id, status="sent_in_batch", batch_id=BID)
        prep = InterviewPrep(application_id=1, vacancy_id=v["13"].id, status="sent_in_batch", batch_id=BID)
        qa = QuestionAnswers(
            vacancy_id=v["14"].id, questions_text="Опыт с Kafka?", status="sent_in_batch", batch_id=BID
        )
        session.add_all([rev, reply, prep, qa])
        tasks = [
            _task("evaluate_and_letter", "10"),
            _task("letter_revision", "11"),
            _task("reply_draft", "12"),
            _task("interview_prep", "13"),
            _task("answer_questions", "14"),
        ]
        await _batch(session, s, tasks)
        await session.commit()

        items = [{"vacancy_id": h} for h in ("10", "11", "12", "13", "14")]
        out = await import_result_file(session, s, _done(s, _result(items)))
        await session.commit()

    assert out.status == "partial"
    assert out.errors == [
        "10: нет evaluation",
        "11: переработка без письма",
        "12: нет reply",
        "13: нет prep",
        "14: нет answers",
    ]
    assert (out.evaluations, out.letters, out.replies, out.preps, out.answers) == (0, 0, 0, 0, 0)
    async with rt.session_factory() as session:
        ev_ = await session.get(Vacancy, ev.id)
        assert ev_ is not None
        # no evaluation: back to the queue for the next packet, one attempt counted
        assert (ev_.pipeline_status, ev_.batch_id, ev_.claude_attempts) == ("prescored", None, 1)
        assert ev_.pipeline_reason == "результат без оценки"
        rev_ = await session.get(LetterRevisionRequest, rev.id)
        reply_ = await session.get(ReplyDraft, reply.id)
        prep_ = await session.get(InterviewPrep, prep.id)
        qa_ = await session.get(QuestionAnswers, qa.id)
        assert rev_ is not None and (rev_.status, rev_.batch_id) == ("queued", None)
        assert reply_ is not None and (reply_.status, reply_.error) == ("cancelled", "результат без reply")
        assert prep_ is not None and (prep_.status, prep_.error) == ("cancelled", "результат без prep")
        assert qa_ is not None and (qa_.status, qa_.error) == ("cancelled", "результат без answers")


async def test_resume_advice_results(rt: Runtime) -> None:
    s = rt.settings
    async with rt.session_factory() as session:
        failed, good, waiting = (ResumeAdvice(status="sent_in_batch", batch_id=BID) for _ in range(3))
        foreign = ResumeAdvice(status="sent_in_batch", batch_id=OTHER_BID)
        session.add_all([failed, good, waiting, foreign])
        await session.flush()
        ids = {
            "bad": "advice-abc",
            "unknown": "advice-999999",
            "foreign": f"advice-{foreign.id}",
            "failed": f"advice-{failed.id}",
            "good": f"advice-{good.id}",
            "waiting": f"advice-{waiting.id}",
        }
        await _batch(session, s, [_task("resume_advice", vid) for vid in ids.values()])
        await session.commit()

        edits = {
            "summary": "Сдвинуть акцент на FastAPI",
            "edits": [{"section": "О себе", "proposed": "Backend на Python"}],
        }
        items = [
            {"vacancy_id": ids["bad"], "advice": edits},
            {"vacancy_id": ids["unknown"], "advice": edits},
            {"vacancy_id": ids["foreign"], "advice": edits},
            {"vacancy_id": ids["failed"], "error": ERROR},
            {"vacancy_id": ids["good"], "advice": edits},
            # «waiting» has no result at all
        ]
        out = await import_result_file(session, s, _done(s, _result(items)))
        await session.commit()

    assert out.advices == 1
    assert out.missing == [ids["waiting"]]
    assert out.errors == [
        f"{ids['bad']}: неверный id правок резюме",
        f"{ids['unknown']}: запрос правок резюме не найден в пакете",
        f"{ids['foreign']}: запрос правок резюме не найден в пакете",
        f"{ids['failed']}: {ERROR}",
    ]
    async with rt.session_factory() as session:
        rows = {k: await session.get(ResumeAdvice, r.id) for k, r in (("failed", failed), ("good", good))}
        rows |= {k: await session.get(ResumeAdvice, r.id) for k, r in (("waiting", waiting), ("foreign", foreign))}
        assert rows["good"] is not None and rows["good"].status == "done"
        assert json.loads(rows["good"].result_json or "{}")["summary"] == "Сдвинуть акцент на FastAPI"
        assert rows["failed"] is not None and (rows["failed"].status, rows["failed"].error) == ("cancelled", ERROR)
        # only the request without a result goes back to the queue: the done and the failed ones stay as they are
        assert rows["waiting"] is not None and (rows["waiting"].status, rows["waiting"].batch_id) == ("queued", None)
        assert rows["foreign"] is not None and (rows["foreign"].status, rows["foreign"].batch_id) == (
            "sent_in_batch",
            OTHER_BID,
        )


async def test_tasks_without_results_go_back_to_the_queue(rt: Runtime) -> None:
    s = rt.settings
    async with rt.session_factory() as session:
        ev = await _vacancy(session, "20")
        v = {h: await _vacancy(session, h, status="suitable", batch_id=None) for h in ("21", "22", "23", "24")}
        hidden = await _vacancy(session, "26", status="hidden")  # hidden by the user while the packet was out
        rev = LetterRevisionRequest(vacancy_id=v["21"].id, user_comment="короче", status="sent_in_batch", batch_id=BID)
        reply = ReplyDraft(negotiation_id=1, vacancy_id=v["22"].id, status="sent_in_batch", batch_id=BID)
        prep = InterviewPrep(application_id=1, vacancy_id=v["23"].id, status="sent_in_batch", batch_id=BID)
        qa = QuestionAnswers(vacancy_id=v["24"].id, questions_text="Почему мы?", status="sent_in_batch", batch_id=BID)
        session.add_all([rev, reply, prep, qa])
        tasks = [
            _task("evaluate_and_letter", "20"),
            _task("letter_revision", "21"),
            _task("reply_draft", "22"),
            _task("interview_prep", "23"),
            _task("answer_questions", "24"),
            _task("evaluate_and_letter", "25"),  # the vacancy is gone from the database
            _task("evaluate_and_letter", "28"),  # the same, but with a result
        ]
        await _batch(session, s, tasks)
        await session.commit()

        items = [
            {"vacancy_id": "27", "evaluation": {"score": 90, "verdict": "apply"}},  # not a task of this packet
            {"vacancy_id": "28", "evaluation": {"score": 90, "verdict": "apply"}},
        ]
        out = await import_result_file(session, s, _done(s, _result(items)))
        await session.commit()

    assert out.status == "partial"
    assert out.missing == ["20", "21", "22", "23", "24", "25"]
    assert out.errors == ["28: вакансия не найдена в базе"]
    assert "27: результата нет среди задач пакета — пропущен" in out.warnings
    async with rt.session_factory() as session:
        ev_ = await session.get(Vacancy, ev.id)
        assert ev_ is not None and (ev_.pipeline_status, ev_.batch_id, ev_.claude_attempts) == ("prescored", None, 1)
        assert ev_.pipeline_reason == f"не разобрана в пакете {BID}, будет включена снова"
        hidden_ = await session.get(Vacancy, hidden.id)
        # the packet is closed: the hidden vacancy keeps its status but no longer points to the packet
        assert hidden_ is not None and (hidden_.pipeline_status, hidden_.batch_id) == ("hidden", None)
        for model, row in (
            (LetterRevisionRequest, rev),
            (ReplyDraft, reply),
            (InterviewPrep, prep),
            (QuestionAnswers, qa),
        ):
            got = await session.get(model, row.id)  # type: ignore[arg-type]
            assert got is not None and (got.status, got.batch_id) == ("queued", None), model.__name__


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ("not_json", "невалидный JSON"),
        ("schema", "результат не соответствует схеме"),
        ("protocol", "protocol_version 999 не поддерживается"),
        ("name_mismatch", "не совпадает с batch_id"),
        ("unknown_batch", f"пакет {OTHER_BID} неизвестен панели"),
        ("packet_lost", "не удалось прочитать пакет"),
    ],
)
async def test_broken_files_are_rejected(rt: Runtime, kind: str, reason: str) -> None:
    s = rt.settings
    async with rt.session_factory() as session:
        vacancy = await _vacancy(session, "31")
        batch = await _batch(session, s, [_task("evaluate_and_letter", "31")])
        if kind == "packet_lost":
            Path(batch.packet_path).unlink()
        await session.commit()

        name = f"batch_{BID}.json"
        payload: object = _result([{"vacancy_id": "31", "evaluation": {"score": 80, "verdict": "apply"}}])
        if kind == "not_json":
            payload = "{not json"
        elif kind == "schema":
            payload = _result([{"vacancy_id": "31", "evaluation": {"score": 500, "verdict": "apply"}}])
        elif kind == "protocol":
            payload = _result([], protocol_version=999)
        elif kind == "name_mismatch":
            payload = _result([], batch_id=OTHER_BID)
        elif kind == "unknown_batch":
            name, payload = f"batch_{OTHER_BID}.json", _result([], batch_id=OTHER_BID)
        out = await import_result_file(session, s, _done(s, payload, name))
        await session.commit()

    assert out.status == "rejected"
    assert len(out.errors) == 1 and reason in out.errors[0], out.errors
    rejected = rejected_dir(s)
    assert (rejected / name).exists()
    assert reason in (rejected / f"{name}.error.txt").read_text(encoding="utf-8")
    assert not (done_dir(s) / name).exists()
    async with rt.session_factory() as session:
        v = await session.get(Vacancy, vacancy.id)
        assert v is not None and v.pipeline_status == "awaiting_claude"  # nothing was applied
        b = await session.get(ClaudeBatch, batch.id)
        assert b is not None and b.status == "pending"


async def test_moved_packet_stale_profile_duplicates_and_letter_warnings(rt: Runtime) -> None:
    s = rt.settings
    async with rt.session_factory() as session:
        session.add(CandidateProfile(version=2, is_active=True, preferences_json="{}", profile_md="", hash="h"))
        vacancy = await _vacancy(session, "30")
        batch = await _batch(session, s, [_task("evaluate_and_letter", "30")])
        # the packet file was moved by hand into imported/ (the batch still points to pending/)
        moved = imported_dir(s) / Path(batch.packet_path).name
        moved.parent.mkdir(parents=True, exist_ok=True)
        Path(batch.packet_path).replace(moved)
        # a result of an earlier import with the same name is already there
        (imported_dir(s) / f"batch_{BID}.result.json").write_text("{}", encoding="utf-8")
        await session.commit()

        good = {
            "vacancy_id": "30",
            "evaluation": {"score": 90, "verdict": "apply", "summary": "Подходит по стеку"},
            "letter": {"text": "Здравствуйте! Я командный игрок и хочу у вас работать.", "language": "ru"},
        }
        payload = _result([good, dict(good)], profile_version=1)
        out = await import_result_file(session, s, _done(s, payload))
        await session.commit()

    assert out.status == "imported", out.errors
    assert (out.evaluations, out.letters, out.suitable) == (1, 1, 1)
    assert out.stale_profile is True
    assert "результат сделан для профиля версии 1, текущая 2" in out.warnings
    assert "30: дубликат результата пропущен" in out.warnings
    assert any(w.startswith("30: письмо короче ожидаемого") for w in out.warnings), out.warnings
    assert "30: клише: командный игрок" in out.warnings
    async with rt.session_factory() as session:
        b = await session.get(ClaudeBatch, batch.id)
        assert b is not None and b.status == "imported"
        result_path = Path(b.result_path or "")
        # the name was taken: the new result gets a suffix instead of overwriting the old one
        assert result_path.exists() and result_path.name != f"batch_{BID}.result.json"
        assert result_path.name.startswith(f"batch_{BID}_")
        v = await session.get(Vacancy, vacancy.id)
        assert v is not None and (v.pipeline_status, v.score, v.batch_id) == ("suitable", 90, None)


async def test_pending_results_wait_until_the_file_is_written(rt: Runtime) -> None:
    s = rt.settings
    async with rt.session_factory() as session:
        await _vacancy(session, "40")
        await _batch(session, s, [_task("evaluate_and_letter", "40")])
        await session.commit()
        path = _done(s, _result([{"vacancy_id": "40", "evaluation": {"score": 55, "verdict": "maybe"}}]))

        # just written: Claude Code may still be writing it
        assert is_stable(path) is False
        assert await import_pending_results(session, s) == []
        assert path.exists()

        old = time.time() - 60
        os.utime(path, (old, old))
        outcomes = await import_pending_results(session, s)
        await session.commit()
    assert [o.status for o in outcomes] == ["imported"]
    assert (outcomes[0].evaluations, outcomes[0].maybe) == (1, 1)
    assert is_stable(path) is False  # moved away: a missing file is never «stable»


async def test_unreadable_file_is_left_in_place(rt: Runtime) -> None:
    s = rt.settings
    async with rt.session_factory() as session:
        out = await import_result_file(session, s, done_dir(s) / "batch_missing.json")
    assert out.status == "rejected"
    assert len(out.errors) == 1 and out.errors[0].startswith("не удалось прочитать файл")
    assert not rejected_dir(s).exists() or not any(rejected_dir(s).iterdir())


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("{oops", "невалидный JSON"),
        ("[1, 2]", "в JSON нет batch_id"),
        ('{"results": []}', "в JSON нет batch_id"),
        ('{"batch_id": "../../panel"}', "неверный batch_id"),
    ],
)
async def test_pasted_result_is_checked_before_it_becomes_a_file(rt: Runtime, text: str, error: str) -> None:
    s = rt.settings
    async with rt.session_factory() as session:
        out = await import_result_text(session, s, text)
    assert out.file == "<paste>"
    assert len(out.errors) == 1 and out.errors[0].startswith(error), out.errors
    assert not done_dir(s).exists() or not any(done_dir(s).iterdir())
