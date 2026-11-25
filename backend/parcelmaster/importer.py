"""Files what an importer module found and hands parcels over to carriers."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

from .carriers.base import (
    CarrierError,
    CarrierModule,
    ImportedShipment,
    ImporterModule,
    SyncRequest,
    TrackingEvent,
    TrackingResult,
    switched_on,
)
from .carriers.registry import Registry
from .db import Store
from .status import FINAL, Status
from .tracking import Notice, parse_iso, record_result, saver_on, timezone

log = logging.getLogger(__name__)

STATE_KEY = "importers"
DISMISSED_KEY = "importers_dismissed"
DISCOVERED = "discovered"  # source of a shipment a companion script came across
#: With the saver on, a parcel goes to its carrier's module from these states on.
HAND_OVER_FROM = frozenset({Status.OUT_FOR_DELIVERY, Status.READY_FOR_PICKUP, Status.PROBLEM})
#: Keys in a parcel's extra that tracking writes for itself.
TRACKING_NOTES = (
    "window_told",
    "stops_left",
    "stops_about",
    "live_at",
    "live_lost",
    "van",
    "home",
)
#: How the reference of a delivery begins that is only known through a shared link.
SHARED = "share:"
MAX_DISMISSED = 500
#: Days a finished order still counts as recent when auto-archive is off.
DEFAULT_RECENT_DAYS = 3
#: Errors that mean "the sign-in itself is the problem". Trying again with the
#: same credentials would only make the service suspicious of the account.
SIGN_IN_ERRORS = frozenset({"unauthorized", "sign_in_failed", "needs_code", "needs_person"})
MIN_MANUAL_GAP = timedelta(seconds=60)
#: Bounds for a user-chosen sync interval. A shop account is not asked more
#: often than every half hour, so the sign-in does not look like an attack.
MIN_SYNC_MINUTES = 30
#: While the service delivers a parcel itself today, it is asked this often.
DELIVERY_DAY_SYNC_MINUTES = 30
MAX_SYNC_MINUTES = 24 * 60

_locks: dict[str, asyncio.Lock] = {}
#: Minutes between two looks at an order whose parcel is being delivered right now.
FOLLOW_MINUTES = 10
#: The same while the service names the stops left on the round.
FOLLOW_NEAR_MINUTES = 5
#: Fewer stops than CLOSE_STOPS: the van is minutes away.
FOLLOW_CLOSE_MINUTES = 2
CLOSE_STOPS = 5
_followed: dict[tuple[str, str], float] = {}


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


def _just_arrived(item: ImportedShipment, now: datetime, zone: Any, recent_days: int) -> bool:
    """Whether an order that is complete arrived recently enough to be worth showing."""
    times = [parcel.status_time for parcel in item.parcels if parcel.status_time]
    for parcel in item.parcels:
        times += [e.timestamp for e in parcel.events or [] if e.timestamp and e.status in FINAL]
    if not times:
        return False  # the service does not say when; assume it was long ago
    latest = max(time if time.tzinfo else time.replace(tzinfo=zone) for time in times)
    return now - latest <= timedelta(days=recent_days)


def dismiss(store: Store, source: str, reference: str) -> None:
    """Remember that the user deleted an imported order, so it does not come back."""
    dismissed = store.get_value(DISMISSED_KEY) or []
    if [source, reference] not in dismissed:
        store.set_value(DISMISSED_KEY, [*dismissed, [source, reference]][-MAX_DISMISSED:])


def settled_keys(store: Store) -> frozenset[str]:
    """Import keys of the parcels that have arrived or gone back, archived ones included."""
    return frozenset(
        parcel["extra"]["import_key"]
        for archived in (False, True)
        for shipment in store.list_shipments(archived)
        for parcel in shipment["parcels"]
        if parcel["extra"].get("import_key") and Status(parcel["status"]) in FINAL
    )


def shared_links(store: Store, module: ImporterModule) -> tuple[str, ...]:
    """Links of the service's shared deliveries that have not arrived yet."""
    return tuple(
        parcel["extra"]["url"]
        for parcel in store.open_parcels()
        if parcel["extra"].get("import_key", "").startswith(SHARED)
        and parcel["extra"].get("url")
        and Status(parcel["status"]) not in FINAL
    )


