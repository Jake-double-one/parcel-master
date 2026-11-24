"""Hermes (Germany).

Reads the public tracking that myhermes.de shows: the same answer its
tracking page fetches, without a key. Hermes offers private recipients no
tracking API of its own.
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

SEARCH_URL = "https://api.my-deliveries.de/tnt/v2/shipments/search/"
#: What Hermes shows only to who knows the recipient's postal code: the live round.
ADDRESS_URL = "https://api.my-deliveries.de/tnt/v2/address/"
PAGE_URL = "https://www.myhermes.de/empfangen/sendungsverfolgung/sendungsinformation"
#: The website names no limit; this only keeps a mistake from asking without end.
DAILY_BUDGET = 500
#: Hermes writes this many stops for "more than 30".
MANY_STOPS = 31
#: From this many stops on, the round is followed by the minute.
CLOSE_STOPS = 5

# The answer names each step in words and with a code of its own. The codes are
# not documented, so both are read: the code where it is plain, else the words.
_RETURNED = re.compile(
    r"RETURN|RETOURE|zurück an den (absender|versender)|rücksendung|zurückgesandt"
    r"|return(ed)? to (the )?sender|being returned",
    re.I,
)
# "Delivered" ends the tracking, so only the past tense counts: "wird morgen
# zugestellt" and "konnte nicht zugestellt werden" must not match, and neither
# "wurde geladen und wird heute zugestellt", where "wurde" belongs to the loading.
_DELIVERED = re.compile(
    r"DELIVERED|COLLECTED|wurde(?:(?!\bwird\b)[^.]){0,80}?(zugestellt|abgeholt|übergeben)"
    r"|erfolgreich zugestellt"
    r"|(has|was) been (successfully )?(delivered|collected)|was delivered",
    re.I,
)
_FAILED = re.compile(
    r"nicht (zugestellt|angetroffen|zustellbar|möglich)|zustellversuch|konnte nicht"
    r"|could not be delivered|not (be )?delivered|nobody (was )?(at home|met)"
    r"|delivery attempt",
    re.I,
)
_READY_FOR_PICKUP = re.compile(
    r"READY_FOR_COLLECTION|zur abholung bereit|abholbereit|liegt (im|in)[^.]{0,80}bereit"
    r"|ready for (collection|pick-?up)",
    re.I,
)
_OUT_FOR_DELIVERY = re.compile(
    r"OUT_FOR_DELIVERY|IN_DELIVERY|DELIVERY_TOUR|in (der )?zustellung|zustellfahrzeug|zustelltour"
    r"|wird (heute|voraussichtlich heute) zugestellt|out for delivery|on the delivery vehicle",
    re.I,
)
_REGISTERED = re.compile(
    r"ANNOUNCED|elektronisch angekündigt|angekündigt|noch nicht (bei|an) hermes"
    r"|has been announced|announced electronically",
    re.I,
)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


#: Codes seen in real answers. A known code settles the step; its wording is
#: only read for codes not listed here.
_CODES = {
    "ANNOUNCED": Status.REGISTERED,
    "TAKEN_OVER_BY_HERMES": Status.IN_TRANSIT,
    "SORTED": Status.IN_TRANSIT,
    "ARRIVED_IN_DESTINATION_REGION": Status.IN_TRANSIT,
    "DELIVERY_TOUR_STARTED": Status.OUT_FOR_DELIVERY,
}


def _step_status(step: dict[str, Any]) -> Status:
    """The status one step of the history stands for."""
    code = str(step.get("parcelStatus") or "")
    text = str(step.get("historyText") or "")
    if code.upper() in _CODES:
        return _CODES[code.upper()]
    for words in (code, text):
        if not words:
            continue
        if _RETURNED.search(words):
            return Status.RETURNED
        if _FAILED.search(words):
            return Status.PROBLEM
        if _READY_FOR_PICKUP.search(words):
            return Status.READY_FOR_PICKUP
        if _DELIVERED.search(words):
            return Status.DELIVERED
        if _OUT_FOR_DELIVERY.search(words):
            return Status.OUT_FOR_DELIVERY
        if _REGISTERED.search(words):
            return Status.REGISTERED
    if str(step.get("status") or "").upper() == "UNHAPPY":
        return Status.PROBLEM
    return Status.IN_TRANSIT


def parse_response(payload: Any) -> TrackingResult:
    """Turn the answer of the shipment search into a tracking result."""
    shipment = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(shipment, dict) or not shipment:
        raise CarrierError("not_found")
    steps = [step for step in shipment.get("parcelProgress") or [] if isinstance(step, dict)]
    events = [
        TrackingEvent(
            timestamp=_parse_time(step.get("timestamp")),
            status=_step_status(step),
            description=str(step.get("historyText") or "").strip(),
        )
        for step in steps
    ]
    events.sort(key=lambda event: event.timestamp.timestamp() if event.timestamp else 0)
    events.reverse()  # newest first, like everywhere else

    attributes = shipment.get("parcelAttributes")
    attributes = attributes if isinstance(attributes, dict) else {}
    view = shipment.get("viewParameters")
    view = view if isinstance(view, dict) else {}
    # What the page itself decides by, before the wording of the last step.
    if attributes.get("returnDeliveredTimestamp") or attributes.get("retoureDeliveredTimestamp"):
        status = Status.RETURNED
    elif attributes.get("delivered") or view.get("collected"):
        status = Status.DELIVERED
    elif (
        view.get("readyForCollection")
        or view.get("readyForParcelBoxCollection")
        or attributes.get("readyForParcelBoxCollection")
    ):
        status = Status.READY_FOR_PICKUP
    elif events:
        status = events[0].status
        if status is Status.IN_TRANSIT and view.get("deliveryFailedAttempt"):
            status = Status.PROBLEM  # nobody was met; Hermes tries again
        if status not in (Status.DELIVERED, Status.RETURNED) and (
            attributes.get("shipmentStopped") or attributes.get("deliveryStopped")
        ):
            status = Status.PROBLEM  # Hermes holds the parcel back
    else:
        status = Status.REGISTERED

    eta_from = eta_to = None
    forecast = shipment.get("forecast")
    if isinstance(forecast, dict) and status not in (Status.DELIVERED, Status.RETURNED):
        eta_from = _parse_time(forecast.get("deliveryTimeFromUTC"))
        eta_to = _parse_time(forecast.get("deliveryTimeToUTC")) or eta_from
        if eta_from is None:
            eta_from = eta_to = _parse_time(forecast.get("pickupReadyFromUTC"))
    return TrackingResult(
        status=status,
        description=events[0].description if events else "",
        events=events,
        eta_from=eta_from,
        eta_to=eta_to,
    )


def read_live(payload: Any, result: TrackingResult) -> None:
    """Add what the address answer knows about the delivery round: the stops left."""
    address = payload.get("address") if isinstance(payload, dict) else None
    address = address if isinstance(address, dict) else payload
    live = address.get("livetracking") if isinstance(address, dict) else None
    stops = live.get("stopsRemaining") if isinstance(live, dict) else None
    if isinstance(stops, bool) or not isinstance(stops, int) or stops < 0:
        return
    result.stops_about = stops >= MANY_STOPS
    result.stops_left = min(stops, MANY_STOPS - 1)
    result.again_in_minutes = 1 if stops <= CLOSE_STOPS else 5


class HermesDe(CarrierModule):
    id = "de.hermes"
    country = "de"
    name = "Hermes"
    colour = "#0091cd"
    patterns = (
        re.compile(r"\d{14}"),
        re.compile(r"\d{16}"),
        re.compile(r"H\d{19}"),
    )
    settings_fields = (Field("default_postal_code", placeholder="53113"),)
    parcel_fields = (Field("postal_code", placeholder="53113"),)
    default_daily_budget = DAILY_BUDGET
    pickup_hold_days = 7

    def tracking_url(self, tracking_number: str, extra: dict[str, str], language: str) -> str:
        return f"{PAGE_URL}#{tracking_number}"

    def extract_from_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host not in ("myhermes.de", "www.myhermes.de", "www.hermesworld.com"):
            return None
        query = parse_qs(parsed.query)
        candidates = [parsed.fragment, *query.get("TrackId", []), *query.get("TrackID", [])]
        for candidate in candidates:
            number = self.normalize(candidate.split("/")[-1])
            if self.matches(number):
                return number
        return None

    def companion_script(self) -> str | None:
        return (Path(__file__).parent / "companion.user.js").read_text(encoding="utf-8")

    def _shown(self, payload: Any) -> list[tuple[str, dict[str, Any]]]:
        """Number and answer for each shipment the tracking page handed on."""
        shipments = payload.get("shipments") if isinstance(payload, dict) else None
        shown = []
        for shipment in shipments if isinstance(shipments, list) else []:
            if not isinstance(shipment, dict) or not isinstance(shipment.get("barcode"), str):
                continue
            number = self.normalize(shipment["barcode"])
            if self.matches(number):
                shown.append((number, shipment))
        return shown

    def take_from_companion(self, payload: Any) -> list[tuple[str, TrackingResult]]:
        """What the tracking page in the user's browser got from Hermes."""
        found = []
        for number, shipment in self._shown(payload):
            try:
                found.append((number, parse_response(shipment)))
            except CarrierError:
                continue
        return found

    def discover(self, payload: Any) -> list[Found]:
        found = []
        for number, shipment in self._shown(payload):
            sender = shipment.get("atg")
            sender = sender.get("companyName") if isinstance(sender, dict) else ""
            attributes = shipment.get("parcelAttributes")
            attributes = attributes if isinstance(attributes, dict) else {}
            try:
                settled = parse_response(shipment).status in (Status.DELIVERED, Status.RETURNED)
            except CarrierError:
                settled = False
            found.append(
                Found(
                    number=number,
                    title=str(sender or "").strip(),
                    direction=(
                        "outgoing" if attributes.get("directionEnum") == "RETURN" else "incoming"
                    ),
                    settled=settled,
                )
            )
        return found

    def describe_companion_news(self, payload: Any, known: set[str]) -> list[str]:
        return [
            f"{number} ({'tracked' if number in known else 'not tracked'}), "
            f"{len(shipment.get('parcelProgress') or [])} step(s)"
            for number, shipment in self._shown(payload)
        ]

    async def track(self, request: TrackRequest, http: httpx.AsyncClient) -> TrackingResult:
        try:
            response = await http.get(
                SEARCH_URL + request.tracking_number,
                headers={
                    "Accept": "application/json",
                    "X-Language": "de" if request.language == "de" else "en",
                },
            )
        except httpx.HTTPError as error:
            raise CarrierError("unavailable", type(error).__name__) from error
        if response.status_code in (400, 404):
            raise CarrierError("not_found")  # no such number, or not a Hermes number at all
        if response.status_code == 429:
            raise CarrierError("rate_limited")
        if response.status_code >= 400:
            raise CarrierError("unavailable", f"HTTP {response.status_code}")
        log.debug("Hermes: %s", response.text[:4000])
        try:
            payload = response.json()
        except ValueError as error:
            log.warning("Hermes answered something else than JSON: %s", response.text[:300])
            raise CarrierError("invalid_response", "not JSON") from error
        result = parse_response(payload)
        postal_code = (request.extra.get("postal_code") or "").strip()
        if postal_code and result.status is Status.OUT_FOR_DELIVERY:
            await self._add_live(request.tracking_number, postal_code, result, http)
        return result

    async def _add_live(
        self, number: str, postal_code: str, result: TrackingResult, http: httpx.AsyncClient
    ) -> None:
        """Ask for the live round; whatever goes wrong here, the status itself stands."""
        try:
            response = await http.get(
                ADDRESS_URL + number,
                headers={"Accept": "application/json", "X-ZipCode": postal_code},
            )
        except httpx.HTTPError as error:
            log.warning("Hermes live round for %s failed: %s", number, type(error).__name__)
            return
        if response.status_code == 403:
            log.warning("Hermes does not accept the postal code entered for %s", number)
            return
        if response.status_code >= 400:
            log.warning("Hermes live round for %s: HTTP %s", number, response.status_code)
            return
        try:
            read_live(response.json(), result)
        except ValueError:
            log.warning("Hermes live round for %s was not JSON", number)


MODULE = HermesDe()
