"""Runs the hh capability probe and stores its verdict (shared by the Settings button and the weekly re-probe).

hh.ru closed the applicant methods for new applications in 2025; if they reopen, the panel should notice on its
own: the weekly tick re-runs the safe probe and, when a capability goes from `unavailable` to `available` (or the
effective apply mode changes), records it and notifies the user."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import as_utc, utcnow
from app.hh.probe import ProbeOptions, ProbeReport, run_probe
from app.runtime import Runtime
from app.services.audit import audit
from app.services.state import (
    auth_public_state,
    effective_apply_mode,
    get_hh_auth,
    get_setting,
    get_system_state,
    read_capabilities,
    set_setting,
)

log = structlog.get_logger("probe_runner")
WATCHED = ("negotiations_list", "negotiations_post", "resumes_mine", "resume_get", "chats", "vacancies_search")


async def run_and_store_probe(
    runtime: Runtime, session: AsyncSession, options: ProbeOptions, *, manual_resume_id: str | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    """Runs the probe, stores capabilities/apply mode/report. Returns (report dict, capabilities before)."""
    state = await get_system_state(session)
    before = dict(read_capabilities(state))
    report: ProbeReport = await run_probe(runtime.hh_client(), options)
    report_dict = report.to_dict()
    state.capabilities_json = json.dumps(report.capabilities, ensure_ascii=False)
    state.probe_at = utcnow()
    state.apply_mode = effective_apply_mode(runtime.settings, state, report.apply_mode_suggested)
    report_dict["apply_mode_effective"] = state.apply_mode
    if report.resume_ids:
        await set_setting(session, "resumes_hint", report.resume_ids)
    if manual_resume_id:
        await set_setting(session, "manual_resume_id", manual_resume_id)
    await set_setting(session, "probe_report", report_dict)
    await audit(session, "probe", f"Зонд выполнен: режим {state.apply_mode}", capabilities=report.capabilities)
    path = runtime.settings.data_dir / "probe_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report_dict, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("probe_done", apply_mode=state.apply_mode)
    return report_dict, before


def capability_changes(before: dict[str, str], after: dict[str, str]) -> list[tuple[str, str, str]]:
    """(key, old, new) for watched capabilities that changed between two probes."""
    out: list[tuple[str, str, str]] = []
    for key in WATCHED:
        old, new = before.get(key), after.get(key)
        if old != new and new is not None and old is not None:
            out.append((key, old, new))
    return out


async def probe_due(session: AsyncSession, settings: Settings, now: datetime | None = None) -> bool:
    """PROBE_INTERVAL_DAYS have passed since the last stored probe (or there was none), the user is logged in and the
    panel is not paused. Counted from system_state.probe_at, so restarts of the panel do not reset it."""
    if settings.probe_interval_days <= 0:
        return False
    if not auth_public_state(await get_hh_auth(session))["logged_in"]:
        return False
    state = await get_system_state(session)
    if state.paused_reason:
        return False
    last = as_utc(state.probe_at)
    return last is None or (now or utcnow()) - last >= timedelta(days=settings.probe_interval_days)


async def scheduled_probe(runtime: Runtime) -> dict[str, Any]:
    """Job body `probe`: the safe probe (no publish, no blacklist) with the resume id the user saved."""
    settings: Settings = runtime.settings
    from app.services.notify import emit_event

    async with runtime.session_factory() as session:
        if not auth_public_state(await get_hh_auth(session))["logged_in"]:
            return {"skipped": "не выполнен вход"}
        state = await get_system_state(session)
        if state.paused_reason:
            return {"skipped": f"paused: {state.paused_reason}"}
        prev_mode = state.apply_mode
        resume_id = await get_setting(session, "manual_resume_id")
        options = ProbeOptions(allow_publish=False, allow_blacklist=False, resume_id=resume_id or None)
        report, before = await run_and_store_probe(runtime, session, options)
        after = dict(report.get("capabilities") or {})
        changes = capability_changes(before, after)
        mode_changed = report.get("apply_mode_effective") != prev_mode
        if changes or mode_changed:
            payload = {
                "changes": [{"key": k, "old": o, "new": n} for k, o, n in changes],
                "apply_mode": report.get("apply_mode_effective"),
                "prev_mode": prev_mode,
            }
            await emit_event(session, settings, "capability", payload)
            await audit(
                session,
                "probe",
                "Возможности hh.ru изменились: " + ", ".join(f"{k}: {o} → {n}" for k, o, n in changes),
                level="warning",
            )
        await session.commit()
        return {"changes": len(changes), "apply_mode": report.get("apply_mode_effective"), "mode_changed": mode_changed}
