"""The carrier-independent status model every module maps onto."""

from enum import StrEnum


class Status(StrEnum):
    UNKNOWN = "unknown"
    REGISTERED = "registered"  # announced to the carrier, not yet handed over
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    READY_FOR_PICKUP = "ready_for_pickup"  # parcel locker, shop, neighbour
    DELIVERED = "delivered"
    PROBLEM = "problem"
    RETURNED = "returned"


#: Nothing more will happen to a parcel in one of these states.
FINAL = frozenset({Status.DELIVERED, Status.RETURNED})

_ATTENTION_ORDER = (
    Status.PROBLEM,
    Status.READY_FOR_PICKUP,
    Status.OUT_FOR_DELIVERY,
    Status.IN_TRANSIT,
    Status.REGISTERED,
    Status.UNKNOWN,
)


def aggregate(statuses: list[Status]) -> Status:
    """Status of a shipment made of several parcels.

    While parcels are still open, the one that needs attention most wins.
    """
    if not statuses:
        return Status.UNKNOWN
    open_statuses = [s for s in statuses if s not in FINAL]
    if not open_statuses:
        return Status.DELIVERED if Status.DELIVERED in statuses else Status.RETURNED
    for candidate in _ATTENTION_ORDER:
        if candidate in open_statuses:
            return candidate
    return Status.UNKNOWN
