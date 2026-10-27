"""Candidate profile: resume sources, preferences, versioned profile.md."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.db.models import CandidateProfile, Resume, SelectionProfile
from app.exchange.profile_md import render_profile_md
from app.hh.client import HHClient
from app.hh.errors import HHError, HHForbidden, HHNotFound
from app.hh.resume import NormalizedResume, normalize_resume, resume_to_text
from app.services.audit import audit
from app.services.preferences import Preferences, get_preferences, save_preferences
from app.services.resume_parse import PARSER_VERSION, parse_resume_text
from app.services.resume_text import extract_layout_text, extract_text
from app.services.state import get_setting, get_system_state, read_capabilities, set_setting, update_capability

log = structlog.get_logger("profile")

PROFILE_MD_NAME = "profile.md"


class ProfileError(Exception):
    """User-facing error with an HTTP status hint."""

    def __init__(self, message: str, status: int = 400) -> None:
        self.status = status
        super().__init__(message)


# ---- helpers ----------------------------------------------------------------


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    dt = as_utc(value)
    return dt.isoformat(timespec="seconds") if dt else None


def profile_md_path(settings: Settings) -> Path:
    return settings.exchange_dir / PROFILE_MD_NAME


def uploads_dir(settings: Settings) -> Path:
    return settings.data_dir / "uploads"


def safe_filename(name: str) -> str:
    base = re.sub(r"[^\w.\-]+", "_", name.strip(), flags=re.UNICODE).strip("._") or "resume"
    return base[:120]


# ---- resumes ----------------------------------------------------------------


async def list_resumes(session: AsyncSession) -> list[Resume]:
    rows = (await session.execute(select(Resume).order_by(Resume.source, Resume.id))).scalars().all()
    return list(rows)


async def primary_resume(session: AsyncSession, source: str) -> Resume | None:
    return (
        await session.execute(select(Resume).where(Resume.source == source, Resume.is_primary.is_(True)).limit(1))
    ).scalar_one_or_none()


async def _make_primary(session: AsyncSession, resume: Resume) -> None:
    others = (
        await session.execute(select(Resume).where(Resume.source == resume.source, Resume.id != resume.id))
    ).scalars()
    for other in others:
        other.is_primary = False
    resume.is_primary = True
    await session.flush()


def resume_row(resume: Resume) -> dict[str, Any]:
    return {
        "id": resume.id,
        "source": resume.source,
        "title": resume.title,
        "hh_resume_id": resume.hh_resume_id,
        "file_name": resume.file_name,
        "is_primary": resume.is_primary,
        "chars": len(resume.text_extracted or ""),
        "updated_at": _iso(resume.updated_at),
        "hh_updated_at": _iso(resume.hh_updated_at),
        "can_publish_or_update": resume.can_publish_or_update,
        "next_publish_at": _iso(resume.next_publish_at),
        "alternate_url": _alternate_url(resume),
    }


def _alternate_url(resume: Resume) -> str | None:
    if resume.structured_json:
        try:
            return NormalizedResume.model_validate_json(resume.structured_json).alternate_url
        except ValueError:
            return None
    return None


def hh_list_item(item: dict[str, Any]) -> dict[str, Any]:
    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    assert isinstance(status, dict)
    return {
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or ""),
        "status": str(status.get("id") or status.get("name") or ""),
        "updated_at": item.get("updated_at"),
        "alternate_url": item.get("alternate_url"),
        "can_publish_or_update": item.get("can_publish_or_update"),
        "next_publish_at": item.get("next_publish_at"),
        "total_views": item.get("total_views"),
        "new_views": item.get("new_views"),
    }


async def hh_list_resumes(session: AsyncSession, client: HHClient) -> list[dict[str, Any]]:
    state = await get_system_state(session)
    caps = read_capabilities(state)
    if caps.get("resumes_mine") == "unavailable":
        raise ProfileError(
            "По данным зонда список резюме (GET /resumes/mine) недоступен приложению. "
            "Укажите resume_id вручную или загрузите резюме файлом.",
            status=409,
        )
    try:
        data = await client.get("/resumes/mine")
    except (HHForbidden, HHNotFound) as exc:
        await update_capability(session, "resumes_mine", "unavailable")
        raise ProfileError(
            f"hh.ru отклонил запрос списка резюме ({exc.summary()}). Укажите resume_id вручную или загрузите файл.",
            status=502,
        ) from exc
    except HHError as exc:
        raise ProfileError(f"Не удалось получить список резюме: {exc.summary()}", status=502) from exc
    raw_items = data.get("items") if isinstance(data, dict) else None
    items = [hh_list_item(i) for i in (raw_items or []) if isinstance(i, dict)]
    await update_capability(session, "resumes_mine", "available")
    hint = [{"id": i["id"], "title": i["title"], "status": i["status"]} for i in items]
    await set_setting(session, "resumes_hint", hint)
    return items


async def import_hh_resume(session: AsyncSession, settings: Settings, client: HHClient, resume_id: str) -> Resume:
    resume_id = resume_id.strip()
    if resume_id.startswith("http"):
        resume_id = resume_id.rstrip("/").split("/")[-1].split("?")[0]
    if not re.fullmatch(r"[0-9a-fA-F]{8,64}", resume_id):
        raise ProfileError("resume_id должен быть шестнадцатеричной строкой из адреса https://hh.ru/resume/<id>")

    state = await get_system_state(session)
    caps = read_capabilities(state)
    if caps.get("resume_get") == "unavailable":
        raise ProfileError(
            "По данным зонда просмотр резюме (GET /resumes/{id}) недоступен приложению. Загрузите резюме файлом.",
            status=409,
        )
    try:
        raw = await client.get(f"/resumes/{resume_id}")
    except (HHForbidden, HHNotFound) as exc:
        if isinstance(exc, HHForbidden):
            await update_capability(session, "resume_get", "unavailable")
        raise ProfileError(
            f"hh.ru не отдал резюме {resume_id} ({exc.summary()}). Проверьте id или загрузите резюме файлом.",
            status=502,
        ) from exc
    except HHError as exc:
        raise ProfileError(f"Не удалось получить резюме: {exc.summary()}", status=502) from exc
    if not isinstance(raw, dict):
        raise ProfileError("hh.ru вернул неожиданный ответ вместо резюме", status=502)

    await update_capability(session, "resume_get", "available")
    norm = normalize_resume(raw)
    if not norm.hh_id:
        norm.hh_id = resume_id

    row = (await session.execute(select(Resume).where(Resume.hh_resume_id == resume_id))).scalar_one_or_none()
    created = row is None
    if row is None:
        row = Resume(source="hh", hh_resume_id=resume_id)
        session.add(row)
    row.title = norm.title
    row.raw_json = json.dumps(raw, ensure_ascii=False)
    row.structured_json = norm.model_dump_json()
    row.text_extracted = resume_to_text(norm)
    row.hh_updated_at = _parse_dt(norm.updated_at)
    row.can_publish_or_update = norm.can_publish_or_update
    row.next_publish_at = _parse_dt(norm.next_publish_at)
    await session.flush()
    if created and await primary_resume(session, "hh") is None:
        await _make_primary(session, row)
    await set_setting(session, "manual_resume_id", resume_id)
    verb = "импортировано" if created else "обновлено"
    await audit(session, "profile", f"Резюме из hh.ru {verb}: {norm.title or resume_id}")
    return row


async def upload_resume_file(
    session: AsyncSession, settings: Settings, filename: str, data: bytes, *, make_primary: bool = True
) -> Resume:
    text = extract_text(data, filename)  # raises ValueError subclasses handled by the API layer
    folder = uploads_dir(settings)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    stored = folder / f"{stamp}_{safe_filename(filename)}"
    stored.write_bytes(data)

    parsed = parse_resume_text(text, extract_layout_text(data, filename))
    row = Resume(
        source="file",
        title=filename,
        file_name=filename,
        file_path=str(stored),
        text_extracted=text,
        structured_json=parsed.model_dump_json() if parsed else None,
    )
    session.add(row)
    await session.flush()
    has_primary = (
        await session.execute(select(Resume).where(Resume.source == "file", Resume.is_primary.is_(True)).limit(1))
    ).scalar_one_or_none()
    if make_primary or has_primary is None:
        await _make_primary(session, row)
    note = (
        f", распознано: навыков {len(parsed.skills)}, стаж {parsed.total_experience_months or 0} мес" if parsed else ""
    )
    await audit(session, "profile", f"Загружен файл резюме: {filename} ({len(text)} символов{note})")
    return row


def ensure_file_structure(resume: Resume | None) -> NormalizedResume | None:
    """Structure of an uploaded resume; files stored before the parser existed are parsed on first use."""
    if resume is None or resume.source != "file":
        return None
    norm = _structured(resume)
    if norm is not None and norm.parser_version == PARSER_VERSION:
        return norm
    layout = None
    if resume.file_path and resume.file_name:
        try:
            layout = extract_layout_text(Path(resume.file_path).read_bytes(), resume.file_name)
        except OSError:
            layout = None
    parsed = parse_resume_text(resume.text_extracted or "", layout)
    resume.structured_json = parsed.model_dump_json() if parsed else None
    return parsed


def primary_structure(hh: Resume | None, file: Resume | None) -> NormalizedResume | None:
    """What the panel knows about the candidate: the hh resume when it exists, otherwise the parsed file."""
    return _structured(hh) or ensure_file_structure(file)


async def set_primary(session: AsyncSession, resume_pk: int) -> Resume:
    row = await session.get(Resume, resume_pk)
    if row is None:
        raise ProfileError("Резюме не найдено", status=404)
    await _make_primary(session, row)
    return row


async def delete_resume(session: AsyncSession, resume_pk: int) -> None:
    row = await session.get(Resume, resume_pk)
    if row is None:
        raise ProfileError("Резюме не найдено", status=404)
    if row.file_path:
        try:
            from pathlib import Path

            Path(row.file_path).unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - best effort
            log.warning("upload_delete_failed", path=row.file_path, error=str(exc))
    was_primary, source = row.is_primary, row.source
    for profile in (
        await session.execute(select(SelectionProfile).where(SelectionProfile.resume_id == row.id))
    ).scalars():
        profile.resume_id = None  # the set falls back to the primary resume
    await session.delete(row)
    await session.flush()
    if was_primary:
        replacement = (
            await session.execute(select(Resume).where(Resume.source == source).order_by(Resume.id.desc()).limit(1))
        ).scalar_one_or_none()
        if replacement is not None:
            replacement.is_primary = True
    await audit(session, "profile", f"Удалено резюме #{resume_pk}")


# ---- profile versions -------------------------------------------------------


async def active_profile(session: AsyncSession) -> CandidateProfile | None:
    return (
        await session.execute(select(CandidateProfile).where(CandidateProfile.is_active.is_(True)).limit(1))
    ).scalar_one_or_none()


def _structured(resume: Resume | None) -> NormalizedResume | None:
    if resume is None or not resume.structured_json:
        return None
    try:
        return NormalizedResume.model_validate_json(resume.structured_json)
    except ValueError:
        return None


def compute_hash(
    resume: NormalizedResume | None,
    file_text: str | None,
    prefs: Preferences,
    set_inputs: list[dict[str, Any]] | None = None,
) -> str:
    payload = {
        "resume": resume.model_dump() if resume else None,
        "file_text": file_text,
        "prefs": prefs.model_dump(),
        "sets": set_inputs or [],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


async def _set_inputs(session: AsyncSession) -> list[dict[str, Any]]:
    """What the per-set profile files depend on: the set's criteria and its resume text."""
    from app.services.selection import set_resumes

    out: list[dict[str, Any]] = []
    for profile_id, resume in sorted((await set_resumes(session)).items()):
        row = await session.get(SelectionProfile, profile_id)
        out.append(
            {
                "set": profile_id,
                "criteria": row.criteria_json if row else "",
                "resume": resume.id,
                "text": resume.text_extracted or "",
            }
        )
    return out


