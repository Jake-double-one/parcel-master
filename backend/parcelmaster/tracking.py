"""Looks a parcel up at its carrier and records what changed."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .carriers.base import (
    CarrierError,
    CarrierModule,
    TrackingResult,
    TrackRequest,
    switched_on,
)
from .carriers.registry import Registry
from .db import Store
from .status import FINAL, Status

log = logging.getLogger(__name__)

MIN_INTERVAL_MINUTES = 15
PICKUP_REMINDER_DAYS = 2
AUTO_ARCHIVED_KEY = "auto_archived"
#: With the saver on: minutes between lookups of a parcel that is only announced,
#: and of one whose delivery is still days away.
ANNOUNCED_MINUTES = 6 * 60
FAR_AWAY_MINUTES = 12 * 60
#: On the day a parcel is expected, it is asked about at least this often, so
#: "out for delivery" shows up soon after the van is loaded.
DELIVERY_DAY_MINUTES = 30
#: A parcel is not asked about twice within this time, whoever wants it.
MIN_GAP_SECONDS = 10
#: The shortest wait a module may ask for between two lookups of one parcel.
MIN_LIVE_MINUTES = 1
#: Stops left at which a message goes out, each once on the way down.
STOP_MARKS = (20, 10, 5, 2, 1, 0)
#: How long live news of a delivery round stand against an ordinary answer.
LIVE_STAYS = timedelta(minutes=10)
#: A delivery window is reported again once its start or end has moved this far.
WINDOW_NEWS = timedelta(minutes=15)
#: Tries per carrier within any hour, failed ones included.
TRIES_PER_HOUR = 100
#: Minutes to wait after the first, second, ... failure in a row; then the usual interval.
RETRY_MINUTES = (5, 15, 30, 60)
#: "Next check" of a parcel that is never looked up on its own.
NEVER = "9999-12-31T00:00:00+00:00"


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


def in_window(window: dict[str, Any], zone: ZoneInfo, now: datetime | None = None) -> bool:
    """Whether the local time lies in a daily window such as 22:00 to 06:00."""
    if not window.get("enabled"):
        return False
    try:
        start = time.fromisoformat(window["from"])
        end = time.fromisoformat(window["to"])
    except (KeyError, ValueError):
        return False
    current = (now or datetime.now(zone)).time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end  # the window crosses midnight


def saver_on(settings: dict[str, Any]) -> bool:
    return bool(settings["polling"].get("saver", {}).get("enabled"))


def resting(settings: dict[str, Any], now: datetime | None = None) -> bool:
    """Whether the saver's night rest applies right now: nothing is asked on its own."""
    return in_window(settings["polling"].get("saver", {}), timezone(settings), now)


