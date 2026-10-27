"""Background watcher for data/exchange/done: imports results as soon as files are stable."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog
from watchfiles import awatch

from app.exchange.importer import STABLE_SECONDS, import_pending_results
from app.exchange.packets import done_dir

if TYPE_CHECKING:  # pragma: no cover
    from app.runtime import Runtime

log = structlog.get_logger("exchange.watcher")


async def import_once(runtime: Runtime, *, require_stable: bool = True) -> list[dict[str, Any]]:
    async with runtime.session_factory() as session:
        outcomes = await import_pending_results(session, runtime.settings, require_stable=require_stable)
        await session.commit()
    return [o.to_dict() for o in outcomes]


async def watch_done_dir(runtime: Runtime, stop: asyncio.Event) -> None:
    folder = done_dir(runtime.settings)
    folder.mkdir(parents=True, exist_ok=True)
    log.info("watcher_started", path=str(folder))
    try:
        async for changes in awatch(folder, stop_event=stop, debounce=500, step=200):
            if not any(str(p).endswith(".json") for _, p in changes):
                continue
            await asyncio.sleep(STABLE_SECONDS)
            try:
                outcomes = await import_once(runtime)
                if outcomes:
                    log.info("watcher_imported", files=[o["file"] for o in outcomes])
            except Exception as exc:  # noqa: BLE001 - the watcher must survive bad files
                log.exception("watcher_import_failed", error=str(exc))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("watcher_crashed", error=str(exc))
    finally:
        log.info("watcher_stopped")