def carrier_for(
    registry: Registry, name: str | None, number: str, carrier_settings: dict[str, Any]
) -> CarrierModule | None:
    """The tracker module that can take over this parcel, if one is set up."""
    trackers = [
        m
        for m in registry.all()
        if m.kind == "tracker" and m.id != "demo" and switched_on(carrier_settings.get(m.id, {}))
    ]
    wanted = (name or "").casefold()
    by_name = [m for m in trackers if wanted and m.name.casefold() in wanted]
    by_number = [m for m in trackers if m.matches(number)]
    candidates = by_name or (by_number if len(by_number) == 1 else [])
    for module in candidates:
        if module.is_configured(carrier_settings.get(module.id, {})):
            return module
    return None


def _same_parcel_elsewhere(
    store: Store, module: ImporterModule, item: ImportedShipment
) -> dict[str, Any] | None:
    """Another open shipment of this service that holds one of the item's parcels."""
    numbers = {parcel.tracking_number for parcel in item.parcels if parcel.tracking_number}
    if not numbers:
        return None
    for other in store.list_shipments(False):
        if other["source"] != module.name or other["reference"] == item.reference:
            continue
        if any(parcel["tracking_number"] in numbers for parcel in other["parcels"]):
            return other
    return None


#: Separates an order number from the key of a parcel that has a card of its own.
PART = "#"


def order_of(reference: str) -> str:
    """The order a shipment belongs to: its reference without a parcel part."""
    return reference.split(PART, 1)[0]


def split_orders(found: list[ImportedShipment]) -> list[ImportedShipment]:
    """One card per parcel once its carrier number is known.

    Parcels the shop has not handed to a carrier yet stay together under the
    order. Each parcel with a tracking number becomes a shipment of its own,
    titled after what is inside, so it can arrive and leave the overview on
    its own. Orders sharing one box meet again on one card (see together.py).
    """
    result: list[ImportedShipment] = []
    for item in found:
        numbered = [p for p in item.parcels if p.tracking_number]
        if item.reference.startswith(SHARED) or not numbered:
            result.append(item)
            continue
        waiting = [p for p in item.parcels if not p.tracking_number]
        if waiting:
            result.append(ImportedShipment(item.reference, item.title, waiting))
        total = len(item.parcels)
        for position, parcel in enumerate(item.parcels, start=1):
            if not parcel.tracking_number:
                continue
            title = parcel.items[0] if parcel.items else item.title
            result.append(
                ImportedShipment(
                    f"{item.reference}{PART}{parcel.key}",
                    title,
                    [dataclasses.replace(parcel, part=f"{position}/{total}")],
                )
            )
    return result


def _take_from_order(store: Store, module: ImporterModule, shipment: dict[str, Any], key: str):
    """Move a parcel that so far lay in its order's card into its own card."""
    order = store.find_by_reference(module.name, order_of(shipment["reference"]))
    if order is None or order["id"] == shipment["id"]:
        return
    for parcel in order["parcels"]:
        if parcel["extra"].get("import_key") == key:
            store.move_parcel(parcel["id"], shipment["id"])
    if not store.get_shipment(order["id"])["parcels"]:
        store.delete_shipment(order["id"])  # every parcel has a card of its own now


def _drop_stale_placeholders(
    store: Store, module: ImporterModule, found: list[ImportedShipment]
) -> None:
    """Forget stand-ins the service no longer reports.

    Before the shop names a parcel, it is filed as "<order>/1". Once the shop
    gives it an id, the same parcel comes back under that id, and the stand-in
    would linger in the order's card with a status nobody updates any more.
    """
    for item in found:
        if item.reference.startswith(SHARED):
            continue
        order = store.find_by_reference(module.name, item.reference)
        if order is None or order["archived_at"] or order["deleted_at"]:
            continue
        keys = {parcel.key for parcel in item.parcels}
        for parcel in order["parcels"]:
            stand_in = parcel["carrier"] == module.id and "/" in parcel["tracking_number"]
            if (
                stand_in
                and parcel["extra"].get("import_key") not in keys
                and Status(parcel["status"]) not in FINAL
            ):
                store.delete_parcel(parcel["id"])
        if not store.get_shipment(order["id"])["parcels"]:
            store.delete_shipment(order["id"])  # every parcel has a card of its own


def _in_order_card(store: Store, module: ImporterModule, item: ImportedShipment) -> bool:
    """Whether a parcel of this card still lies in its order's card and must move out."""
    if PART not in item.reference:
        return False
    order = store.find_by_reference(module.name, order_of(item.reference))
    keys = {parcel.key for parcel in item.parcels}
    return order is not None and any(
        parcel["extra"].get("import_key") in keys for parcel in order["parcels"]
    )


