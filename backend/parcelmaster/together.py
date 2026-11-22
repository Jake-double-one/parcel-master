"""Orders that travel in one box show as one card.

A shop may pack several orders into a single parcel. Each order stays its own
shipment in the database; only the overview puts them together. One shipment
leads (the one whose parcel a carrier tracks), and each other order joins it
when all of its parcels are in that box. An order with a parcel of its own
keeps its own card.
"""

from __future__ import annotations

from typing import Any

from .carriers.registry import Registry


def _numbers(shipment: dict[str, Any]) -> set[str]:
    # Placeholders like "<order>/1" stand for a parcel without a number yet.
    return {p["tracking_number"] for p in shipment["parcels"] if "/" not in p["tracking_number"]}


def _tracked(shipment: dict[str, Any], registry: Registry) -> bool:
    """Whether a carrier (not the shop) follows one of its parcels."""
    for parcel in shipment["parcels"]:
        module = registry.get(parcel["carrier"])
        if module is not None and module.kind != "importer":
            return True
    return False


def groups(shipments: list[dict[str, Any]], registry: Registry) -> dict[int, list[int]]:
    """Map each leading shipment's id to the ids of the orders that joined it."""
    by_number: dict[str, list[dict[str, Any]]] = {}
    for shipment in shipments:
        for number in _numbers(shipment):
            by_number.setdefault(number, []).append(shipment)
    joined: dict[int, list[int]] = {}
    taken: set[int] = set()
    # Leaders first: a tracked parcel, then the oldest order.
    ordered = sorted(shipments, key=lambda s: (not _tracked(s, registry), s["id"]))
    for leader in ordered:
        if leader["id"] in taken:
            continue
        numbers = _numbers(leader)
        if not numbers:
            continue
        for number in sorted(numbers):
            for other in by_number[number]:
                if other["id"] == leader["id"] or other["id"] in taken or other["id"] in joined:
                    continue
                if _numbers(other) and _numbers(other) <= numbers:
                    joined.setdefault(leader["id"], []).append(other["id"])
                    taken.add(other["id"])
    return {leader: sorted(ids) for leader, ids in joined.items()}


def companions(shipments: list[dict[str, Any]], registry: Registry, shipment_id: int) -> list[int]:
    """The shipment and every order shown on the same card with it."""
    for leader, ids in groups(shipments, registry).items():
        if shipment_id == leader or shipment_id in ids:
            return [leader, *ids]
    return [shipment_id]
