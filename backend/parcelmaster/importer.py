"""Files what an importer module found and hands parcels over to carriers."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .carriers.base import (
    CarrierError,
    CarrierModule,
    ImportedShipment,
    ImporterModule,
    SyncRequest,
    TrackingEvent,
    TrackingResult,
)
from .carriers.registry import Registry
from .db import Store
from .status import FINAL, Status
from .tracking import Notice, parse_iso, record_result, timezone

log = logging.getLogger(__name__)

STATE_KEY = "importers"
#: Errors that mean "the sign-in itself is the problem". Trying again with the
#: same credentials would only make the service suspicious of the account.
SIGN_IN_ERRORS = frozenset({"unauthorized", "sign_in_failed", "needs_code", "needs_person"})
MIN_MANUAL_GAP = timedelta(seconds=60)

_locks: dict[str, asyncio.Lock] = {}


def describe(error: Exception) -> str:
    """An unexpected error in one line, enough to tell what went wrong."""
    lines = str(error).strip().splitlines()
    return f"{type(error).__name__}: {lines[0] if lines else ''}"[:300]


def importers(registry: Registry) -> list[ImporterModule]:
    return [module for module in registry.all() if isinstance(module, ImporterModule)]


def get_state(store: Store, module_id: str) -> dict[str, Any]:
    return (store.get_value(STATE_KEY) or {}).get(module_id, {})


def _set_state(store: Store, module_id: str, **changes: Any) -> None:
    states = store.get_value(STATE_KEY) or {}
    states[module_id] = {**states.get(module_id, {}), **changes}
    store.set_value(STATE_KEY, states)


def carrier_for(
    registry: Registry, name: str | None, number: str, carrier_settings: dict[str, Any]
) -> CarrierModule | None:
    """The tracker module that can take over this parcel, if one is set up."""
    trackers = [m for m in registry.all() if m.kind == "tracker" and m.id != "demo"]
    wanted = (name or "").casefold()
    by_name = [m for m in trackers if wanted and m.name.casefold() in wanted]
    by_number = [m for m in trackers if m.matches(number)]
    candidates = by_name or (by_number if len(by_number) == 1 else [])
    for module in candidates:
        if module.is_configured(carrier_settings.get(module.id, {})):
            return module
    return None


def apply(
    store: Store, registry: Registry, module: ImporterModule, found: list[ImportedShipment]
) -> list[Notice]:
    """Bring the stored shipments in line with what the service reports."""
    settings = store.get_settings()
    zone = timezone(settings)
    base = int(settings["polling"]["interval_minutes"])
    now = datetime.now(UTC)
    notices: list[Notice] = []

    for item in found:
        shipment = store.find_by_reference(module.name, item.reference)
        if shipment and shipment["archived_at"]:
            continue  # the user is done with this order
        if shipment is None:
            if all(parcel.status in FINAL for parcel in item.parcels):
                continue  # nothing is on its way; no need to list old orders
            shipment_id = store.save_shipment(
                None,
                {
                    "title": item.title,
                    "direction": "incoming",
                    "note": "",
                    "source": module.name,
                    "reference": item.reference,
                },
                [],
            )
            shipment = store.get_shipment(shipment_id)

        known = {
            parcel["extra"]["import_key"]: parcel
            for parcel in shipment["parcels"]
            if parcel["extra"].get("import_key")
        }
        for position, imported in enumerate(item.parcels, start=1):
            taker = (
                carrier_for(
                    registry, imported.carrier_name, imported.tracking_number, settings["carriers"]
                )
                if imported.tracking_number
                else None
            )
            extra = {"import_key": imported.key, "url": imported.url or ""}
            if imported.items:
                extra["items"] = json.dumps(imported.items, ensure_ascii=False)
            parcel = known.get(imported.key)
            if taker:
                carrier, number = taker.id, taker.normalize(imported.tracking_number)
                # The carrier's own fields: what the user entered wins, then their defaults.
                own_fields = {f.key for f in taker.parcel_fields}
                kept = {k: v for k, v in (parcel or {}).get("extra", {}).items() if k in own_fields}
                extra = {
                    **taker.parcel_defaults(settings["carriers"].get(taker.id, {})),
                    **kept,
                    **extra,
                }
            else:
                # Without a carrier number yet, show the order and which parcel of it this is.
                carrier = module.id
                number = imported.tracking_number or f"{item.reference}/{position}"
                if imported.carrier_name:
                    extra["carrier_name"] = imported.carrier_name

            moved = parcel is not None and (parcel["carrier"], parcel["tracking_number"]) != (
                carrier,
                number,
            )
            if (parcel is None or moved) and store.find_duplicate(carrier, number, shipment["id"]):
                continue  # the user already tracks this number by hand
            if parcel is None:
                parcel_id = store.add_parcel(shipment["id"], carrier, number, extra)
            else:
                parcel_id = parcel["id"]
                if moved:
                    store.retarget_parcel(parcel_id, carrier, number, extra)
                elif parcel["extra"] != extra:
                    store.set_parcel_extra(parcel_id, extra)
            parcel = store.get_parcel(parcel_id)

            if carrier == module.id:
                if imported.events is not None:
                    events = imported.events  # the service shows the whole history
                else:
                    events = [
                        TrackingEvent(
                            parse_iso(e["timestamp"]), Status(e["status"]), e["description"]
                        )
                        for e in store.parcel_events(parcel_id)
                    ]
                    if not events or events[-1].description != imported.description:
                        events.append(TrackingEvent(now, imported.status, imported.description))
                result = TrackingResult(
                    status=imported.status,
                    description=imported.description,
                    events=events,
                    eta_from=imported.eta_from,
                )
                notices += record_result(store, parcel, result, base, zone, now, poll=False)
            elif parcel["last_success_at"] is None:
                # Handed over, but the carrier has not answered yet: show what
                # the service knows until it does.
                store.update_parcel(
                    parcel_id, status=str(imported.status), description=imported.description
                )
    return notices


def due(store: Store, module: ImporterModule, carrier_settings: dict[str, Any]) -> bool:
    state = get_state(store, module.id)
    # Only an account whose sign-in was checked is synced on its own.
    if not state.get("connected") or not module.is_configured(carrier_settings):
        return False
    if state.get("blocked"):
        return False  # wait until the credentials change or the user asks
    last = parse_iso(state.get("last_attempt_at"))
    interval = timedelta(minutes=module.sync_interval_minutes)
    return last is None or datetime.now(UTC) - last >= interval


def too_soon(store: Store, module: ImporterModule, gap: timedelta = MIN_MANUAL_GAP) -> bool:
    last = parse_iso(get_state(store, module.id).get("last_attempt_at"))
    return last is not None and datetime.now(UTC) - last < gap


async def connect(
    store: Store,
    registry: Registry,
    module: ImporterModule,
    data_dir: Path,
    credentials: dict[str, Any],
) -> CarrierError | None:
    """Check a sign-in. Only credentials that work are kept.

    The check is a full sync, so the first shipments are there right away.
    Returns the error if the sign-in failed.
    """
    lock = _locks.setdefault(module.id, asyncio.Lock())
    async with lock:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        _set_state(store, module.id, last_attempt_at=now)
        settings = store.get_settings()
        request = SyncRequest(credentials, settings["language"], data_dir / "modules" / module.id)
        try:
            found = await asyncio.to_thread(module.sync, request)
        except CarrierError as error:
            return error
        except Exception as error:  # a broken module must not take the request down
            log.exception("importer %s failed", module.id)
            return CarrierError("module_error", describe(error))
        settings["carriers"][module.id] = credentials
        store.save_settings(settings)
        apply(store, registry, module, found)
        _set_state(
            store,
            module.id,
            connected=True,
            account=str(credentials.get(module.account_field) or ""),
            last_sync_at=now,
            last_error=None,
            blocked=False,
            found=len(found),
        )
        return None


def disconnect(store: Store, module: ImporterModule, data_dir: Path) -> None:
    """Forget the account: credentials, session files and sync state.

    Shipments that were imported stay; they are the user's now.
    """
    settings = store.get_settings()
    settings["carriers"].pop(module.id, None)
    store.save_settings(settings)
    shutil.rmtree(data_dir / "modules" / module.id, ignore_errors=True)
    states = store.get_value(STATE_KEY) or {}
    states.pop(module.id, None)
    store.set_value(STATE_KEY, states)


async def run(
    store: Store, registry: Registry, module: ImporterModule, data_dir: Path
) -> list[Notice]:
    """Sync one importer now and record how it went."""
    settings = store.get_settings()
    own = settings["carriers"].get(module.id, {})
    now = datetime.now(UTC).isoformat(timespec="seconds")
    if not module.is_configured(own):
        _set_state(store, module.id, last_error={"code": "not_configured", "detail": ""})
        return []
    lock = _locks.setdefault(module.id, asyncio.Lock())
    if lock.locked():
        return []
    async with lock:
        _set_state(store, module.id, last_attempt_at=now)
        request = SyncRequest(own, settings["language"], data_dir / "modules" / module.id)
        try:
            found = await asyncio.to_thread(module.sync, request)
        except CarrierError as error:
            _set_state(
                store,
                module.id,
                last_error={"code": error.code, "detail": error.detail},
                blocked=error.code in SIGN_IN_ERRORS,
            )
            return []
        except Exception as error:  # a broken module must not stop the scheduler
            log.exception("importer %s failed", module.id)
            _set_state(
                store,
                module.id,
                last_error={"code": "module_error", "detail": describe(error)},
            )
            return []
        notices = apply(store, registry, module, found)
        _set_state(
            store, module.id, last_sync_at=now, last_error=None, blocked=False, found=len(found)
        )
        return notices
