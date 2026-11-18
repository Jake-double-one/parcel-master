"""Parcels a companion script saw on a carrier's page and we do not track yet,
and the bin that deleted and discarded shipments rest in before they go."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from . import importer
from .carriers.base import CarrierModule, Found
from .db import Store, now_iso
from .tracking import Notice

journal = logging.getLogger(__name__)

SOURCE = importer.DISCOVERED
MODES = ("button", "suggest", "auto")
TRASH_DAYS = 14
FORGOTTEN_KEY = "discovery_forgotten"
FORGET_DAYS = 180
MAX_FORGOTTEN = 1000
MAX_AT_ONCE = 30


def mode_of(settings: dict[str, Any]) -> str:
    mode = (settings.get("discovery") or {}).get("mode")
    return mode if mode in MODES else "suggest"


def _forgotten(store: Store) -> dict[str, str]:
    """Numbers the user threw away for good; they are not found again for a while."""
    cutoff = (datetime.now(UTC) - timedelta(days=FORGET_DAYS)).isoformat(timespec="seconds")
    kept = store.get_value(FORGOTTEN_KEY) or {}
    return {number: at for number, at in kept.items() if at > cutoff}


def remove_for_good(store: Store, shipment: dict[str, Any]) -> None:
    """Delete a shipment and see to it that nothing brings it back by itself."""
    forgotten = _forgotten(store)
    for parcel in shipment.get("parcels", []):
        forgotten[parcel["tracking_number"]] = now_iso()
    store.set_value(FORGOTTEN_KEY, dict(list(forgotten.items())[-MAX_FORGOTTEN:]))
    if shipment["reference"] and shipment["source"] not in ("manual", "import", "demo", SOURCE):
        importer.dismiss(store, shipment["source"], shipment["reference"])
    store.delete_shipment(shipment["id"])


def empty_trash(store: Store, everything: bool = False) -> int:
    """Removes what has lain in the bin long enough, or all of it."""
    if everything:
        ids = [shipment["id"] for shipment in store.list_trash()]
    else:
        cutoff = datetime.now(UTC) - timedelta(days=TRASH_DAYS)
        ids = store.trash_older_than(cutoff.isoformat(timespec="seconds"))
    for shipment_id in ids:
        shipment = store.get_shipment(shipment_id)
        if shipment:
            remove_for_good(store, shipment)
    return len(ids)


def purge_at(shipment: dict[str, Any]) -> str | None:
    if not shipment.get("deleted_at"):
        return None
    gone = datetime.fromisoformat(shipment["deleted_at"]) + timedelta(days=TRASH_DAYS)
    return gone.isoformat(timespec="seconds")


def take(
    store: Store,
    module: CarrierModule,
    found: list[Found],
    mode: str,
    wanted: frozenset[str] = frozenset(),
) -> list[Notice]:
    """Files what the page showed. ``wanted`` are numbers the user clicked on there:
    those are tracked whatever the mode and whatever became of them here before."""
    notices: list[Notice] = []
    forgotten = _forgotten(store)
    for item in found[:MAX_AT_ONCE]:
        asked = item.number in wanted
        if not asked and (mode == "button" or not item.full or item.settled):
            continue
        existing = store.find_number(item.number)
        if existing is not None:
            if asked:
                if existing["deleted_at"]:
                    store.set_deleted(existing["id"], False)
                if existing["archived_at"]:
                    store.set_archived(existing["id"], False)
                store.accept_suggestion(existing["id"])
            continue
        if not asked and item.number in forgotten:
            continue
        suggested = not asked and mode == "suggest"
        shipment_id = store.save_shipment(
            None,
            {
                "title": item.title[:200],
                "direction": item.direction,
                "note": "",
                "source": SOURCE,
                "reference": "",
                "suggested": suggested,
            },
            [{"carrier": module.id, "tracking_number": item.number, "extra": {}}],
        )
        journal.info(
            "%s: found %s, %s", module.id, item.number, "suggested" if suggested else "tracked"
        )
        if not asked:
            parcel = store.get_shipment(shipment_id)["parcels"][0]
            notices.append(
                Notice("discovered", parcel, {"variant": "body" if suggested else "taken"})
            )
    return notices