def next_delay_minutes(
    status: Status,
    base: int,
    last_event_at: datetime | None,
    eta_from: datetime | None = None,
    saver: bool = False,
    zone: ZoneInfo | None = None,
) -> int | None:
    """How long to wait before asking again; None once nothing more can happen."""
    base = max(MIN_INTERVAL_MINUTES, base)
    now = datetime.now(UTC)
    if status in FINAL:
        return None
    if status is Status.OUT_FOR_DELIVERY:
        return max(MIN_INTERVAL_MINUTES, base // 4)
    if status is Status.READY_FOR_PICKUP:
        return base * 3
    local = zone or ZoneInfo("UTC")
    if eta_from and eta_from.astimezone(local).date() == now.astimezone(local).date():
        # The expected day: the van may be loaded any time now.
        return min(base, DELIVERY_DAY_MINUTES)
    if saver and status is Status.REGISTERED:
        return max(base, ANNOUNCED_MINUTES)  # only announced: little happens for a while
    if saver and eta_from and (eta_from - now) > timedelta(days=1, hours=12):
        return max(base, FAR_AWAY_MINUTES)  # days away: twice a day shows the movement
    if last_event_at and now - last_event_at > timedelta(days=3):
        return base * 2
    return base


class Tries:
    """Counts lookups per carrier within the last hour; forgotten on restart."""

    def __init__(self) -> None:
        self._times: dict[str, deque[float]] = {}

    def _recent(self, carrier: str) -> deque[float]:
        times = self._times.setdefault(carrier, deque())
        while times and monotonic() - times[0] >= 3600:
            times.popleft()
        return times

    def count(self, carrier: str) -> None:
        self._recent(carrier).append(monotonic())

    def wait_minutes(self, carrier: str) -> float:
        """0 if another try is fine, else the minutes until the oldest one is an hour old."""
        times = self._recent(carrier)
        if len(times) < TRIES_PER_HOUR:
            return 0
        return max(1.0, (3600 - (monotonic() - times[0])) / 60)

    def clear(self) -> None:
        self._times.clear()


TRIES = Tries()


def daily_budget(module: CarrierModule, carrier_settings: dict[str, Any]) -> int | None:
    value = carrier_settings.get("daily_budget")
    try:
        return int(value) if value not in (None, "") else module.budget(carrier_settings)
    except (TypeError, ValueError):
        return module.budget(carrier_settings)


def pickup_deadline(parcel: dict[str, Any], module: CarrierModule | None) -> datetime | None:
    ready = parse_iso(parcel.get("pickup_ready_at"))
    if not ready or not module or not module.pickup_hold_days:
        return None
    if parcel["status"] != Status.READY_FOR_PICKUP:
        return None
    return ready + timedelta(days=module.pickup_hold_days)


def record_result(
    store: Store,
    parcel: dict[str, Any],
    result: TrackingResult,
    base: int,
    zone: ZoneInfo,
    now: datetime,
    poll: bool = True,
) -> list[Notice]:
    """Store what a carrier (or an importing service) reported about a parcel.

    With ``poll=False`` the parcel is not scheduled for lookups of its own:
    its status arrives with the next sync of the service that imported it.
    """
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
    eta_from_iso = to_iso(result.eta_from, zone)
    eta_to_iso = to_iso(result.eta_to, zone)
    # Live news from the delivery round outrank the plan for a while: the next
    # ordinary answer knows only the planned window and nothing about stops.
    seen_live = parse_iso(parcel["extra"].get("live_at"))
    keep_live = (
        result.stops_left is None
        and status is Status.OUT_FOR_DELIVERY
        and seen_live is not None
        and now - seen_live < LIVE_STAYS
    )
    if keep_live:
        eta_from_iso, eta_to_iso = parcel["eta_from"], parcel["eta_to"]
    delay = next_delay_minutes(
        status,
        base,
        parse_iso(last_event_at),
        parse_iso(eta_from_iso),
        saver_on(store.get_settings()),
        zone,
    )
    if result.again_in_minutes and delay:
        delay = min(delay, max(MIN_LIVE_MINUTES, result.again_in_minutes))
    store.update_parcel(
        parcel["id"],
        status=str(status),
        description=result.description,
        location=result.location,
        eta_from=eta_from_iso,
        eta_to=eta_to_iso,
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
            (now + timedelta(minutes=delay)).isoformat(timespec="seconds")
            if delay and poll
            else None
            if poll
            else NEVER
        ),
    )
    store.replace_events(parcel["id"], events)

    # Stops left on the delivery round: kept with the parcel while it is on the van.
    extra = dict(parcel["extra"])
    before = extra.get("stops_left")
    stops = result.stops_left if status is Status.OUT_FOR_DELIVERY else None
    if not keep_live:
        for note in ("stops_left", "stops_about", "live_at"):
            extra.pop(note, None)
    if status is not Status.OUT_FOR_DELIVERY:
        extra.pop("live_lost", None)
    if stops is not None:
        extra["stops_left"] = str(stops)
        extra["live_at"] = now_iso
        if result.stops_about:
            extra["stops_about"] = "1"
    if extra != parcel["extra"]:
        store.set_parcel_extra(parcel["id"], extra)

    updated = store.get_parcel(parcel["id"])
    notices: list[Notice] = []
    if stops is not None:
        earlier = int(before) if before and before.isdigit() else None
        if any(stops <= mark and (earlier is None or earlier > mark) for mark in STOP_MARKS):
            variant = stops_variant(stops, result.stops_about)
            notices.append(Notice("stops", updated, {"count": stops, "variant": variant}))
    # A delivery window is news whenever the carrier names a new one, unless the
    # user is looking at a parcel they added moments ago.
    window = delivery_window(eta_from_iso, eta_to_iso, zone, now)
    just_added = now - (parse_iso(parcel["created_at"]) or now) < timedelta(minutes=5)
    if (
        window
        and status not in FINAL
        and (eta_from_iso, eta_to_iso) != (parcel["eta_from"], parcel["eta_to"])
        and not just_added
        and window_moved(updated["extra"].get("window_told"), eta_from_iso, eta_to_iso)
    ):
        # Remember what was said: a window drifting by minutes is not news each time.
        store.set_parcel_extra(
            parcel["id"], {**updated["extra"], "window_told": f"{eta_from_iso}|{eta_to_iso}"}
        )
        updated = store.get_parcel(parcel["id"])
        notices.append(Notice("delivery_window", updated, {"window": window}))
    # The first answer for a parcel is not news: the user just added it.
    if parcel["last_success_at"] is not None and status != old_status:
        notices.append(Notice(str(status), updated, {"description": result.description}))
    return notices


