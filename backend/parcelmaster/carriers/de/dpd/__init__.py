"""DPD (Germany).

Reads the open answer of DPD's parcel tracking: the stage the parcel is in
and every scan with its time and place. DPD answers in the language asked for.
DPD's own parcel page (my.dpd.de) shows more to who is signed in: the
companion script passes on its state and, on the delivery day, where the van is.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
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

STATUS_URL = "https://tracking.dpd.de/rest/plc/{locale}/{number}"
PAGE_URL = "https://my.dpd.de/myParcel-tracking.aspx?parcelno={number}"
#: DPD names no limit; this only keeps a mistake from asking without end.
DAILY_BUDGET = 300
#: Scans read from one answer at most, and the length kept of each text.
MAX_SCANS = 100
MAX_TEXT = 300

#: The five stages of DPD's progress bar.
_STAGES = {
    "ACCEPTED": Status.REGISTERED,
    "AT_SENDING_DEPOT": Status.IN_TRANSIT,
    "ON_THE_ROAD": Status.IN_TRANSIT,
    "AT_DELIVERY_DEPOT": Status.IN_TRANSIT,
    "DELIVERED": Status.DELIVERED,
}

# The sentences of the scans, German and English. "Delivered" ends the tracking,
# so it is asked last and never for what is about to happen or did not happen.
_STEPS = (
    (
        re.compile(
            r"zurück an den (absender|versender)|rücksendung|retour"
            r"|return(ed)? to (the )?(sender|consignor)|being returned",
            re.I,
        ),
        Status.RETURNED,
    ),
    (
        re.compile(
            r"nicht (zugestellt|angetroffen|zustellbar|möglich)|konnte nicht|zustellversuch"
            r"|zustellhindernis|annahme verweigert|beschädigt|verzöger|fehlgeleitet"
            r"|could not|not (be )?delivered|unsuccessful|refused|damaged|delay|misrouted",
            re.I,
        ),
        Status.PROBLEM,
    ),
    (
        re.compile(
            r"(paketshop|pickup|abholstation)[^.]{0,200}?(abgegeben|hinterlegt|bereit|zugestellt"
            r"|noch nicht abgeholt)|zur abholung|abholbereit"
            r"|ready for (pick-?up|collection)"
            r"|(delivered|handed) to (a |the )?(pickup|parcel ?shop)",
            re.I,
        ),
        Status.READY_FOR_PICKUP,
    ),
    (
        re.compile(
            r"in zustellung|zustellfahrzeug|wird heute zugestellt|out for delivery"
            r"|on the delivery vehicle|will be delivered today",
            re.I,
        ),
        Status.OUT_FOR_DELIVERY,
    ),
    (
        re.compile(
            r"auftragsdaten|paketdaten|noch nicht (an dpd|bei dpd)"
            r"|order (information|data)|parcel data|not yet (been )?(handed|received)",
            re.I,
        ),
        Status.REGISTERED,
    ),
    (
        re.compile(
            r"\b(wurde|ist)\b(?:(?!\bwird\b)[^.]){0,80}?\b(zugestellt|abgeholt|übergeben)\b"
            r"|^(paket )?(zugestellt|abgeholt)\b|erfolgreich zugestellt"
            r"|\b(has|was) been (delivered|collected)\b"
            r"|^delivered\b|^collected\b",
            re.I,
        ),
        Status.DELIVERED,
    ),
)

# "… am 26.11.2026 zwischen 10:15 und 11:15 …", "… on 26.11.2026 between 10:15 and 11:15 …"
_WINDOW = re.compile(
    r"(\d{1,2})\.(\d{1,2})\.(\d{4})\D{1,40}?(\d{1,2}):(\d{2})\D{1,12}?(\d{1,2}):(\d{2})"
)


def step_status(text: str) -> Status:
    """The status one sentence of a scan stands for."""
    for words, status in _STEPS:
        if words.search(text):
            return status
    return Status.IN_TRANSIT


def read_window(text: str) -> tuple[datetime | None, datetime | None]:
    """The delivery window a sentence names, if it names a day and two times."""
    found = _WINDOW.search(text)
    if not found:
        return None, None
    day, month, year, hour, minute, to_hour, to_minute = (int(part) for part in found.groups())
    try:
        start = datetime(year, month, day, hour, minute)
        end = start.replace(hour=to_hour, minute=to_minute)
    except ValueError:
        return None, None
    return (start, end) if end >= start else (None, None)


def _words(value: Any) -> str:
    """The text of a ``{"content": [...]}`` entry, or of a plain string."""
    if isinstance(value, dict):
        value = value.get("content")
    if isinstance(value, list):
        value = " ".join(str(part).strip() for part in value[:10] if isinstance(part, str))
    return value.strip()[:MAX_TEXT] if isinstance(value, str) else ""


def _time(value: Any) -> datetime | None:
    """A scan's time: "2026-11-24T18:03:11", local."""
    try:
        return datetime.fromisoformat(value.strip()) if isinstance(value, str) else None
    except ValueError:
        return None


