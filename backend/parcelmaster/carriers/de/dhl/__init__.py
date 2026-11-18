"""DHL Paket (Germany).

Without an API key the module reads the public tracking of dhl.de, the same
answer the website shows. With a key it uses the official "Shipment Tracking -
Unified" API instead, which DHL hands out only on request.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from parcelmaster.carriers.base import (
    CarrierError,
    CarrierModule,
    Field,
    TrackingEvent,
    TrackingResult,
    TrackRequest,
)
from parcelmaster.status import Status

log = logging.getLogger(__name__)

API_URL = "https://api-eu.dhl.com/track/shipments"

_STATUS_CODES = {
    "pre-transit": Status.REGISTERED,
    "transit": Status.IN_TRANSIT,
    "delivered": Status.DELIVERED,
    "failure": Status.PROBLEM,
    "unknown": Status.UNKNOWN,
}

# The API only knows five coarse codes. The finer states are only visible in
# the event text, which is German or English depending on the request.
_OUT_FOR_DELIVERY = re.compile(
    r"zustellfahrzeug|in zustellung|wird (heute|voraussichtlich heute) zugestellt"
    r"|out for delivery|loaded onto the delivery vehicle|being delivered",
    re.I,
)
_READY_FOR_PICKUP = re.compile(
    r"zur abholung bereit|abholbereit|liegt (in|zur)[^.]{0,60}(abholung|bereit)"
    r"|ready for (pick-?up|collection)|available for pick-?up",
    re.I,
)
_RETURNED = re.compile(
    r"rücksendung|zurück an den absender|zurückgesandt|return(ed)? to (the )?sender"
    r"|being returned",
    re.I,
)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _locality(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    address = node.get("address")
    if isinstance(address, dict):
        return address.get("addressLocality") or None
    return None


def _status(node: dict[str, Any]) -> Status:
    text = " ".join(
        str(node.get(key) or "") for key in ("status", "description", "remark", "nextSteps")
    )
    coarse = _STATUS_CODES.get(str(node.get("statusCode") or "").lower(), Status.UNKNOWN)
    if coarse is Status.DELIVERED:
        return coarse
    if _RETURNED.search(text):
        return Status.RETURNED
    if _READY_FOR_PICKUP.search(text):
        return Status.READY_FOR_PICKUP
    if _OUT_FOR_DELIVERY.search(text):
        return Status.OUT_FOR_DELIVERY
    return coarse


def _description(node: dict[str, Any]) -> str:
    return str(node.get("description") or node.get("status") or "").strip()


def parse_response(payload: Any) -> TrackingResult:
    shipments = payload.get("shipments") if isinstance(payload, dict) else None
    if not isinstance(shipments, list) or not shipments or not isinstance(shipments[0], dict):
        raise CarrierError("invalid_response", "no shipment in response")
    shipment = shipments[0]

    events = [
        TrackingEvent(
            timestamp=_parse_time(event.get("timestamp")),
            status=_status(event),
            description=_description(event),
            location=_locality(event.get("location")),
        )
        for event in shipment.get("events") or []
        if isinstance(event, dict)
    ]

    current = shipment.get("status") if isinstance(shipment.get("status"), dict) else {}
    frame = shipment.get("estimatedDeliveryTimeFrame")
    frame = frame if isinstance(frame, dict) else {}
    estimate = _parse_time(shipment.get("estimatedTimeOfDelivery"))
    return TrackingResult(
        status=_status(current),
        description=_description(current),
        location=_locality(current.get("location")),
        events=events,
        eta_from=_parse_time(frame.get("estimatedFrom")) or estimate,
        eta_to=_parse_time(frame.get("estimatedThrough")) or estimate,
    )


WEB_URL = "https://www.dhl.de/int-verfolgen/data"
WEB_DAILY_BUDGET = 1000

_NOT_DELIVERED = re.compile(
    r"nicht zugestellt werden|nicht angetroffen|zustellung (war )?nicht möglich"
    r"|could not be delivered|delivery (was )?not possible|recipient (was )?not (present|home)",
    re.I,
)


def _web_time(value: Any, end_of_day: bool = False) -> datetime | None:
    """A time from the website; a bare date becomes the start or end of that day."""
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        day = date.fromisoformat(value)
        return datetime.combine(day, time(23, 59) if end_of_day else time(0, 0))
    return _parse_time(value)


def _web_status(text: str, progress: Any, delivered: bool, returning: bool) -> Status:
    if delivered:
        return Status.DELIVERED
    if returning or _RETURNED.search(text):
        return Status.RETURNED
    if _READY_FOR_PICKUP.search(text):
        return Status.READY_FOR_PICKUP
    if _OUT_FOR_DELIVERY.search(text):
        return Status.OUT_FOR_DELIVERY
    if _NOT_DELIVERED.search(text):
        return Status.PROBLEM
    if not isinstance(progress, int):
        return Status.IN_TRANSIT if text else Status.UNKNOWN
    # The website counts five steps: announced, on its way (2 and 3), on the van, delivered.
    if progress <= 1:
        return Status.REGISTERED
    if progress == 4:
        return Status.OUT_FOR_DELIVERY
    if progress >= 5:
        return Status.DELIVERED
    return Status.IN_TRANSIT


def parse_web_response(payload: Any) -> TrackingResult:
    """Maps the answer of the dhl.de tracking page onto the common model."""
    if isinstance(payload, dict) and (payload.get("rateLimited") or payload.get("isRateLimited")):
        raise CarrierError("rate_limited")
    shipments = payload.get("sendungen") if isinstance(payload, dict) else None
    if not isinstance(shipments, list) or not shipments or not isinstance(shipments[0], dict):
        raise CarrierError("invalid_response", "no shipment in response")
    shipment = shipments[0]
    details = shipment.get("sendungsdetails")
    if shipment.get("sendungNichtGefunden") or not isinstance(details, dict):
        raise CarrierError("not_found")
    history = details.get("sendungsverlauf")
    history = history if isinstance(history, dict) else {}
    delivered = bool(details.get("istZugestellt"))
    returning = bool(details.get("ruecksendung"))

    raw_events = [event for event in history.get("events") or [] if isinstance(event, dict)]
    events = []
    for index, event in enumerate(raw_events):
        text = str(event.get("status") or "").strip()
        last = index == len(raw_events) - 1
        events.append(
            TrackingEvent(
                timestamp=_parse_time(event.get("datum")),
                status=_web_status(
                    text,
                    history.get("fortschritt") if last else (1 if index == 0 else 2),
                    delivered and last,
                    bool(event.get("ruecksendung")),
                ),
                description=text,
                location=str(event.get("ort") or "").strip() or None,
            )
        )
    events.reverse()  # newest first, like everywhere else

    # The headline is short; the newest event says the same in a full sentence.
    description = str(history.get("status") or "").strip()
    if events and events[0].description:
        description = events[0].description
    status = _web_status(description, history.get("fortschritt"), delivered, returning)
    delivery = details.get("zustellung")
    delivery = delivery if isinstance(delivery, dict) else {}
    result = TrackingResult(
        status=status,
        description=description,
        location=events[0].location if events else None,
        events=events,
        eta_from=None if delivered else _web_time(delivery.get("zustellzeitfensterVon")),
        eta_to=None if delivered else _web_time(delivery.get("zustellzeitfensterBis"), True),
    )
    live = details.get("liveTracking")
    if isinstance(live, dict) and not delivered:
        result.stops_left, result.stops_about = live_stops(live)
        frame = live.get("timeframe")
        if isinstance(frame, dict) and _parse_time(frame.get("from")) and frame.get("to"):
            # The round's own window is closer to the truth than the planned one.
            result.eta_from = _parse_time(frame.get("from"))
            result.eta_to = _parse_time(frame.get("to")) or result.eta_to
    return result


def live_stops(live: dict[str, Any]) -> tuple[int | None, bool]:
    """Stops left before this parcel, and whether that is a lower bound.

    The page gets a key like "CD20" ("a bit more than 20 stops"), "CD01"
    ("one stop") or "CD02" ("only N stops") together with the number N.
    """
    if str(live.get("state") or "").lower() not in ("ok", "reduced"):
        return None, False
    match = re.fullmatch(r"CD(\d{2})", str(live.get("countdownTextKey") or ""))
    if not match:
        return None, False
    step = int(match.group(1))
    if step >= 10:
        return step, True
    if step == 1:
        return 1, False
    counts = [
        value
        for key, value in live.items()
        if isinstance(value, int)
        and not isinstance(value, bool)
        and re.search(r"count|stop", key, re.I)
        and 0 < value < 10
    ]
    # Without the number the key still says "fewer than ten".
    return (counts[0], False) if counts else (9, True)


#: How long one look at a delivery round serves every parcel on it.
ROUND_FRESH_SECONDS = 240
#: After a failed visit the browser is left alone for this long.
BROWSER_REST_SECONDS = 30 * 60
PAGE_URL = "https://www.dhl.de/de/privatkunden/pakete-empfangen/verfolgen.html"
_ROUNDS: dict[tuple[str, str, str], tuple[float, int | None, bool]] = {}
_BROWSER_RESTS_UNTIL = [0.0]


async def _visit_page(tracking_number: str, postal_code: str) -> TrackingResult | None:
    """Opens the tracking page like a visitor, enters the postal code and reads the answer.

    An ordinary browser, as it is: if the site does not serve it, there is no
    live tracking. Returns None for any failure.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None
    started = monotonic()
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(args=["--disable-dev-shm-usage"])
            try:
                page = await browser.new_page(locale="de-DE")
                page.set_default_timeout(20_000)
                await page.goto(
                    f"{PAGE_URL}?piececode={tracking_number}", wait_until="domcontentloaded"
                )
                field = page.locator('[data-testid="shipment-details"] form input').first
                await field.wait_for(state="visible")
                await field.fill(postal_code)
                async with page.expect_response(
                    lambda r: r.url.startswith(f"{WEB_URL}/shipment") and r.request.method == "POST"
                ) as answer:
                    await field.press("Enter")
                response = await answer.value
                if response.status != 200:
                    log.warning("dhl.de: the page's postal code step got HTTP %s", response.status)
                    return None
                payload = await response.json()
            finally:
                await browser.close()
        details = payload["sendungen"][0].get("sendungsdetails") or {}
        result = parse_web_response(payload)
        log.info(
            "dhl.de live %s: %s after %.1fs -> stops %s%s",
            tracking_number,
            {k: v for k, v in (details.get("liveTracking") or {}).items() if k != "position"},
            monotonic() - started,
            result.stops_left,
            "+" if result.stops_about else "",
        )
        return result
    except Exception as error:  # noqa: BLE001 - the plain answer stands without this
        log.warning(
            "dhl.de: the visit with the browser failed after %.1fs (%s: %s)",
            monotonic() - started,
            type(error).__name__,
            str(error).splitlines()[0][:200] if str(error) else "",
        )
        return None