def _replace_found(store: Store, item: ImportedShipment) -> None:
    """A shipment a companion script came across steps aside once an order that
    is filed holds the same parcel: the order is the fuller entry."""
    for parcel in item.parcels:
        if not parcel.tracking_number:
            continue
        other = store.find_number(parcel.tracking_number)
        if not other or other["source"] != DISCOVERED or other["deleted_at"]:
            continue
        held = store.get_shipment(other["id"]) or {"parcels": []}
        if other["note"] or len(held["parcels"]) > 1:
            # It holds more than the order does (a note, further parcels): into the
            # bin, where the user finds it, rather than gone.
            store.set_deleted(other["id"], True)
        else:
            store.delete_shipment(other["id"])


def apply(
    store: Store,
    registry: Registry,
    module: ImporterModule,
    found: list[ImportedShipment],
    wanted: bool = False,
) -> list[Notice]:
    """Bring the stored shipments in line with what the service reports.

    ``wanted`` means the user asked for exactly these orders: they are filed
    whatever their state, and come back from the archive.
    """
    settings = store.get_settings()
    zone = timezone(settings)
    base = int(settings["polling"]["interval_minutes"])
    now = datetime.now(UTC)
    notices: list[Notice] = []
    saving = saver_on(settings)
    dismissed = store.get_value(DISMISSED_KEY) or []
    # Orders that arrived within the days they would stay in the overview anyway.
    recent_days = int(settings["polling"]["auto_archive_days"]) or DEFAULT_RECENT_DAYS

    for item in split_orders(found):
        # The same parcel may be heard of twice: in the account's order and through a
        # link shared for it. The order is the fuller entry and wins either way.
        twin = _same_parcel_elsewhere(store, module, item)
        if twin is not None:
            if item.reference.startswith(SHARED):
                continue  # already there as part of its order
            if twin["reference"].startswith(SHARED):
                store.delete_shipment(twin["id"])  # the order takes its place
        shipment = store.find_by_reference(module.name, item.reference)
        if shipment and shipment["deleted_at"]:
            if not wanted:
                continue  # lies in the bin
            store.set_deleted(shipment["id"], False)
        if shipment and shipment["archived_at"]:
            if not wanted:
                continue  # the user is done with this order
            store.set_archived(shipment["id"], False)
        if shipment is None:
            if not wanted:
                if [module.name, item.reference] in dismissed or (
                    [module.name, order_of(item.reference)] in dismissed
                ):
                    continue  # the user deleted this order (or this parcel) here
                if (
                    all(parcel.status in FINAL for parcel in item.parcels)
                    and not _just_arrived(item, now, zone, recent_days)
                    and not _in_order_card(store, module, item)
                ):
                    continue  # arrived a while ago; no need to list old orders
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
        _replace_found(store, item)  # only now: the order is filed for sure
        if PART in item.reference:
            # A parcel that just got its number: it leaves the order's card with its history.
            for imported in item.parcels:
                _take_from_order(store, module, shipment, imported.key)
            shipment = store.get_shipment(shipment["id"])

        known = {
            parcel["extra"]["import_key"]: parcel
            for parcel in shipment["parcels"]
            if parcel["extra"].get("import_key")
        }
        for position, imported in enumerate(item.parcels, start=1):
            parcel = known.get(imported.key)
            if parcel is None and imported.tracking_number:
                # The same parcel heard of another way, such as through a shared link.
                parcel = next(
                    (
                        p
                        for p in shipment["parcels"]
                        if p["tracking_number"] == imported.tracking_number
                    ),
                    None,
                )
            if (
                parcel is not None
                and Status(parcel["status"]) in FINAL
                and imported.status in FINAL
            ):
                continue  # arrived and filed: there is nothing left to bring in line
            taker = (
                carrier_for(
                    registry, imported.carrier_name, imported.tracking_number, settings["carriers"]
                )
                if imported.tracking_number
                else None
            )
            # A carrier whose lookups are scarce is, with the saver on, only asked
            # once its answer adds something: the delivery window on the day, or
            # help when things go wrong. Any other carrier takes over at once.
            handed_over = parcel is not None and parcel["carrier"] != module.id
            if (
                taker
                and saving
                and not handed_over
                and taker.scarce(settings["carriers"].get(taker.id, {}))
                and imported.status not in HAND_OVER_FROM
            ):
                taker = None
            extra = {"import_key": imported.key, "url": imported.url or ""}
            # Another order's entry already follows this parcel at its carrier, and
            # that one tells the news: this entry stays quiet.
            shared_box = False
            if imported.items:
                extra["items"] = json.dumps(imported.items, ensure_ascii=False)
            if imported.part:
                extra["part"] = imported.part
            if taker:
                number = taker.normalize(imported.tracking_number)
                already_there = parcel is not None and (
                    parcel["carrier"],
                    parcel["tracking_number"],
                ) == (taker.id, number)
                if not already_there and store.find_duplicate(taker.id, number, shipment["id"]):
                    # The carrier is already asked about this number, by hand or for
                    # another order in the same box. The service keeps this entry.
                    taker = None
                    shared_box = True
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

            # What tracking itself noted on the parcel stays, whatever the service says.
            for own_note in TRACKING_NOTES:
                if parcel is not None and own_note in parcel["extra"]:
                    extra[own_note] = parcel["extra"][own_note]

            moved = parcel is not None and (parcel["carrier"], parcel["tracking_number"]) != (
                carrier,
                number,
            )
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
                    # A status without words adds nothing to the history.
                    if imported.description and (
                        not events or events[-1].description != imported.description
                    ):
                        events.append(
                            TrackingEvent(
                                imported.status_time or now, imported.status, imported.description
                            )
                        )
                result = TrackingResult(
                    status=imported.status,
                    description=imported.description,
                    events=events,
                    eta_from=imported.eta_from,
                    eta_to=imported.eta_to,
                    stops_left=imported.stops_left,
                )
                heard = record_result(store, parcel, result, base, zone, now, poll=False)
                if imported.tracking_number and not shared_box:
                    # Several orders in one box that the service follows itself: the
                    # oldest of them speaks for the box.
                    first = store.find_number(number)
                    shared_box = bool(
                        first and first["id"] != shipment["id"] and not first["deleted_at"]
                    )
                if not shared_box:
                    notices += heard
            elif parcel["last_success_at"] is None:
                # Handed over, but the carrier has not answered yet: show what
                # the service knows until it does.
                store.update_parcel(
                    parcel_id, status=str(imported.status), description=imported.description
                )
    _drop_stale_placeholders(store, module, found)
    return notices