async def write_set_profiles(
    session: AsyncSession, settings: Settings, *, version: int, generated_at: str
) -> list[str]:
    """profile_set<id>.md for every set with its own resume; stale files of sets without one are removed."""
    from app.services.selection import effective_preferences, set_profile_file, set_resumes

    folder = profile_md_path(settings).parent
    folder.mkdir(parents=True, exist_ok=True)
    wanted: set[str] = set()
    written: list[str] = []
    for profile_id, resume in sorted((await set_resumes(session)).items()):
        prefs = await effective_preferences(session, profile_id)
        norm = ensure_file_structure(resume) if resume.source == "file" else _structured(resume)
        md = render_profile_md(
            version=version,
            generated_at=generated_at,
            resume=norm,
            file_text=resume.text_extracted if resume.source == "file" else None,
            file_name=resume.file_name,
            prefs=prefs,
        )
        name = set_profile_file(profile_id)
        (folder / name).write_text(md, encoding="utf-8")
        wanted.add(name)
        written.append(name)
    for stale in folder.glob("profile_set*.md"):
        if stale.name not in wanted:
            stale.unlink(missing_ok=True)
    return written


def _write_md(settings: Settings, text: str) -> None:
    path = profile_md_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


async def rebuild_profile(
    session: AsyncSession, settings: Settings, *, force: bool = False
) -> tuple[CandidateProfile | None, bool]:
    """Creates a new profile version when inputs changed. Returns (active_profile, created_new)."""
    hh = await primary_resume(session, "hh")
    file = await primary_resume(session, "file")
    prefs = await get_preferences(session)
    active = await active_profile(session)

    if hh is None and file is None:
        if active is not None:
            active.is_active = False
            profile_md_path(settings).unlink(missing_ok=True)
            await audit(session, "profile", "Источников резюме нет — профиль деактивирован", level="warning")
        return None, False

    norm = primary_structure(hh, file)
    file_text = file.text_extracted if file else None
    set_inputs = await _set_inputs(session)
    digest = compute_hash(norm, file_text, prefs, set_inputs)
    if active is not None and active.hash == digest and not force:
        if not profile_md_path(settings).exists():
            _write_md(settings, active.profile_md)
        return active, False

    max_version = (await session.execute(select(func.max(CandidateProfile.version)))).scalar() or 0
    version = int(max_version) + 1
    generated_at = utcnow().isoformat(timespec="seconds")
    md = render_profile_md(
        version=version,
        generated_at=generated_at,
        resume=norm,
        file_text=file_text,
        file_name=file.file_name if file else None,
        prefs=prefs,
    )
    if active is not None:
        active.is_active = False
    new = CandidateProfile(
        version=version,
        resume_id=hh.id if hh else None,
        file_resume_id=file.id if file else None,
        preferences_json=prefs.model_dump_json(),
        profile_md=md,
        hash=digest,
        is_active=True,
    )
    session.add(new)
    await session.flush()
    _write_md(settings, md)
    set_files = await write_set_profiles(session, settings, version=version, generated_at=generated_at)
    await audit(session, "profile", f"Собран profile.md версии {version}", chars=len(md), set_files=set_files)
    log.info("profile_rebuilt", version=version, chars=len(md), set_files=set_files)
    return new, True


