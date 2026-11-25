"""GLS (Germany).

Reads the open answer GLS gives about a parcel number: where the parcel
stands and when it is expected. That answer holds no history, so each new
state is added to the one collected so far. The real history stands only on
GLS' own tracking page, which answers a browser and no server: the companion
script passes it on when the user looks at that page.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from parcelmaster.carriers.base import (
    CarrierError,
    CarrierModule,
    Field,
    Found,
    TrackingEvent,
    TrackingResult,
    TrackRequest,
)
from parcelmaster.status import Status

log = logging.getLogger(__name__)

STATUS_URL = "https://gls-group.eu/app/service/open/rest/{place}/rstt029"
PAGE_URL = "https://www.gls-pakete.de/reach-sendungsverfolgung"
#: GLS turns away who asks in quick succession; this keeps well below that.
DAILY_BUDGET = 300
#: Below this many stops the page in the user's browser tells more than the
#: open status, whose planned window would only undo the live one.
FEW_STOPS = 10
#: So long after the page's last news the open status is asked again.
QUIET_AFTER_LIVE_MINUTES = 60

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


# The sentences of the history on GLS' page. "Delivered" ends the tracking, so
# it only counts as the plain past: not "wird zugestellt", not "konnte nicht".
_STEPS = (
    (re.compile(r"zurück an den (absender|versender)|retourniert|rücksendung", re.I), "returned"),
    (
        re.compile(
            r"nicht (zugestellt|angetroffen|zustellbar|möglich)|konnte nicht|zustellversuch"
            r"|verzöger|beschädigt|annahme verweigert",
            re.I,
        ),
        "problem",
    ),
    (
        re.compile(
            r"(paketshop|paketstation|abholort)[^.]{0,200}?(zugestellt|abgegeben|bereit)"
            r"|zur abholung bereit|abholbereit",
            re.I,
        ),
        "ready_for_pickup",
    ),
    (re.compile(r"^das paket wurde (erfolgreich )?(zugestellt|abgeholt)\b", re.I), "delivered"),
    (
        re.compile(r"in (der )?zustellung|zustellfahrzeug|im laufe des tages zugestellt", re.I),
        "out_for_delivery",
    ),
    (re.compile(r"noch nicht an gls übergeben|paketdaten wurden", re.I), "registered"),
)


def step_status(text: str) -> Status:
    """The status one sentence of the page's history stands for."""
    for words, status in _STEPS:
        if words.search(text):
            return Status(status)
    return Status.IN_TRANSIT


def _page_time(value: Any) -> datetime | None:
    """A time as the page notes it: "2026-11-24 09:27:28", local."""
    try:
        return datetime.fromisoformat(value.strip()) if isinstance(value, str) else None
    except ValueError:
        return None


def _live_window(value: Any) -> tuple[datetime | None, datetime | None]:
    """The window of the page's live view: two times with their zone."""
    if not isinstance(value, dict):
        return None, None
    try:
        start = datetime.fromisoformat(str(value.get("from")).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(value.get("to")).replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if start.tzinfo is None or end.tzinfo is None or end < start:
        return None, None
    return start, end


def _spot(value: Any) -> tuple[float, float] | None:
    """A place on the page's live map as (latitude, longitude)."""
    if not isinstance(value, list) or len(value) != 2:
        return None
    if any(isinstance(part, bool) or not isinstance(part, int | float) for part in value):
        return None
    latitude, longitude = float(value[0]), float(value[1])
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return latitude, longitude


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
    settings_fields = (Field("live_map", type="choice", options=("on", "off")),)
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

    def companion_script(self) -> str | None:
        return (Path(__file__).parent / "companion.user.js").read_text(encoding="utf-8")

    def _shown(self, payload: Any) -> list[tuple[str, dict[str, Any], list[TrackingEvent]]]:
        """Number, entry and history for each parcel the tracking page handed on."""
        parcels = payload.get("parcels") if isinstance(payload, dict) else None
        shown = []
        for parcel in (parcels if isinstance(parcels, list) else [])[:10]:
            if not isinstance(parcel, dict) or not isinstance(parcel.get("number"), str):
                continue
            number = self.normalize(parcel["number"])
            if not self.matches(number):
                continue
            events = []
            steps = parcel.get("events")
            for step in (steps if isinstance(steps, list) else [])[:100]:
                text = step.get("text") if isinstance(step, dict) else None
                when = _page_time(step.get("time")) if isinstance(step, dict) else None
                if not isinstance(text, str) or not text.strip() or when is None:
                    continue
                text = text.strip()[:300]  # cut first: nothing longer is matched or kept
                place = step.get("place")
                events.append(
                    TrackingEvent(
                        timestamp=when,
                        status=step_status(text),
                        description=text,
                        location=place.strip()[:100] or None if isinstance(place, str) else None,
                    )
                )
            events.sort(key=lambda event: event.timestamp, reverse=True)  # newest first
            shown.append((number, parcel, events))
        return shown

    def take_from_companion(self, payload: Any) -> list[tuple[str, TrackingResult]]:
        """What GLS' tracking page showed in the user's browser: history, window, stops."""
        found = []
        for number, parcel, events in self._shown(payload):
            eta_from, eta_to = _live_window(parcel.get("window"))
            stops = parcel.get("stops")
            if isinstance(stops, bool) or not isinstance(stops, int) or not 0 <= stops < 1000:
                stops = None
            van, home = _spot(parcel.get("van")), _spot(parcel.get("home"))
            if events:
                result = TrackingResult(
                    van=van,
                    home=home if van else None,
                    status=events[0].status,
                    description=events[0].description,
                    location=events[0].location,
                    events=events,
                    eta_from=eta_from,
                    eta_to=eta_to,
                    keep_window=eta_from is None,  # the history alone names no window
                    stops_left=stops,
                )
                if stops is not None and stops < FEW_STOPS:
                    result.hold_minutes = QUIET_AFTER_LIVE_MINUTES
            elif eta_from is not None or stops is not None:
                # The history is folded away on the page: only the live view is news.
                result = TrackingResult(
                    status=Status.OUT_FOR_DELIVERY,
                    eta_from=eta_from,
                    eta_to=eta_to,
                    stops_left=stops,
                    live_only=True,
                )
            else:
                continue
            found.append((number, result))
        return found

    def discover(self, payload: Any) -> list[Found]:
        return [
            Found(
                number=number,
                title=str(parcel.get("sender") or "").strip()[:200],
                settled=bool(events) and events[0].status in (Status.DELIVERED, Status.RETURNED),
                full=bool(events),
            )
            for number, parcel, events in self._shown(payload)
        ]

    def describe_companion_news(self, payload: Any, known: set[str]) -> list[str]:
        return [
            f"{number} ({'tracked' if number in known else 'not tracked'}), {len(events)} step(s)"
            f", window {'yes' if parcel.get('window') else 'no'}, stops {parcel.get('stops')!r:.6}"
            for number, parcel, events in self._shown(payload)
        ]

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