def interval_minutes(module: ImporterModule, carrier_settings: dict[str, Any]) -> int:
    """Minutes between two syncs: the user's choice, else the module's own pace."""
    try:
        chosen = int(carrier_settings.get("sync_minutes") or 0)
    except (TypeError, ValueError):
        chosen = 0
    if chosen <= 0:
        return module.sync_interval_minutes
    return max(MIN_SYNC_MINUTES, min(MAX_SYNC_MINUTES, chosen))


def pace_minutes(store: Store, module: ImporterModule, carrier_settings: dict[str, Any]) -> int:
    """Minutes until the next sync: the usual interval, shorter on a delivery day.

    While the service itself brings a parcel today, its status and window
    change within the hour, and nobody else is asked about that parcel.
    """
    usual = interval_minutes(module, carrier_settings)
    on_the_van = any(
        parcel["carrier"] == module.id and parcel["status"] == Status.OUT_FOR_DELIVERY
        for parcel in store.open_parcels()
    )
    return min(usual, DELIVERY_DAY_SYNC_MINUTES) if on_the_van else usual


def next_sync_at(
    store: Store, module: ImporterModule, carrier_settings: dict[str, Any]
) -> str | None:
    """When the background sync will run next, or None while it is not running."""
    state = get_state(store, module.id)
    if not state.get("connected") or state.get("blocked"):
        return None
    last = parse_iso(state.get("last_attempt_at"))
    if last is None:
        return None
    due_at = last + timedelta(minutes=pace_minutes(store, module, carrier_settings))
    return due_at.isoformat(timespec="seconds")


def due(
    store: Store, module: ImporterModule, carrier_settings: dict[str, Any], early: bool = False
) -> bool:
    """Whether the account is synced now; ``early`` does not wait for its pace."""
    state = get_state(store, module.id)
    if not switched_on(carrier_settings):
        return False
    # Only an account whose sign-in was checked is synced on its own.
    if not state.get("connected") or not module.is_configured(carrier_settings):
        return False
    if state.get("blocked"):
        return False  # wait until the credentials change or the user asks
    last = parse_iso(state.get("last_attempt_at"))
    interval = (
        MIN_MANUAL_GAP
        if early
        else timedelta(minutes=pace_minutes(store, module, carrier_settings))
    )
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


