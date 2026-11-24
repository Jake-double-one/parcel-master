"""GLS (Germany).

Reads the open answer GLS gives about a parcel number: where the parcel
stands and when it is expected. That answer holds no history, so each new
state is added to the one collected so far.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from parcelmaster.carriers.base import (
    CarrierError,
    CarrierModule,
    TrackingResult,
    TrackRequest,
)
from parcelmaster.status import Status

log = logging.getLogger(__name__)

STATUS_URL = "https://gls-group.eu/app/service/open/rest/{place}/rstt029"
PAGE_URL = "https://www.gls-pakete.de/reach-sendungsverfolgung"
#: GLS turns away who asks in quick succession; this keeps well below that.
DAILY_BUDGET = 300

#: The stage GLS names for the parcel.
_STAGES = {
    "PREADVICE": Status.REGISTERED,
    "INTRANSIT": Status.IN_TRANSIT,
    "INWAREHOUSE": Status.IN_TRANSIT,
    "INDELIVERY": Status.OUT_FOR_DELIVERY,
    "DELIVERED": Status.DELIVERED,
    "DELIVEREDPS": Status.READY_FOR_PICKUP,
    "NOTDELIVERED": Status.PROBLEM,
    "CANCELED": Status.PROBLEM,
}
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# "25.11.2026, 12:45 - 16:00" in German, "25-Nov-2026, 12:45 - 16:00" in English.
_WINDOW = re.compile(
    r"(\d{1,2})[.\-](\d{1,2}|[A-Za-z]{3})[.\-](\d{4})"
    r"(?:\D{1,3}(\d{1,2}):(\d{2})(?:\s*-\s*(\d{1,2}):(\d{2}))?)?"
)


def read_window(text: Any) -> tuple[datetime | None, datetime | None]:
    """The day and, where named, the hours GLS expects to deliver in; local time."""
    found = _WINDOW.search(text) if isinstance(text, str) else None
    if not found:
        return None, None
    day, month, year, hour, minute, to_hour, to_minute = found.groups()
    if not month.isdigit():
        if month.lower() not in _MONTHS:
            return None, None
        month = str(_MONTHS.index(month.lower()) + 1)
    try:
        if hour is None:
            # Only the day is known: the whole of it.
            start = datetime(int(year), int(month), int(day))
            return start, start.replace(hour=23, minute=59)
        start = datetime(int(year), int(month), int(day), int(hour), int(minute))
        end = start.replace(hour=int(to_hour), minute=int(to_minute)) if to_hour else start
    except ValueError:
        return None, None
    return start, end


def parse_response(payload: Any) -> TrackingResult:
    """Turn GLS' answer about one parcel number into a tracking result."""
    parcels = payload.get("tuStatus") if isinstance(payload, dict) else None
    parcel = parcels[0] if isinstance(parcels, list) and parcels else None
    if not isinstance(parcel, dict):
        raise CarrierError("not_found")
    bar = parcel.get("progressBar")
    bar = bar if isinstance(bar, dict) else {}
    stage = str(bar.get("statusInfo") or "").upper()
    status = _STAGES.get(stage)
    if status is None:
        log.info("GLS names a stage not known here: %s", stage or "none")
        status = Status.IN_TRANSIT
    if bar.get("retourFlag") and status is Status.DELIVERED:
        status = Status.RETURNED  # it went back to who sent it

    # The sentence of the stage the parcel is in; its first line says it all.
    description = ""
    for step in bar.get("statusBar") or []:
        if isinstance(step, dict) and str(step.get("status") or "").upper() == stage:
            description = str(step.get("statusText") or "").strip().split("\n")[0].strip()
    description = description or str(bar.get("statusText") or "").strip()

    eta_from = eta_to = None
    arrival = parcel.get("arrivalTime")
    if isinstance(arrival, dict) and status not in (Status.DELIVERED, Status.RETURNED):
        eta_from, eta_to = read_window(arrival.get("value"))
    return TrackingResult(
        status=status,
        description=description,
        eta_from=eta_from,
        eta_to=eta_to,
        collect=True,
    )


class GlsDe(CarrierModule):
    id = "de.gls"
    country = "de"
    name = "GLS"
    colour = "#061ab1"
    patterns = (re.compile(r"\d{11,12}"),)
    default_daily_budget = DAILY_BUDGET

    def tracking_url(self, tracking_number: str, extra: dict[str, str], language: str) -> str:
        return f"{PAGE_URL}?match={tracking_number}&trackingNumber={tracking_number}"

    def extract_from_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if host not in ("gls-pakete.de", "gls-group.eu", "gls-group.com", "gls-group.net"):
            return None
        query = parse_qs(parsed.query)
        for candidate in [*query.get("trackingNumber", []), *query.get("match", [])]:
            number = self.normalize(candidate)
            if self.matches(number):
                return number
        return None

    async def track(self, request: TrackRequest, http: httpx.AsyncClient) -> TrackingResult:
        place = "DE/de" if request.language == "de" else "GROUP/en"
        try:
            response = await http.get(
                STATUS_URL.format(place=place),
                params={"match": request.tracking_number},
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as error:
            raise CarrierError("unavailable", type(error).__name__) from error
        if response.status_code == 404:
            raise CarrierError("not_found")  # unknown, or not yet handed to GLS
        if response.status_code == 429:
            raise CarrierError("rate_limited")
        if response.status_code >= 300:
            # A redirect leads to GLS' sign-up for its API: this way is closed then.
            raise CarrierError("unavailable", f"HTTP {response.status_code}")
        log.debug("GLS: %s", response.text[:4000])
        try:
            payload = response.json()
        except ValueError as error:
            log.warning("GLS answered something else than JSON: %s", response.text[:300])
            raise CarrierError("invalid_response", "not JSON") from error
        return parse_response(payload)


MODULE = GlsDe()
