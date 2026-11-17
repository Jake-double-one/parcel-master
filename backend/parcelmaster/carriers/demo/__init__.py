"""A made-up carrier that needs no key and no network.

It drives the demo mode and the test-suite. The last digit of a tracking
number picks the story the parcel tells:

    1 registered      4 ready for pickup   7 stuck in transit
    2 in transit      5 delivered          other: advances one step
    3 out for delivery 6 problem                  per minute, live
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import httpx

from parcelmaster.carriers.base import CarrierModule, TrackingEvent, TrackingResult, TrackRequest
from parcelmaster.status import Status

_STEPS: tuple[tuple[Status, str, str], ...] = (
    (Status.REGISTERED, "registered", "Online"),
    (Status.IN_TRANSIT, "picked_up", "Hamburg"),
    (Status.IN_TRANSIT, "sorting", "Hannover"),
    (Status.IN_TRANSIT, "arrived", "Köln"),
    (Status.OUT_FOR_DELIVERY, "out_for_delivery", "Köln"),
    (Status.DELIVERED, "delivered", "Köln"),
)

_TEXT = {
    "en": {
        "registered": "The sender announced the parcel.",
        "picked_up": "The parcel was handed over to the carrier.",
        "sorting": "The parcel was processed at the sorting centre.",
        "arrived": "The parcel arrived in the destination region.",
        "out_for_delivery": "The parcel is on the delivery vehicle.",
        "delivered": "The parcel was delivered.",
        "pickup": "The parcel is ready for pickup at the parcel locker.",
        "problem": "The recipient could not be found. The address is being checked.",
    },
    "de": {
        "registered": "Der Absender hat das Paket angekündigt.",
        "picked_up": "Das Paket wurde dem Paketdienst übergeben.",
        "sorting": "Das Paket wurde im Paketzentrum bearbeitet.",
        "arrived": "Das Paket ist in der Zielregion angekommen.",
        "out_for_delivery": "Das Paket ist im Zustellfahrzeug.",
        "delivered": "Das Paket wurde zugestellt.",
        "pickup": "Das Paket liegt in der Packstation zur Abholung bereit.",
        "problem": "Der Empfänger wurde nicht gefunden. Die Adresse wird geprüft.",
    },
}


class DemoCarrier(CarrierModule):
    id = "demo"
    country = ""
    name = "Demo Express"
    patterns = (re.compile(r"DEMO\d{4,}"),)
    pickup_hold_days = 7

    def tracking_url(self, tracking_number: str, extra: dict[str, str], language: str) -> str:
        return f"https://example.com/track/{tracking_number}"

    async def track(self, request: TrackRequest, http: httpx.AsyncClient) -> TrackingResult:
        return self.simulate(request, datetime.now().astimezone())

    def simulate(self, request: TrackRequest, now: datetime) -> TrackingResult:
        text = _TEXT.get(request.language, _TEXT["en"])
        scenario = request.tracking_number[-1]
        hour = timedelta(hours=1)

        def history(count: int, newest: datetime, spacing: timedelta) -> list[TrackingEvent]:
            return [
                TrackingEvent(newest - spacing * (count - 1 - i), status, text[key], location)
                for i, (status, key, location) in enumerate(_STEPS[:count])
            ]

        eta = (now + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
        if scenario == "1":
            events = history(1, now - 5 * hour, hour)
        elif scenario == "2":
            events = history(3, now - 3 * hour, 7 * hour)
        elif scenario == "3":
            events = history(5, now - hour, 6 * hour)
            eta = now.replace(hour=18, minute=0, second=0, microsecond=0)
        elif scenario == "4":
            events = history(4, now - 30 * hour, 6 * hour)
            events.append(
                TrackingEvent(now - 26 * hour, Status.READY_FOR_PICKUP, text["pickup"], "Köln")
            )
        elif scenario == "5":
            events = history(6, now - 20 * hour, 6 * hour)
        elif scenario == "6":
            events = history(4, now - 9 * hour, 6 * hour)
            events.append(TrackingEvent(now - 2 * hour, Status.PROBLEM, text["problem"], "Köln"))
        elif scenario == "7":
            events = history(3, now - timedelta(days=6), 5 * hour)
        else:
            minutes = max(0, int((now - request.created_at).total_seconds() // 60))
            count = min(len(_STEPS), 1 + minutes)
            events = history(count, now, timedelta(minutes=1))
            eta = now.replace(second=0, microsecond=0) + timedelta(minutes=len(_STEPS) - count)

        latest = events[-1]
        # A parcel that has not moved for days has no believable estimate.
        open_delivery = scenario != "7" and latest.status in (
            Status.REGISTERED,
            Status.IN_TRANSIT,
            Status.OUT_FOR_DELIVERY,
        )
        return TrackingResult(
            status=latest.status,
            description=latest.description,
            location=latest.location,
            events=events,
            eta_from=eta if open_delivery else None,
            eta_to=eta + 3 * hour if open_delivery and scenario in "123" else None,
        )


MODULE = DemoCarrier()