# ---- preferences ------------------------------------------------------------


async def update_preferences(session: AsyncSession, settings: Settings, prefs: Preferences) -> None:
    await save_preferences(session, prefs)
    await audit(session, "profile", "Анкета предпочтений сохранена")
    await rebuild_profile(session, settings)


# ---- view -------------------------------------------------------------------


async def profile_view(session: AsyncSession, settings: Settings, *, hh_logged_in: bool) -> dict[str, Any]:
    resumes = await list_resumes(session)
    hh = next((r for r in resumes if r.source == "hh" and r.is_primary), None)
    file = next((r for r in resumes if r.source == "file" and r.is_primary), None)
    prefs = await get_preferences(session)
    norm = primary_structure(hh, file)
    active = await active_profile(session)
    state = await get_system_state(session)
    caps = read_capabilities(state)
    path = profile_md_path(settings)

    warnings: list[str] = []
    if not resumes:
        warnings.append("Нет источников резюме: импортируйте из hh.ru или загрузите файл.")
    if caps.get("resumes_mine") == "unavailable":
        warnings.append(
            "Список резюме из hh.ru недоступен по данным зонда — укажите resume_id вручную или загрузите файл."
        )
    if caps.get("resume_get") == "unavailable":
        warnings.append("Просмотр резюме через API недоступен по данным зонда — профиль строится из файла.")
    if active is not None and norm is not None and file is not None and not path.exists():
        warnings.append("Файл profile.md отсутствует — нажмите «Пересобрать».")

    merged = prefs.model_dump()
    merged["skills_weights"] = [s.model_dump() for s in prefs.merged_skills(norm.skills if norm else [])]

    from app.services.selection import set_profile_file, set_resumes

    set_files = [
        {
            "profile_id": pid,
            "resume_id": r.id,
            "resume_name": r.file_name or r.title,
            "file": set_profile_file(pid),
            "exists": (path.parent / set_profile_file(pid)).exists(),
        }
        for pid, r in sorted((await set_resumes(session)).items())
    ]
    return {
        "version": active.version if active else None,
        "generated_at": _iso(active.created_at) if active else None,
        "hash": active.hash if active else None,
        "profile_md_path": str(path),
        "profile_md_exists": path.exists(),
        "profile_md_chars": len(active.profile_md) if active else 0,
        "set_profiles": set_files,
        "resumes": [resume_row(r) for r in resumes],
        "structured": norm.model_dump() if norm else None,
        "file": {
            "name": file.file_name,
            "chars": len(file.text_extracted or ""),
            "preview": (file.text_extracted or "")[:2000],
        }
        if file
        else None,
        "preferences": merged,
        "capabilities": {k: caps.get(k) for k in ("resumes_mine", "resume_get", "resume_publish")},
        "manual_resume_id": await get_setting(session, "manual_resume_id"),
        "resumes_hint": await get_setting(session, "resumes_hint") or [],
        "hh_logged_in": hh_logged_in,
        "warnings": warnings,
    }