def stops_variant(stops: int, about: bool) -> str:
    """Which sentence fits a number of stops: "you are next", "one", "a bit more than", "N"."""
    if stops <= 0:
        return "next"
    return "one" if stops == 1 else "about" if about else "body"


def record_live(
    store: Store, parcel: dict[str, Any], result: TrackingResult, zone: ZoneInfo, now: datetime
) -> list[Notice]:
    """Notes what is known live about a delivery round and nothing else.

    For news that carry no status and no history, such as what a companion
    script reads off a map: the stops left and, if named, the window.
    """
    if Status(parcel["status"]) in FINAL:
        return []
    changes: dict[str, Any] = {}
    if result.eta_from and result.eta_to:
        changes = {"eta_from": to_iso(result.eta_from, zone), "eta_to": to_iso(result.eta_to, zone)}
    extra = dict(parcel["extra"])
    before = extra.get("stops_left")
    stops = result.stops_left
    if stops is not None or changes:
        extra["live_at"] = now.isoformat(timespec="seconds")
    lost = result.live_lost and before is not None
    if stops is not None:
        extra["stops_left"] = str(stops)
        extra.pop("stops_about", None)
        extra.pop("live_lost", None)
        if result.stops_about:
            extra["stops_about"] = "1"
    elif lost:
        # A moment ago the van was so many stops away, now the view is gone:
        # the number no longer holds, and that is news of its own.
        extra.pop("stops_left", None)
        extra.pop("stops_about", None)
        extra["live_lost"] = "1"
    if changes:
        store.update_parcel(parcel["id"], **changes)
    if extra != parcel["extra"]:
        store.set_parcel_extra(parcel["id"], extra)
    updated = store.get_parcel(parcel["id"])
    if lost:
        return [Notice("stops", updated, {"count": int(before), "variant": "lost"})]
    if stops is None:
        return []
    earlier = int(before) if before and before.isdigit() else None
    if not any(stops <= mark and (earlier is None or earlier > mark) for mark in STOP_MARKS):
        return []
    variant = stops_variant(stops, result.stops_about)
    return [Notice("stops", updated, {"count": stops, "variant": variant})]


def window_moved(told: str | None, eta_from: str | None, eta_to: str | None) -> bool:
    """Whether a delivery window differs enough from the one last reported to say so again."""
    old_from, _, old_to = (told or "").partition("|")
    before = (parse_iso(old_from), parse_iso(old_to))
    after = (parse_iso(eta_from), parse_iso(eta_to))
    if not all(before) or not all(after):
        return True
    return any(abs(new - old) >= WINDOW_NEWS for old, new in zip(before, after, strict=True))


