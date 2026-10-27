"""Paths of files kept in the base, after data/ moved to another machine (README, «Переезд»).

The base keeps the absolute path of the machine that wrote the file: the packets of /hh-batch
(claude_batches.packet_path, result_path) and the uploaded resumes (resumes.file_path). When data/ moves from the
Windows laptop to the Linux server (or back), the files come along but the paths point nowhere: the
importer could not read the packet of a result that was being written during the move and never moved that packet out
of pending/, so the background `claude -p` would take it again and again. At start the panel points such a path
to the file with the same name in the folder where that kind of file lives on this machine.
"""

from __future__ import annotations

from pathlib import Path, PureWindowsPath

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import ClaudeBatch, Resume

log = structlog.get_logger("relocate")


def file_name(stored: str) -> str:
    """The last part of a path written on either system: PureWindowsPath splits on both `\\` and `/`."""
    return PureWindowsPath(stored).name


def local_path(stored: str | None, *folders: Path) -> Path | None:
    """`stored` when it exists here, else the file with the same name in the first of `folders` that has it; None when
    there is no such file."""
    if not stored:
        return None
    path = Path(stored)
    if path.exists():
        return path
    name = file_name(stored)
    if not name:
        return None
    return next((folder / name for folder in folders if (folder / name).exists()), None)


async def relocate_stored_paths(session: AsyncSession, settings: Settings) -> dict[str, int]:
    """Rewrites the stored paths that lead nowhere on this machine; returns how many of each kind were fixed.

    A packet lives in pending/ until its result is imported, then in imported/ (the result as `<name>.result.json`
    next to it). rejected/ is not searched: a rejected result keeps the packet's name there."""
    from app.exchange.packets import imported_dir, pending_dir

    packets = (pending_dir(settings), imported_dir(settings))
    uploads = settings.data_dir / "uploads"
    fixed = {"packets": 0, "results": 0, "resumes": 0}
    for batch in (await session.execute(select(ClaudeBatch))).scalars():
        for attr, kind, folders in (
            ("packet_path", "packets", packets),
            ("result_path", "results", (imported_dir(settings),)),
        ):
            stored = getattr(batch, attr)
            if stored and not Path(stored).exists():
                found = local_path(stored, *folders)
                if found is not None:
                    setattr(batch, attr, str(found))
                    fixed[kind] += 1
    for resume in (await session.execute(select(Resume).where(Resume.file_path.is_not(None)))).scalars():
        if resume.file_path and not Path(resume.file_path).exists():
            found = local_path(resume.file_path, uploads)
            if found is not None:
                resume.file_path = str(found)
                fixed["resumes"] += 1
    if any(fixed.values()):
        from app.services.audit import audit

        await audit(
            session,
            "system",
            f"Пути файлов после переноса данных обновлены: пакетов {fixed['packets']}, результатов "
            f"{fixed['results']}, резюме {fixed['resumes']}",
            packets=fixed["packets"],
            results=fixed["results"],
            resumes=fixed["resumes"],
        )
        log.info("stored_paths_relocated", **fixed)
    return fixed