#: The five steps DPD's page lists, by the names the page itself gives them.
_PAGE_STEPS = {
    "Start": Status.REGISTERED,
    "OnTheRoad": Status.IN_TRANSIT,
    "DeliveryDepot": Status.IN_TRANSIT,
    "CarLoad": Status.OUT_FOR_DELIVERY,
    "Delivered": Status.DELIVERED,
}


def _page_day(value: Any, today: date) -> date | None:
    """A day as the page writes it, "24.11.": the latest such day that is not ahead."""
    found = (
        re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.", value.strip()) if isinstance(value, str) else None
    )
    if not found:
        return None
    for year in (today.year, today.year - 1):
        try:
            day = date(year, int(found.group(2)), int(found.group(1)))
        except ValueError:
            continue
        if day <= today + timedelta(days=1):
            return day
    return None


def page_steps(value: Any, today: date) -> list[TrackingEvent]:
    """The steps DPD's page lists with their days, newest first.

    The page names no time of day: the steps of one day keep their order by seconds.
    """
    events = []
    for index, step in enumerate(value[:10] if isinstance(value, list) else []):
        if not isinstance(step, dict) or step.get("step") not in _PAGE_STEPS:
            continue
        text, day = _words(step.get("text")), _page_day(step.get("day"), today)
        if text and day:
            when = datetime(day.year, day.month, day.day, 0, 0, index)
            events.append(TrackingEvent(when, _PAGE_STEPS[step["step"]], text))
    events.reverse()
    return events


def _spot(value: Any) -> tuple[float, float] | None:
    """A place as the page names it: latitude and longitude."""
    if not isinstance(value, list) or len(value) != 2:
        return None
    if not all(isinstance(part, int | float) and not isinstance(part, bool) for part in value):
        return None
    return float(value[0]), float(value[1])


def parse_response(payload: Any) -> TrackingResult:
    """Turn DPD's answer about one parcel number into a tracking result."""
    data = payload.get("parcellifecycleResponse") if isinstance(payload, dict) else None
    data = data.get("parcelLifeCycleData") if isinstance(data, dict) else None
    if not isinstance(data, dict):
        raise CarrierError("not_found")

    stages = data.get("statusInfo")
    stages = [s for s in stages if isinstance(s, dict)] if isinstance(stages, list) else []
    current = next((s for s in stages if s.get("isCurrentStatus") is True), None)
    if current is None:
        reached = [s for s in stages if s.get("statusHasBeenReached") is True]
        current = reached[-1] if reached else None

    scans = data.get("scanInfo")
    scans = scans.get("scan") if isinstance(scans, dict) else None
    events = []
    for scan in scans[:MAX_SCANS] if isinstance(scans, list) else []:
        if not isinstance(scan, dict):
            continue
        text = _words(scan.get("scanDescription"))
        if not text:
            continue
        place = scan.get("scanData")
        place = place.get("location") if isinstance(place, dict) else None
        events.append(
            TrackingEvent(
                timestamp=_time(scan.get("date")),
                status=step_status(text),
                description=text,
                location=place.strip()[:100] or None if isinstance(place, str) else None,
            )
        )
    # Newest first, whichever way DPD lists them; scans without a time go last.
    events.sort(key=lambda e: (e.timestamp is not None, e.timestamp or datetime.min), reverse=True)

    if current is None and not events:
        raise CarrierError("not_found")  # unknown, or not yet announced to DPD
    stage = str(current.get("status") or "").upper() if current else ""
    staged = _STAGES.get(stage)
    if current is not None and staged is None:
        log.info("DPD names a stage not known here: %s", stage or "none")

    if events:
        status = events[0].status
        description = events[0].description
        location = events[0].location
        # The bar says "delivered" in plain words even where the scan's sentence does not.
        if staged is Status.DELIVERED and status in (Status.IN_TRANSIT, Status.OUT_FOR_DELIVERY):
            status = Status.DELIVERED
    else:
        status = staged or Status.IN_TRANSIT
        description = _words(current.get("description")) or _words(current.get("label"))
        location = None

    eta_from = eta_to = None
    if current is not None and status not in (Status.DELIVERED, Status.RETURNED):
        told = " ".join(
            _words(current.get(key)) if key == "description" else _words({"content": items})
            for key in ("description", "importantItems", "normalItems")
            if (items := current.get(key))
        )
        eta_from, eta_to = read_window(told)
    return TrackingResult(
        status=status,
        description=description,
        location=location,
        events=events,
        eta_from=eta_from,
        eta_to=eta_to,
    )


