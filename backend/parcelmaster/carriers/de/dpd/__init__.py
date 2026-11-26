"""DPD (Germany).

DPD tells about a parcel only to a person in a browser: who enters the number
on its website and solves a captcha. So nothing is asked from here. The
companion script passes on what DPD's parcel page (my.dpd.de) shows the user:
the steps with their days, the state, the time of delivery and, on the
delivery day, where the van is.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from parcelmaster.carriers.base import (
    CarrierModule,
    Field,
    Found,
    TrackingEvent,
    TrackingResult,
)
from parcelmaster.status import Status

log = logging.getLogger(__name__)

#: Where DPD asks for the parcel number. It then wants a captcha solved, so a
#: parcel cannot be linked to directly, and no server can ask for it.
PAGE_URL = "https://www.dpd.com/de/de/empfangen/"
#: The length kept of each text.
MAX_TEXT = 300

# The sentences DPD uses, German and English. "Delivered" ends the tracking,
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


def step_status(text: str) -> Status:
    """The status one sentence of DPD stands for."""
    for words, status in _STEPS:
        if words.search(text):
            return status
    return Status.IN_TRANSIT


def _words(value: Any) -> str:
    """The text of a ``{"content": [...]}`` entry, or of a plain string."""
    if isinstance(value, dict):
        value = value.get("content")
    if isinstance(value, list):
        value = " ".join(str(part).strip() for part in value[:10] if isinstance(part, str))
    return value.strip()[:MAX_TEXT] if isinstance(value, str) else ""


def _time(value: Any) -> datetime | None:
    """A time as the script notes it: "2026-11-26T13:49", local."""
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


class DpdDe(CarrierModule):
    id = "de.dpd"
    country = "de"
    name = "DPD"
    colour = "#dc0032"
    patterns = (re.compile(r"\d{14}"),)
    settings_fields = (Field("live_map", type="choice", options=("on", "off")),)
    asks = False
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
        return PAGE_URL

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


MODULE = DpdDe()
