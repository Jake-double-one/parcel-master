"""DHL Paket (Germany).

Without an API key the module reads the public tracking of dhl.de, the same
answer the website shows. With a key it uses the official "Shipment Tracking -
Unified" API instead, which DHL hands out only on request.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree
from xml.sax.saxutils import quoteattr
from zoneinfo import ZoneInfo

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


#: A delivery window this short means the van is close.
NARROW_WINDOW = timedelta(minutes=30)
PARCEL_DE_URL = "https://api-eu.dhl.com/parcel/de/tracking/v0/shipments"
_DELIVERED = re.compile(
    r"wurde (erfolgreich )?zugestellt|zustellung erfolgreich|has been (successfully )?delivered"
    r"|delivery successful",
    re.I,
)


def round_pace(start: datetime | None, end: datetime | None, now: datetime) -> float:
    """Minutes until a parcel on the van is asked about again.

    This API allows millions of lookups a day, so the day of delivery is
    followed closely: the window moves while the van is on its round, and
    DHL narrows it as the van comes near.
    """
    has_window = bool(start and end and end - start < timedelta(hours=12))
    if not has_window:
        return 10  # no window yet: watch for one
    if now > end + timedelta(hours=2):
        return 10  # long overdue: something else is going on
    if end - start <= NARROW_WINDOW and now >= start - timedelta(minutes=15):
        return 1  # DHL has narrowed it down: the van is nearly there
    if now >= start - timedelta(minutes=30):
        return 5
    return 10


def _local_time(value: Any) -> datetime | None:
    """A time as Parcel DE Tracking writes it: "18.11.2026 10:18", local to Germany."""
    try:
        return datetime.strptime(str(value or "").strip(), "%d.%m.%Y %H:%M")
    except ValueError:
        return None


def _parcel_de_status(node: Any, text: str) -> Status:
    if node.get("delivery-event-flag") == "1" or _DELIVERED.search(text):
        return Status.DELIVERED
    if node.get("ruecksendung") == "true":
        return Status.RETURNED
    if node.get("standard-event-code") == "VA":
        return Status.REGISTERED  # only announced by the sender so far
    return _web_status(text, None, False, False)


def parse_parcel_de(text: str) -> TrackingResult:
    """Maps the XML of "Parcel DE Tracking" (public status) onto the common model."""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        raise CarrierError("invalid_response", "not XML") from error
    piece = next(
        (node for node in root.iter("data") if node.get("name") == "piece-status-public"), None
    )
    if piece is None or piece.get("error-status") not in (None, "", "0"):
        raise CarrierError("not_found")
    events = [
        TrackingEvent(
            timestamp=_local_time(node.get("event-timestamp")),
            status=_parcel_de_status(node, node.get("event-status") or ""),
            description=(node.get("event-status") or node.get("event-text") or "").strip(),
            location=(node.get("event-location") or "").strip() or None,
        )
        for node in piece.iter("data")
        if node.get("name") == "piece-event"
    ]
    events.reverse()  # newest first, like everywhere else
    headline = (piece.get("status") or "").strip()
    description = events[0].description if events and events[0].description else headline
    status = _parcel_de_status(piece, f"{headline} {description}")
    eta_from = eta_to = None
    try:
        day = datetime.strptime(piece.get("delivery-date") or "", "%d.%m.%Y").date()
    except ValueError:
        day = None
    if day and status is not Status.DELIVERED:
        start = _local_time(f"{day:%d.%m.%Y} {piece.get('delivery-timeframe-from')}")
        end = _local_time(f"{day:%d.%m.%Y} {piece.get('delivery-timeframe-to')}")
        eta_from = start or datetime.combine(day, time(0, 0))
        eta_to = end or datetime.combine(day, time(23, 59))
    return TrackingResult(
        status=status,
        description=description,
        location=events[0].location if events else None,
        events=events,
        eta_from=eta_from,
        eta_to=eta_to,
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
#: From this many stops on, the round is looked at every minute.
CLOSE_STOPS = 5
CLOSE_FRESH_SECONDS = 45
#: After a failed visit the browser is left alone for this long.
BROWSER_REST_SECONDS = 30 * 60
PAGE_URL = "https://www.dhl.de/de/privatkunden/pakete-empfangen/verfolgen.html"
_ROUNDS: dict[tuple[str, str, str], tuple[float, int | None, bool]] = {}
_BROWSER_RESTS_UNTIL = [0.0]


def _browser_client_hints(version: str) -> tuple[str, str]:
    """Chrome user agent and sec-ch-ua for this Chromium, without HeadlessChrome.

    The edge in front of dhl.de answers 404 (an Apache HTML page) when the
    user agent is missing or the brand is HeadlessChrome. A normal Chrome
    brand is handed the tracking JSON.
    """
    major = version.split(".", 1)[0] if version else "131"
    full = version or f"{major}.0.0.0"
    user_agent = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{full} Safari/537.36"
    )
    sec_ch_ua = f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not_A Brand";v="24"'
    return user_agent, sec_ch_ua


async def _align_client_hints(page: Any, user_agent: str, version: str) -> None:
    """Tell the page the same Chrome brand the requests already send.

    Replacing the user-agent string is not enough: headless Chromium still
    puts HeadlessChrome in navigator.userAgentData, and in sec-ch-ua, unless
    the brand list is overridden too.
    """
    major = version.split(".", 1)[0] if version else "131"
    full = version or f"{major}.0.0.0"
    try:
        client = await page.context.new_cdp_session(page)
        await client.send(
            "Network.setUserAgentOverride",
            {
                "userAgent": user_agent,
                "acceptLanguage": "de-DE,de;q=0.9,en;q=0.8",
                "platform": "Linux",
                "userAgentMetadata": {
                    "brands": [
                        {"brand": "Chromium", "version": major},
                        {"brand": "Google Chrome", "version": major},
                        {"brand": "Not_A Brand", "version": "24"},
                    ],
                    "fullVersionList": [
                        {"brand": "Chromium", "version": full},
                        {"brand": "Google Chrome", "version": full},
                        {"brand": "Not_A Brand", "version": "24.0.0.0"},
                    ],
                    "fullVersion": full,
                    "platform": "Linux",
                    "platformVersion": "6.8.0",
                    "architecture": "x86",
                    "model": "",
                    "mobile": False,
                    "bitness": "64",
                    "wow64": False,
                },
            },
        )
    except Exception:  # noqa: BLE001 - the request headers already carry the brand
        return


async def _fetch_shipment(
    page: Any, tracking_number: str, postal_code: str, seen: list[str]
) -> dict[str, Any] | None:
    """POST /shipment the way the page's own script does, from inside the page.

    The postal-code field is only rendered when DHL asks for one. The request
    is still the same: piececode, zip, international, and the CSRF token the
    config (and the search answer) left on the page.
    """
    seen.append("in-page shipment")
    try:
        posted = await page.evaluate(
            """async ({ piece, zip }) => {
                const state = window.__INITIAL_APP_STATE__;
                const cfg = (state && state.config) || {};
                const base = cfg.verfolgenDataPath || "/int-verfolgen/data";
                const language = cfg.currentLanguage || "de";
                const url = base + "/shipment?language=" + encodeURIComponent(language);
                const response = await fetch(url, {
                    method: "POST",
                    credentials: "include",
                    headers: {
                        Accept: "application/json",
                        "Content-Type": "application/json",
                        "verfolgen-CSRF-token": cfg.verfolgenCsrfToken || ""
                    },
                    body: JSON.stringify({ piececode: piece, zip: zip, international: false })
                });
                const text = await response.text();
                let payload = null;
                try { payload = JSON.parse(text); } catch (e) { payload = null; }
                const error = payload && payload.error ? String(payload.error) : "";
                return { status: response.status, payload, error };
            }""",
            {"piece": tracking_number, "zip": postal_code},
        )
    except Exception as error:  # noqa: BLE001 - the caller logs the visit and returns None
        seen.append(f"in-page shipment failed {type(error).__name__}")
        return None
    status = posted.get("status") if isinstance(posted, dict) else None
    if status != 200:
        if isinstance(posted, dict) and posted.get("error"):
            seen.append(f"shipment {status} {str(posted['error'])[:120]}")
        log.warning("dhl.de: the page's postal code step got HTTP %s", status)
        return None
    payload = posted.get("payload") if isinstance(posted, dict) else None
    if not isinstance(payload, dict):
        seen.append("shipment not json")
        return None
    return payload


async def _shipment_payload(
    page: Any, tracking_number: str, postal_code: str, seen: list[str]
) -> dict[str, Any] | None:
    """Loads the tracking page and returns the postal-code JSON, or None."""
    try:
        # Listen before navigation: the search often returns before goto resolves.
        async with page.expect_response(
            lambda response: (
                "/int-verfolgen/data/search" in response.url and response.status == 200
            ),
            timeout=20_000,
        ):
            await page.goto(
                f"{PAGE_URL}?piececode={tracking_number}", wait_until="domcontentloaded"
            )
            # A visitor first answers the question about cookies: only the necessary ones.
            consent = page.get_by_text("Nur notwendige Technologien").first
            try:
                await consent.click(timeout=5_000)
            except Exception:  # noqa: BLE001 - no banner, no click
                seen.append("no consent banner")
    except Exception as error:  # noqa: BLE001 - the in-page request below may still answer
        seen.append(f"search not ok {type(error).__name__}")

    field = page.locator('[data-testid="shipment-details"] form input').first
    try:
        # The widget paints the field only after a search it accepts, and only
        # when this shipment actually asks for a postal code.
        await field.wait_for(state="visible", timeout=5_000)
    except Exception:  # noqa: BLE001 - ask the endpoint ourselves
        seen.append("no postal code field")
        return await _fetch_shipment(page, tracking_number, postal_code, seen)

    try:
        await field.fill(postal_code)
        async with page.expect_response(
            lambda response: (
                response.url.startswith(f"{WEB_URL}/shipment") and response.request.method == "POST"
            )
        ) as answer:
            await field.press("Enter")
        response = await answer.value
    except Exception:  # noqa: BLE001 - the form did not post; one fetch, not a second post
        seen.append("postal code form did not answer")
        if any(item.startswith(("got shipment", "asked shipment")) for item in seen):
            return None
        return await _fetch_shipment(page, tracking_number, postal_code, seen)
    if response.status != 200:
        log.warning("dhl.de: the page's postal code step got HTTP %s", response.status)
        return None
    return await response.json()


async def _visit_page(tracking_number: str, postal_code: str) -> TrackingResult | None:
    """Opens the tracking page like a visitor, enters the postal code and reads the answer.

    Headless Chromium announces itself, and dhl.de then answers 404, so the
    browser is presented as Chrome. Returns None for any failure.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None
    started = monotonic()
    seen: list[str] = []  # what the page asked DHL and how that went, for the log

    def note(prefix: str, url: str, outcome: Any) -> None:
        if "/int-verfolgen/data/" in url and len(seen) < 30:
            seen.append(f"{prefix} {url.split('/int-verfolgen/data/')[1].split('?')[0]} {outcome}")

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                args=["--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
            )
            try:
                version = getattr(browser, "version", None) or "131.0.0.0"
                user_agent, sec_ch_ua = _browser_client_hints(version)
                context = await browser.new_context(
                    user_agent=user_agent,
                    locale="de-DE",
                    timezone_id="Europe/Berlin",
                    viewport={"width": 1366, "height": 768},
                    extra_http_headers={
                        "sec-ch-ua": sec_ch_ua,
                        "sec-ch-ua-mobile": "?0",
                        "sec-ch-ua-platform": '"Linux"',
                    },
                )
                page = await context.new_page()
                page.set_default_timeout(20_000)
                await page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => false})"
                )
                await _align_client_hints(page, user_agent, version)
                page.on("request", lambda request: note("asked", request.url, ""))
                page.on("response", lambda response: note("got", response.url, response.status))
                page.on(
                    "requestfailed",
                    lambda request: note("lost", request.url, request.failure or ""),
                )
                payload = await _shipment_payload(page, tracking_number, postal_code, seen)
                if payload is None:
                    widget = page.locator("#nolp")
                    shown = (await widget.inner_text())[:200] if await widget.count() else "none"
                    account = await page.evaluate("typeof window.ciam")
                    log.warning(
                        "dhl.de: the page showed no shipment in the browser. Requests: %s. "
                        "Tracking widget: %r. Account script: %s",
                        "; ".join(seen) or "none",
                        " ".join(shown.split()),
                        account,
                    )
                    return None
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
        Field("api_secret", type="secret"),
        Field("prefer", type="choice", options=("website", "api")),
        Field("live", type="choice", options=("off", "on")),
        Field("default_postal_code", placeholder="53113"),
    )
    parcel_fields = (Field("postal_code", placeholder="53113"),)
    default_daily_budget = 250
    pickup_hold_days = 7

    def budget(self, settings: dict[str, Any]) -> int | None:
        # The API allows 250 a day. The website names no number; this only
        # keeps a mistake from asking without end.
        return 250 if self._unified_first(settings) else WEB_DAILY_BUDGET

    def scarce(self, settings: dict[str, Any]) -> bool:
        return self._unified_first(settings)

    def _unified_first(self, settings: dict[str, Any]) -> bool:
        """Whether the API with the tight quota of 250 a day is asked first.

        "Parcel DE Tracking" names no such quota; if it does slow us down,
        the module falls back to dhl.de.
        """
        return self._plan(settings)[0] == "api" and not settings.get("api_secret")

    def companion_script(self) -> str | None:
        return (Path(__file__).parent / "companion.user.js").read_text(encoding="utf-8")

    def take_from_companion(self, payload: Any) -> list[tuple[str, TrackingResult]]:
        """What the tracking page in the user's browser got from DHL, one parcel each."""
        found = []
        pieces = payload.get("sendungen") if isinstance(payload, dict) else None
        for piece in pieces if isinstance(pieces, list) else []:
            if not isinstance(piece, dict) or not isinstance(piece.get("id"), str):
                continue
            try:
                result = parse_web_response({"sendungen": [piece]})
            except CarrierError:
                continue
            if result.stops_left is not None:
                pace = 1 if result.stops_left <= CLOSE_STOPS else 5
                result.again_in_minutes = pace
            found.append((self.normalize(piece["id"]), result))
        return found

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
        result = await self._ask(request, http)
        postal_code = (request.extra.get("postal_code") or "").strip()
        if (
            result.status is Status.OUT_FOR_DELIVERY
            and postal_code
            and request.settings.get("live") == "on"
        ):
            # Whichever way answered: the stops are only on the page itself.
            result = await self._on_the_round(request, postal_code, result)
        return result

    async def _ask(self, request: TrackRequest, http: httpx.AsyncClient) -> TrackingResult:
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
        if request.settings.get("api_secret"):
            return await self._track_with_parcel_de(request, http)
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

    async def _track_with_parcel_de(
        self, request: TrackRequest, http: httpx.AsyncClient
    ) -> TrackingResult:
        """ "Parcel DE Tracking", the API of DHL Paket: the app's key and secret are enough
        for the status every recipient may see."""
        piece = f"piece-code={quoteattr(request.tracking_number)}"
        if request.extra.get("postal_code"):
            # With the recipient's postal code the events name their places.
            piece += f" zip-code={quoteattr(request.extra['postal_code'].strip())}"
        language = "de" if request.language == "de" else "en"
        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="no"?>'
            f'<data appname="" language-code="{language}" password="" '
            f'request="get-status-for-public-user"><data {piece}/></data>'
        )
        try:
            response = await http.get(
                PARCEL_DE_URL,
                params={"xml": xml},
                headers={"Accept": "application/xml"},
                auth=(request.settings["api_key"], request.settings["api_secret"]),
            )
        except httpx.HTTPError as error:
            raise CarrierError("unavailable", type(error).__name__) from error
        if response.status_code in (401, 403):
            raise CarrierError("unauthorized")
        if response.status_code == 429:
            raise CarrierError("rate_limited")
        if response.status_code >= 300:
            log.warning(
                "DHL Parcel DE Tracking answered HTTP %s: %r",
                response.status_code,
                response.text[:300],
            )
            raise CarrierError("unavailable", f"HTTP {response.status_code}")
        log.debug("DHL Parcel DE Tracking: %s", response.text[:4000])
        result = parse_parcel_de(response.text)
        if result.status is Status.OUT_FOR_DELIVERY:
            now = datetime.now(ZoneInfo("Europe/Berlin")).replace(tzinfo=None)
            result.again_in_minutes = round_pace(result.eta_from, result.eta_to, now)
        return result

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
        return parse_web_response(self._web_json(response))

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
        # Close to the door the picture changes by the minute, as on the page itself.
        close = seen is not None and seen[1] is not None and seen[1] <= CLOSE_STOPS
        if seen and now - seen[0] < (CLOSE_FRESH_SECONDS if close else ROUND_FRESH_SECONDS):
            plain.stops_left, plain.stops_about = seen[1], seen[2]
        elif now >= _BROWSER_RESTS_UNTIL[0]:
            detailed = await _visit_page(request.tracking_number, postal_code)
            if detailed is None:
                _BROWSER_RESTS_UNTIL[0] = now + BROWSER_REST_SECONDS
            else:
                _ROUNDS[key] = (now, detailed.stops_left, detailed.stops_about)
                for old in [k for k, v in _ROUNDS.items() if now - v[0] > 3600]:
                    del _ROUNDS[old]
                plain.stops_left, plain.stops_about = detailed.stops_left, detailed.stops_about
        if plain.stops_left is not None:
            started = plain.eta_from is None or plain.eta_from <= datetime.now(
                plain.eta_from.tzinfo
            ) + timedelta(minutes=15)
            pace = 1 if plain.stops_left <= CLOSE_STOPS else 5
            if pace == 1 or started or plain.stops_left <= 20:
                # Almost there, or on its way to us: follow it, never slower than already planned.
                plain.again_in_minutes = min(pace, plain.again_in_minutes or pace)
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