class DpdDe(CarrierModule):
    id = "de.dpd"
    country = "de"
    name = "DPD"
    colour = "#dc0032"
    patterns = (re.compile(r"\d{14}"),)
    settings_fields = (Field("live_map", type="choice", options=("on", "off")),)
    default_daily_budget = DAILY_BUDGET
    pickup_hold_days = 7

    def companion_script(self) -> str | None:
        return (Path(__file__).parent / "companion.user.js").read_text(encoding="utf-8")

    def _shown(self, payload: Any) -> list[tuple[str, dict[str, Any], TrackingResult]]:
        """Number, entry and state for each parcel DPD's page handed on."""
        parcels = payload.get("parcels") if isinstance(payload, dict) else None
        shown = []
        for parcel in (parcels if isinstance(parcels, list) else [])[:10]:
            if not isinstance(parcel, dict) or not isinstance(parcel.get("number"), str):
                continue
            number = self.normalize(parcel["number"][:40])
            if not self.matches(number):
                continue
            # The driver's note says most; the label below the bar is the fallback.
            told = [_words(parcel.get(key)) for key in ("note", "status")]
            told = [words for words in told if words]
            steps = page_steps(parcel.get("steps"), datetime.now().date())
            if not told and not steps:
                continue
            status = next(
                (s for s in map(step_status, told) if s is not Status.IN_TRANSIT),
                steps[0].status if steps else Status.IN_TRANSIT,
            )
            if steps:
                # The page lists the way so far: that is the history, as far as it tells.
                shown.append(
                    (
                        number,
                        parcel,
                        TrackingResult(
                            status=steps[0].status,
                            description=steps[0].description,
                            events=steps,
                            keep_window=True,
                        ),
                    )
                )
                continue
            van, home = _spot(parcel.get("van")), _spot(parcel.get("home"))
            going = status not in (Status.DELIVERED, Status.RETURNED)
            result = TrackingResult(
                status=status,
                description=told[0],
                collect=True,  # the page shows the state, not the way there
                told_at=_time(parcel.get("when")) if not going else None,
                keep_window=True,
                van=van if going else None,
                home=home if going and van else None,
            )
            shown.append((number, parcel, result))
        return shown

    def take_from_companion(self, payload: Any) -> list[tuple[str, TrackingResult]]:
        """What DPD's parcel page showed in the user's browser."""
        return [(number, result) for number, _, result in self._shown(payload)]

    def discover(self, payload: Any) -> list[Found]:
        return [
            Found(
                number=number,
                title=_words(parcel.get("sender"))[:200],
                settled=result.status in (Status.DELIVERED, Status.RETURNED),
            )
            for number, parcel, result in self._shown(payload)
        ]

    def describe_companion_news(self, payload: Any, known: set[str]) -> list[str]:
        lines = []
        for number, parcel, result in self._shown(payload):
            parts = parcel.get("parts")
            names = (
                [p[:40] for p in parts[:60] if isinstance(p, str)]
                if isinstance(parts, list)
                else []
            )
            lines.append(
                f"{number} ({'tracked' if number in known else 'not tracked'}), {result.status}"
                f", stage {parcel.get('stage')!r:.6}, van {'yes' if result.van else 'no'}"
                f", parts {' '.join(names)}"
            )
        return lines

    def normalize(self, tracking_number: str) -> str:
        # A scanner reads the barcode with a "%" in front.
        number = super().normalize(tracking_number).lstrip("%")
        # The label prints a check character after the fourteen digits.
        if re.fullmatch(r"\d{14}[0-9A-Z]", number):
            return number[:14]
        # The line below the barcode: seven digits of the postal code, the parcel
        # number, service and country, and a check character.
        if re.fullmatch(r"\d{27}[0-9A-Z]?", number):
            return number[7:21]
        return number

    def tracking_url(self, tracking_number: str, extra: dict[str, str], language: str) -> str:
        return PAGE_URL.format(number=tracking_number)

    def extract_from_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not any(host == site or host.endswith("." + site) for site in ("dpd.de", "dpd.com")):
            return None
        query = parse_qs(parsed.query)
        candidates = [
            value
            for key in ("parcelNumber", "parcelno", "pknr", "pno", "query", "match")
            for value in query.get(key, [])
        ]
        candidates += re.findall(r"/parcel/([0-9A-Za-z]{14,15})(?:/|$)", parsed.path)
        for candidate in candidates:
            number = self.normalize(candidate)
            if self.matches(number):
                return number
        return None

    async def track(self, request: TrackRequest, http: httpx.AsyncClient) -> TrackingResult:
        locale = "de_DE" if request.language == "de" else "en_US"
        try:
            response = await http.get(
                STATUS_URL.format(locale=locale, number=request.tracking_number),
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as error:
            raise CarrierError("unavailable", type(error).__name__) from error
        if response.status_code == 404:
            raise CarrierError("not_found")
        if response.status_code == 429:
            raise CarrierError("rate_limited")
        if response.status_code >= 300:
            raise CarrierError("unavailable", f"HTTP {response.status_code}")
        log.debug("DPD: %s", response.text[:4000])
        try:
            payload = response.json()
        except ValueError as error:
            log.warning("DPD answered something else than JSON: %s", response.text[:300])
            raise CarrierError("invalid_response", "not JSON") from error
        return parse_response(payload)


MODULE = DpdDe()
