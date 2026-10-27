"""Paths kept in the base after data/ moved to another machine (services/relocate.py, README «Переезд»).

2026-10-22, the move from the Windows laptop to the Linux server: a packet written on the laptop kept
`C:\\_PROJECTS\\…\\pending\\batch_….json` in claude_batches.packet_path. The result of the background `claude -p` was
rejected («не удалось прочитать пакет»), and the packet stayed in pending/, where the next run would take it again.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db.models import AuditLog, ClaudeBatch, Resume, Vacancy
from app.exchange.importer import import_result_file
from app.exchange.packets import cancel_batch, done_dir, imported_dir, pending_dir, rejected_dir
from app.exchange.schemas import Packet, PacketTask
from app.runtime import Runtime
from app.services.relocate import file_name, local_path, relocate_stored_paths

BID = "20261022_1055_36b2"
OTHER = "20261021_0900_aa11"
GONE = "20261020_0800_bb22"
# where the files lived on the machine that wrote the paths (not this one: a real checkout here has such files)
LAPTOP = r"C:\laptop-of-the-test\job-search-panel\data"
SERVER = "/server-of-the-test/jobsearchpanel/app/data"


@pytest.fixture
async def rt(settings: Settings):
    runtime = await Runtime.create(settings)
    yield runtime
    await runtime.close()


def _write_packet(folder: Path, batch_id: str, vacancy_id: str = "77") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    packet = Packet(
        batch_id=batch_id,
        created_at="2026-10-22T10:55:00",
        profile_version=1,
        letter_defaults={"min_chars": 600, "max_chars": 1200},
        tasks=[
            PacketTask.model_validate(
                {
                    "type": "evaluate_and_letter",
                    "vacancy": {"id": vacancy_id, "name": "Python-разработчик", "employer": {"name": "Рога и копыта"}},
                }
            )
        ],
    )
    path = folder / f"batch_{batch_id}.json"
    path.write_text(packet.model_dump_json(), encoding="utf-8")
    return path


def _batch(batch_id: str, packet_path: str, **extra: object) -> ClaudeBatch:
    return ClaudeBatch(
        batch_id=batch_id,
        packet_path=packet_path,
        status=str(extra.pop("status", "pending")),
        tasks_count=1,
        profile_version=1,
        protocol_version=1,
        **extra,
    )


def test_the_name_is_read_from_a_path_of_either_system(tmp_path: Path) -> None:
    windows = rf"{LAPTOP}\exchange\pending\batch_{BID}.json"
    linux = f"{SERVER}/exchange/pending/batch_{BID}.json"
    assert file_name(windows) == file_name(linux) == f"batch_{BID}.json"
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (second / f"batch_{BID}.json").write_text("{}", encoding="utf-8")
    for stored in (windows, linux):
        assert local_path(stored, first, second) == second / f"batch_{BID}.json"
    here = second / f"batch_{BID}.json"
    assert local_path(str(here)) == here  # a path of this machine is taken as it is
    assert local_path(windows, first) is None
    assert local_path(None, first) is None and local_path("", first) is None


async def test_result_of_a_packet_written_on_the_laptop_is_imported_and_the_packet_leaves_pending(rt: Runtime) -> None:
    s = rt.settings
    _write_packet(pending_dir(s), BID)
    async with rt.session_factory() as session:
        vacancy = Vacancy(
            hh_id="77", name="Python-разработчик", employer_name="Рога и копыта", pipeline_status="awaiting_claude"
        )
        vacancy.batch_id = BID
        session.add(vacancy)
        session.add(_batch(BID, rf"{LAPTOP}\exchange\pending\batch_{BID}.json"))
        await session.commit()
        done = done_dir(s)
        done.mkdir(parents=True, exist_ok=True)
        result = {
            "protocol_version": 1,
            "batch_id": BID,
            "results": [
                {
                    "vacancy_id": "77",
                    "evaluation": {"score": 80, "verdict": "apply", "summary": "Подходит"},
                    "letter": {"text": "Здравствуйте! Делал похожие проекты на Python и LLM.", "language": "ru"},
                }
            ],
        }
        (done / f"batch_{BID}.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        out = await import_result_file(session, s, done / f"batch_{BID}.json")
        await session.commit()

    assert out.status == "imported", out.errors
    # the packet left pending/: the background claude -p takes every packet there
    assert not (pending_dir(s) / f"batch_{BID}.json").exists()
    assert (imported_dir(s) / f"batch_{BID}.json").exists()
    async with rt.session_factory() as session:
        batch = (await session.execute(select(ClaudeBatch).where(ClaudeBatch.batch_id == BID))).scalar_one()
        assert Path(batch.packet_path) == imported_dir(s) / f"batch_{BID}.json"
        v = await session.get(Vacancy, vacancy.id)
        assert v is not None and (v.pipeline_status, v.score) == ("suitable", 80)


async def test_start_up_repair_points_the_paths_to_this_machine(rt: Runtime) -> None:
    s = rt.settings
    _write_packet(pending_dir(s), BID)
    _write_packet(imported_dir(s), OTHER)
    (imported_dir(s) / f"batch_{OTHER}.result.json").write_text("{}", encoding="utf-8")
    # a rejected result keeps the packet's name: it must not be taken for the packet
    rejected_dir(s).mkdir(parents=True, exist_ok=True)
    (rejected_dir(s) / f"batch_{GONE}.json").write_text("{}", encoding="utf-8")
    uploads = s.data_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    (uploads / "20260912_134630_RA_CV.pdf").write_bytes(b"%PDF-1.4")
    async with rt.session_factory() as session:
        session.add(_batch(BID, rf"{LAPTOP}\exchange\pending\batch_{BID}.json"))
        session.add(
            _batch(
                OTHER,
                f"{SERVER}/exchange/imported/batch_{OTHER}.json",
                status="imported",
                result_path=rf"{LAPTOP}\exchange\imported\batch_{OTHER}.result.json",
            )
        )
        session.add(_batch(GONE, rf"{LAPTOP}\exchange\pending\batch_{GONE}.json", status="cancelled"))
        session.add(
            Resume(
                source="file",
                title="RA_CV.pdf",
                file_name="RA_CV.pdf",
                file_path=rf"{LAPTOP}\uploads\20260912_134630_RA_CV.pdf",
            )
        )
        await session.commit()

        fixed = await relocate_stored_paths(session, s)
        await session.commit()
        assert fixed == {"packets": 2, "results": 1, "resumes": 1}
        assert await relocate_stored_paths(session, s) == {"packets": 0, "results": 0, "resumes": 0}

    async with rt.session_factory() as session:
        rows = {b.batch_id: b for b in (await session.execute(select(ClaudeBatch))).scalars()}
        assert Path(rows[BID].packet_path) == pending_dir(s) / f"batch_{BID}.json"
        assert Path(rows[OTHER].packet_path) == imported_dir(s) / f"batch_{OTHER}.json"
        assert Path(rows[OTHER].result_path or "") == imported_dir(s) / f"batch_{OTHER}.result.json"
        assert rows[GONE].packet_path == rf"{LAPTOP}\exchange\pending\batch_{GONE}.json"  # nothing to point to
        resume = (await session.execute(select(Resume))).scalar_one()
        assert Path(resume.file_path or "") == uploads / "20260912_134630_RA_CV.pdf"
        notes = [a.message for a in (await session.execute(select(AuditLog))).scalars()]
        assert any(
            "Пути файлов после переноса данных обновлены: пакетов 2, результатов 1, резюме 1" in n for n in notes
        )


async def test_cancel_removes_a_packet_written_on_another_machine(rt: Runtime) -> None:
    s = rt.settings
    path = _write_packet(pending_dir(s), BID)
    async with rt.session_factory() as session:
        batch = _batch(BID, f"{SERVER}/exchange/pending/batch_{BID}.json")
        session.add(batch)
        await session.commit()
        await cancel_batch(session, s, batch)
        await session.commit()
    assert not path.exists()


async def test_the_start_record_comes_before_the_repair(rt: Runtime) -> None:
    """The start record reads the newest «system» record to tell a clean stop; the repair writes one of its own. With
    the repair first, the first start after the move read «без штатной остановки» (2026-10-22)."""
    from app.main import startup_records
    from app.services.lifecycle import STARTED, STOPPED, record_stop

    s = rt.settings
    _write_packet(pending_dir(s), BID)
    async with rt.session_factory() as session:
        await record_stop(session)
        session.add(_batch(BID, rf"{LAPTOP}\exchange\pending\batch_{BID}.json"))
        await session.commit()

    await startup_records(rt)

    async with rt.session_factory() as session:
        rows = (
            await session.execute(select(AuditLog).where(AuditLog.area == "system").order_by(AuditLog.id))
        ).scalars()
        messages = [row.message for row in rows]
    assert messages[0] == STOPPED
    assert messages[1].startswith(STARTED) and "остановлена штатно" in messages[1], messages
    assert messages[2].startswith("Пути файлов после переноса данных обновлены: пакетов 1"), messages