def delivery_window(
    eta_from: str | None, eta_to: str | None, zone: ZoneInfo, now: datetime
) -> str | None:
    """A window like "today 11:30–13:30" if the carrier names a time of day, else None."""
    start, end = parse_iso(eta_from), parse_iso(eta_to)
    if not start or not end or end <= start:
        return None
    start, end = start.astimezone(zone), end.astimezone(zone)
    if end - start > timedelta(hours=12):
        return None  # a whole day is a date, not a window
    day = "" if start.date() == now.astimezone(zone).date() else f"{start:%d.%m.} "
    return f"{day}{start:%H:%M}–{end:%H:%M}"


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

    asked = parse_iso(parcel["last_checked_at"])
    if asked and timedelta(0) <= now - asked < timedelta(seconds=MIN_GAP_SECONDS):
        return []  # just asked; nothing can have changed, and the carrier is spared

    module = registry.get(parcel["carrier"])
    if module is None:
        return fail("unknown_carrier", parcel["carrier"], 24 * 60, count_error=False)
    if not switched_on(settings["carriers"].get(module.id, {})):
        # The user turned this carrier off: nothing is asked, and the parcel says why.
        return fail("carrier_off", "", 60, count_error=False)
    if module.kind == "importer":
        # Its status comes with the service's sync, not from a lookup by number.
        store.update_parcel(parcel_id, next_check_at=NEVER)
        return []
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

    # Failed tries do not use the budget up, so they get a ceiling of their own:
    # whatever goes wrong, a carrier is never asked more than this often an hour.
    wait = TRIES.wait_minutes(module.id)
    if wait:
        return fail("too_many_tries", str(TRIES_PER_HOUR), wait, count_error=False)
    TRIES.count(module.id)

    request = TrackRequest(
        tracking_number=parcel["tracking_number"],
        extra=parcel["extra"],
        settings=carrier_settings,
        language=settings["language"],
        created_at=parse_iso(parcel["created_at"]) or now,
    )
    started = monotonic()

    def took() -> str:
        return f"{monotonic() - started:.1f}s"

    try:
        result = await module.track(request, http)
    except CarrierError as error:
        log.warning(
            "%s %s: %s%s after %s",
            module.id,
            request.tracking_number,
            error.code,
            f" ({error.detail})" if error.detail else "",
            took(),
        )
        if error.code == "unauthorized":
            retry = 6 * 60
        elif error.code == "rate_limited":
            retry = 60
        elif error.code == "not_found":
            # Freshly printed labels are unknown to the carrier for a while.
            retry = base
        else:
            # Most failures pass quickly, so try again soon and then ever more
            # patiently, but never rarer than a parcel is checked anyway.
            failures = parcel["error_count"]
            retry = RETRY_MINUTES[failures] if failures < len(RETRY_MINUTES) else max(base, 60)
            retry = min(retry, max(base, 60))
        return fail(error.code, error.detail, retry)
    except Exception as error:  # a broken module must not stop the scheduler
        log.exception("carrier module %s failed", module.id)
        return fail("module_error", type(error).__name__, base)

    # Only answers count against the day's budget; a failed try is retried later anyway.
    store.count_usage(module.id)
    log.info(
        "%s %s: %s, %d events, after %s",
        module.id,
        request.tracking_number,
        result.status.value,
        len(result.events),
        took(),
    )
    return record_result(store, parcel, result, base, zone, now)


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
    if ids:
        # Remembered until the user next looks, so the tidy-up does not go unnoticed.
        pending = store.get_value(AUTO_ARCHIVED_KEY) or []
        store.set_value(AUTO_ARCHIVED_KEY, [*pending, *ids][-200:])
    return len(ids)


def take_auto_archived(store: Store) -> list[int]:
    """Shipments archived automatically since this was last asked; asking clears the list."""
    ids = store.get_value(AUTO_ARCHIVED_KEY) or []
    if ids:
        store.set_value(AUTO_ARCHIVED_KEY, [])
    return ids
