"""Looks a parcel up at its carrier and records what changed."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .carriers.base import CarrierError, CarrierModule, TrackRequest
from .carriers.registry import Registry
from .db import Store
from .status import FINAL, Status

log = logging.getLogger(__name__)

MIN_INTERVAL_MINUTES = 15
PICKUP_REMINDER_DAYS = 2


@dataclass
class Notice:
    """Something worth telling the user about."""

    kind: str  # a Status value, "stale" or "pickup_reminder"
    parcel: dict[str, Any]
    params: dict[str, Any]


def timezone(settings: dict[str, Any]) -> ZoneInfo:
    try:
        return ZoneInfo(str(settings.get("timezone") or "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def to_iso(value: datetime | None, zone: ZoneInfo) -> str | None:
    """Store every time in UTC. Carriers that omit the zone mean local time."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=zone)
    return value.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def next_delay_minutes(status: Status, base: int, last_event_at: datetime | None) -> int | None:
    """How long to wait before asking again; None once nothing more can happen."""
    base = max(MIN_INTERVAL_MINUTES, base)
    if status in FINAL:
        return None
    if status is Status.OUT_FOR_DELIVERY:
        return max(MIN_INTERVAL_MINUTES, base // 4)
    if status is Status.READY_FOR_PICKUP:
        return base * 3
    if last_event_at and datetime.now(UTC) - last_event_at > timedelta(days=3):
        return base * 2
    return base


def daily_budget(module: CarrierModule, carrier_settings: dict[str, Any]) -> int | None:
    value = carrier_settings.get("daily_budget")
    try:
        return int(value) if value not in (None, "") else module.default_daily_budget
    except (TypeError, ValueError):
        return module.default_daily_budget


def pickup_deadline(parcel: dict[str, Any], module: CarrierModule | None) -> datetime | None:
    ready = parse_iso(parcel.get("pickup_ready_at"))
    if not ready or not module or not module.pickup_hold_days:
        return None
    if parcel["status"] != Status.READY_FOR_PICKUP:
        return None
    return ready + timedelta(days=module.pickup_hold_days)


async def refresh_parcel(
    store: Store, registry: Registry, http: httpx.AsyncClient, parcel_id: int
) -> list[Notice]:
    parcel = store.get_parcel(parcel_id)
    if parcel is None:
        return []
    now = datetime.now(UTC)
    settings = store.get_settings()
    zone = timezone(settings)
    base = int(settings["polling"]["interval_minutes"])

    def fail(
        code: str, detail: str, retry_minutes: float, count_error: bool = True
    ) -> list[Notice]:
        store.update_parcel(
            parcel_id,
            last_checked_at=now.isoformat(timespec="seconds"),
            last_error={"code": code, "detail": detail},
            error_count=parcel["error_count"] + (1 if count_error else 0),
            next_check_at=(now + timedelta(minutes=retry_minutes)).isoformat(timespec="seconds"),
        )
        return []

    module = registry.get(parcel["carrier"])
    if module is None:
        return fail("unknown_carrier", parcel["carrier"], 24 * 60, count_error=False)
    carrier_settings = settings["carriers"].get(module.id, {})
    if not module.is_configured(carrier_settings):
        return fail("not_configured", "", 60, count_error=False)
    budget = daily_budget(module, carrier_settings)
    if budget is not None and store.usage_today(module.id) >= budget:
        tomorrow = (now.astimezone(zone) + timedelta(days=1)).replace(
            hour=0, minute=5, second=0, microsecond=0
        )
        minutes = (tomorrow - now.astimezone(zone)).total_seconds() / 60
        return fail("budget_exhausted", str(budget), minutes, count_error=False)

    store.count_usage(module.id)
    request = TrackRequest(
        tracking_number=parcel["tracking_number"],
        extra=parcel["extra"],
        settings=carrier_settings,
        language=settings["language"],
        created_at=parse_iso(parcel["created_at"]) or now,
    )
    try:
        result = await module.track(request, http)
    except CarrierError as error:
        if error.code == "unauthorized":
            retry = 6 * 60
        elif error.code == "rate_limited":
            retry = 60
        elif error.code == "not_found":
            # Freshly printed labels are unknown to the carrier for a while.
            retry = base
        else:
            retry = min(24 * 60, base * 2 ** min(parcel["error_count"], 4))
        return fail(error.code, error.detail, retry)
    except Exception as error:  # a broken module must not stop the scheduler
        log.exception("carrier module %s failed", module.id)
        return fail("module_error", type(error).__name__, base)

    events = sorted(
        (
            {
                "timestamp": to_iso(event.timestamp, zone),
                "status": str(event.status),
                "description": event.description,
                "location": event.location,
            }
            for event in result.events
        ),
        key=lambda event: event["timestamp"] or "",
    )
    timestamps = [event["timestamp"] for event in events if event["timestamp"]]
    last_event_at = max(timestamps) if timestamps else parcel["last_event_at"]
    now_iso = now.isoformat(timespec="seconds")

    def first_time(status: Status) -> str | None:
        matching = [e["timestamp"] for e in events if e["status"] == status and e["timestamp"]]
        return min(matching) if matching else None

    old_status = Status(parcel["status"])
    status = result.status
    delay = next_delay_minutes(status, base, parse_iso(last_event_at))
    store.update_parcel(
        parcel_id,
        status=str(status),
        description=result.description,
        location=result.location,
        eta_from=to_iso(result.eta_from, zone),
        eta_to=to_iso(result.eta_to, zone),
        last_event_at=last_event_at,
        delivered_at=(
            (parcel["delivered_at"] or first_time(status) or now_iso) if status in FINAL else None
        ),
        pickup_ready_at=(
            (parcel["pickup_ready_at"] or first_time(status) or now_iso)
            if status is Status.READY_FOR_PICKUP
            else None
        ),
        last_checked_at=now_iso,
        last_success_at=now_iso,
        last_error=None,
        error_count=0,
        next_check_at=(
            (now + timedelta(minutes=delay)).isoformat(timespec="seconds") if delay else None
        ),
    )
    store.replace_events(parcel_id, events)

    # The first answer for a parcel is not news: the user just added it.
    if parcel["last_success_at"] is None or status == old_status:
        return []
    updated = store.get_parcel(parcel_id)
    return [Notice(str(status), updated, {"description": result.description})]


def periodic_notices(store: Store, registry: Registry) -> list[Notice]:
    """Reminders that are due because time passed, not because a carrier answered."""
    settings = store.get_settings()
    now = datetime.now(UTC)
    now_iso = now.isoformat(timespec="seconds")
    stale_after = timedelta(days=int(settings["polling"]["stale_days"]))
    notices: list[Notice] = []
    for parcel in store.open_parcels():
        status = Status(parcel["status"])
        last_event = parse_iso(parcel["last_event_at"])
        if (
            status in (Status.IN_TRANSIT, Status.OUT_FOR_DELIVERY, Status.PROBLEM)
            and last_event
            and now - last_event > stale_after
            and (parcel["stale_notified_at"] or "") < parcel["last_event_at"]
        ):
            store.update_parcel(parcel["id"], stale_notified_at=now_iso)
            notices.append(Notice("stale", parcel, {"days": (now - last_event).days}))
        deadline = pickup_deadline(parcel, registry.get(parcel["carrier"]))
        if (
            deadline
            and not parcel["pickup_reminded_at"]
            and deadline - now <= timedelta(days=PICKUP_REMINDER_DAYS)
        ):
            store.update_parcel(parcel["id"], pickup_reminded_at=now_iso)
            notices.append(
                Notice("pickup_reminder", parcel, {"days": max(0, (deadline - now).days)})
            )
    return notices


def auto_archive(store: Store) -> int:
    days = int(store.get_settings()["polling"]["auto_archive_days"])
    if days <= 0:
        return 0
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    ids = store.shipments_to_archive(cutoff)
    for shipment_id in ids:
        store.set_archived(shipment_id, True)
    return len(ids)
