"""The background loop that keeps open parcels up to date."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx

from . import importer
from .carriers.registry import Registry
from .db import Store
from .discovery import empty_trash
from .notify import Notifier
from .tracking import auto_archive, periodic_notices, refresh_parcel, resting

log = logging.getLogger(__name__)

TICK_SECONDS = 30
BATCH = 20
# How many parcels the round at the start of the night rest asks at most.
LAST_CALL = 200
PAUSE_BETWEEN_LOOKUPS = 0.5


class Scheduler:
    def __init__(
        self,
        store: Store,
        registry: Registry,
        http: httpx.AsyncClient,
        notifier: Notifier,
        data_dir: Path,
    ) -> None:
        self.data_dir = data_dir
        self.store = store
        self.registry = registry
        self.http = http
        self.notifier = notifier
        self.last_run: datetime | None = None
        # Whether the last run fell into the night rest; unknown after a start.
        self._rested: bool | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="parcelmaster-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("scheduler run failed")
            await asyncio.sleep(TICK_SECONDS)

    async def run_once(self) -> None:
        now = datetime.now(UTC)
        settings = self.store.get_settings()
        # During the night rest nothing is asked; what is due waits for the morning.
        # As the rest begins everything is asked once more, due or not, so the
        # night starts from what the carriers know by then.
        rest = resting(settings)
        last_call = (
            rest and self._rested is False and settings["polling"]["night"].get("last_call", True)
        )
        self._rested = rest
        if last_call:
            log.info("night rest begins: asking everything once more")
        if not rest or last_call:
            due = self.store.due_parcels(
                None if last_call else now.isoformat(timespec="seconds"),
                LAST_CALL if last_call else BATCH,
            )
            for parcel_id in due:
                # One parcel that fails must not keep the others from being asked.
                try:
                    notices = await refresh_parcel(self.store, self.registry, self.http, parcel_id)
                    await self.notifier.handle(notices)
                except Exception:
                    log.exception("lookup of parcel %s failed", parcel_id)
                await asyncio.sleep(PAUSE_BETWEEN_LOOKUPS)
            for module in importer.importers(self.registry):
                try:
                    carrier_settings = settings["carriers"].get(module.id, {})
                    if importer.due(self.store, module, carrier_settings, early=last_call):
                        notices = await importer.run(
                            self.store, self.registry, module, self.data_dir
                        )
                    else:
                        # Between two syncs: orders whose parcel is at the door about now.
                        notices = await importer.follow_deliveries(
                            self.store, self.registry, module, self.data_dir
                        )
                    await self.notifier.handle(notices)
                except Exception:
                    log.exception("importer %s failed", module.id)
            try:
                await importer.find_carriers(self.store, self.registry, self.http)
            except Exception:
                log.exception("finding carriers failed")
        # Each of the housekeeping steps runs whatever became of the one before.
        for step in (self._periodic, self.notifier.flush_held, self._tidy_up):
            try:
                await step()
            except Exception:
                log.exception("scheduler step %s failed", step.__name__)
        self.last_run = now

    async def _periodic(self) -> None:
        await self.notifier.handle(periodic_notices(self.store, self.registry))

    async def _tidy_up(self) -> None:
        auto_archive(self.store)
        empty_trash(self.store)