async def fetch_one(
    store: Store, registry: Registry, module: ImporterModule, data_dir: Path, reference: str
) -> int:
    """Import a single order the user asked for by its reference.

    Returns the id of its shipment; raises CarrierError if it cannot be read.
    """
    settings = store.get_settings()
    own = settings["carriers"].get(module.id, {})
    if not get_state(store, module.id).get("connected") or not module.is_configured(own):
        raise CarrierError("not_connected")
    lock = _locks.setdefault(module.id, asyncio.Lock())
    async with lock:
        request = SyncRequest(own, settings["language"], data_dir / "modules" / module.id)
        try:
            found = await asyncio.to_thread(module.fetch, request, reference)
        except CarrierError:
            raise
        except Exception as error:
            log.exception("importer %s failed", module.id)
            raise CarrierError("module_error", describe(error)) from error
        dismissed = store.get_value(DISMISSED_KEY) or []
        if [module.name, found.reference] in dismissed:
            dismissed.remove([module.name, found.reference])
            store.set_value(DISMISSED_KEY, dismissed)
        apply(store, registry, module, [found], wanted=True)
        filed = next(iter(store.find_order(module.name, found.reference)), None)
        # A shared link for a parcel of an order we already have leads to that order.
        filed = filed or _same_parcel_elsewhere(store, module, found)
        if filed is None:
            raise CarrierError("not_found")
        return filed["id"]


async def follow_deliveries(
    store: Store, registry: Registry, module: ImporterModule, data_dir: Path
) -> list[Notice]:
    """Reads again the orders whose parcel the service itself brings right now.

    Around the delivery window the half-hourly sync is too coarse. Only the
    one order is read, not the whole account, and at most every few minutes.
    """
    settings = store.get_settings()
    own = settings["carriers"].get(module.id, {})
    state = get_state(store, module.id)
    if not state.get("connected") or state.get("blocked") or not module.is_configured(own):
        return []
    if not switched_on(own):
        return []
    now = datetime.now(UTC)
    references: list[str] = []
    for parcel in store.open_parcels():
        if parcel["carrier"] != module.id or parcel["status"] != Status.OUT_FOR_DELIVERY:
            continue
        start, end = parse_iso(parcel["eta_from"]), parse_iso(parcel["eta_to"])
        if not start or not end or end - start > timedelta(hours=12):
            continue  # a day without hours: the usual sync will do
        if not start - timedelta(minutes=30) <= now <= end + timedelta(hours=2):
            continue
        reference = order_of(
            (store.get_shipment(parcel["shipment_id"]) or {}).get("reference") or ""
        )
        if parcel["extra"].get("import_key", "").startswith(SHARED):
            reference = parcel["extra"].get("url")  # a shared delivery is read by its link
        last = _followed.get((module.id, reference or ""))
        # Once the service counts the stops, the van is near: look twice as often.
        stops = parcel["extra"].get("stops_left", "")
        pause = FOLLOW_NEAR_MINUTES if stops else FOLLOW_MINUTES
        if stops.isdigit() and int(stops) < CLOSE_STOPS:
            pause = FOLLOW_CLOSE_MINUTES
        if (
            reference
            and reference not in references
            and (last is None or monotonic() - last >= pause * 60)
        ):
            references.append(reference)
    notices: list[Notice] = []
    lock = _locks.setdefault(module.id, asyncio.Lock())
    for reference in references:
        if lock.locked():
            break  # a sync is running and brings the same news
        _followed[(module.id, reference)] = monotonic()
        async with lock:
            request = SyncRequest(own, settings["language"], data_dir / "modules" / module.id)
            try:
                found = await asyncio.to_thread(module.fetch, request, reference)
            except CarrierError as error:
                log.warning("%s %s: following the delivery failed: %s", module.id, reference, error)
                continue
            except Exception:
                log.exception("importer %s failed", module.id)
                continue
            # Not "wanted": a look in the background updates what is open and leaves
            # the bin, the archive and what was deleted for good as the user left them.
            notices += apply(store, registry, module, [found])
            log.info("%s %s: followed on its delivery day", module.id, reference)
    return notices


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
        request = SyncRequest(
            own,
            settings["language"],
            data_dir / "modules" / module.id,
            settled_keys(store),
            shared_links(store, module),
        )
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