class DhlDe(CarrierModule):
    id = "de.dhl"
    country = "de"
    name = "DHL"
    patterns = (
        re.compile(r"00\d{18}"),  # NVE / SSCC licence plate
        re.compile(r"\d{12}"),
        re.compile(r"\d{14}"),
        re.compile(r"JJD\d{10,26}"),
        re.compile(r"JVGL\d{10,22}"),
        re.compile(r"[A-Z]{2}\d{9}DE"),  # international, registered mail
    )
    settings_fields = (
        Field("api_key", type="secret"),
        Field("prefer", type="choice", options=("website", "api")),
        Field("live", type="choice", options=("on", "off")),
        Field("default_postal_code", placeholder="53113"),
    )
    parcel_fields = (Field("postal_code", placeholder="53113"),)
    default_daily_budget = 250
    pickup_hold_days = 7

    def budget(self, settings: dict[str, Any]) -> int | None:
        # The API allows 250 a day. The website names no number; this only
        # keeps a mistake from asking without end.
        return 250 if self._plan(settings)[0] == "api" else WEB_DAILY_BUDGET

    def scarce(self, settings: dict[str, Any]) -> bool:
        return self._plan(settings)[0] == "api"

    def source(self, settings: dict[str, Any]) -> str | None:
        return "_".join(self._plan(settings))

    def extract_from_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        if not re.search(r"(^|\.)dhl\.(de|com)$", parsed.hostname or ""):
            return None
        query = parse_qs(parsed.query)
        for key in ("piececode", "idc", "tracking-id", "trackingNumber", "AWB"):
            if query.get(key):
                return self.normalize(query[key][0])
        return None

    def tracking_url(self, tracking_number: str, extra: dict[str, str], language: str) -> str:
        lang = "de" if language == "de" else "en"
        url = (
            f"https://www.dhl.de/{lang}/privatkunden/pakete-empfangen/verfolgen.html"
            f"?piececode={tracking_number}"
        )
        if extra.get("postal_code"):
            url += f"&zip={extra['postal_code']}"
        return url

    @staticmethod
    def _plan(settings: dict[str, Any]) -> tuple[str, ...]:
        """The ways to ask, in the order to try them."""
        if not settings.get("api_key"):
            return ("website",)
        return ("api", "website") if settings.get("prefer") == "api" else ("website", "api")

    async def track(self, request: TrackRequest, http: httpx.AsyncClient) -> TrackingResult:
        first, *rest = self._plan(request.settings)
        ways = {"api": self._track_with_api, "website": self._track_on_website}
        try:
            return await ways[first](request, http)
        except CarrierError as error:
            # "Not found" is an answer; everything else is worth a try the other way.
            if not rest or error.code == "not_found":
                raise
            log.warning(
                "DHL %s failed for %s (%s%s); trying the %s",
                first,
                request.tracking_number,
                error.code,
                f": {error.detail}" if error.detail else "",
                rest[0],
            )
            return await ways[rest[0]](request, http)

    async def _track_with_api(
        self, request: TrackRequest, http: httpx.AsyncClient
    ) -> TrackingResult:
        params = {
            "trackingNumber": request.tracking_number,
            "requesterCountryCode": "DE",
            "language": "de" if request.language == "de" else "en",
        }
        if request.extra.get("postal_code"):
            # Without the recipient's postal code DHL withholds delivery details.
            params["recipientPostalCode"] = request.extra["postal_code"]
        try:
            response = await http.get(
                API_URL,
                params=params,
                headers={"DHL-API-Key": request.settings["api_key"], "Accept": "application/json"},
            )
        except httpx.HTTPError as error:
            raise CarrierError("unavailable", type(error).__name__) from error

        if response.status_code in (401, 403):
            raise CarrierError("unauthorized")
        if response.status_code == 404:
            raise CarrierError("not_found")
        if response.status_code == 429:
            raise CarrierError("rate_limited")
        if response.status_code >= 400:
            raise CarrierError("unavailable", f"HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise CarrierError("invalid_response", "not JSON") from error
        return parse_response(payload)

    async def _track_on_website(
        self, request: TrackRequest, http: httpx.AsyncClient
    ) -> TrackingResult:
        # One plain question. The page's second step, entering the recipient's
        # postal code for more detail, is refused to anything but the page itself.
        try:
            response = await http.get(
                f"{WEB_URL}/search",
                params={
                    "piececode": request.tracking_number,
                    "language": "de" if request.language == "de" else "en",
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as error:
            raise CarrierError("unavailable", type(error).__name__) from error
        result = parse_web_response(self._web_json(response))
        postal_code = (request.extra.get("postal_code") or "").strip()
        if (
            result.status is Status.OUT_FOR_DELIVERY
            and postal_code
            and request.settings.get("live") != "off"
        ):
            result = await self._on_the_round(request, postal_code, result)
        return result

    async def _on_the_round(
        self, request: TrackRequest, postal_code: str, plain: TrackingResult
    ) -> TrackingResult:
        """Adds what dhl.de shows on the delivery day once the postal code is entered.

        That is the number of stops left, and it is only handed to the page
        itself, so the page is opened in a browser. Parcels with the same
        postal code and delivery window ride in one van: one visit serves them
        all for a few minutes. If the visit fails, the plain answer stays and
        the browser rests for a while.
        """
        key = (postal_code, str(plain.eta_from), str(plain.eta_to))
        now = monotonic()
        seen = _ROUNDS.get(key)
        if seen and now - seen[0] < ROUND_FRESH_SECONDS:
            plain.stops_left, plain.stops_about = seen[1], seen[2]
        elif now >= _BROWSER_RESTS_UNTIL[0]:
            detailed = await _visit_page(request.tracking_number, postal_code)
            if detailed is None:
                _BROWSER_RESTS_UNTIL[0] = now + BROWSER_REST_SECONDS
            else:
                _ROUNDS[key] = (now, detailed.stops_left, detailed.stops_about)
                for old in [k for k, v in _ROUNDS.items() if now - v[0] > 3600]:
                    del _ROUNDS[old]
                plain = detailed
        if plain.stops_left is not None:
            started = plain.eta_from is None or plain.eta_from <= datetime.now(
                plain.eta_from.tzinfo
            ) + timedelta(minutes=15)
            if started or plain.stops_left <= 20:
                plain.again_in_minutes = 5  # the van is close: follow it
        return plain

    @staticmethod
    def _web_json(response: httpx.Response) -> Any:
        kind = response.headers.get("content-type", "")
        if response.status_code >= 300 or "json" not in kind:
            log.warning(
                "dhl.de answered %s %s with HTTP %s (%s, server %s): %r",
                response.request.method,
                response.request.url.path,
                response.status_code,
                kind or "no content type",
                response.headers.get("server", "?"),
                response.text[:300],
            )
        if response.status_code == 429:
            raise CarrierError("rate_limited")
        if response.status_code >= 300:
            # The site also answers 404, or a redirect to an error page, when it
            # turns an automated request away.
            raise CarrierError("unavailable", f"dhl.de: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise CarrierError("invalid_response", "dhl.de: not JSON") from error
        log.debug("dhl.de %s: %s", response.request.url.path, response.text[:4000])
        return payload


MODULE = DhlDe()
