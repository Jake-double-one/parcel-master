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
from .notify import Notifier
from .tracking import auto_archive, periodic_notices, refresh_parcel

log = logging.getLogger(__name__)

TICK_SECONDS = 30
BATCH = 20
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
        for parcel_id in self.store.due_parcels(now.isoformat(timespec="seconds"), BATCH):
            notices = await refresh_parcel(self.store, self.registry, self.http, parcel_id)
            await self.notifier.handle(notices)
            await asyncio.sleep(PAUSE_BETWEEN_LOOKUPS)
        carrier_settings = self.store.get_settings()["carriers"]
        for module in importer.importers(self.registry):
            if importer.due(self.store, module, carrier_settings.get(module.id, {})):
                notices = await importer.run(self.store, self.registry, module, self.data_dir)
                await self.notifier.handle(notices)
        await self.notifier.handle(periodic_notices(self.store, self.registry))
        await self.notifier.flush_held()
        auto_archive(self.store)
        self.last_run = now
