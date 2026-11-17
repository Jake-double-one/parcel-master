"""DHL Paket (Germany) via the official "Shipment Tracking - Unified" API."""

from __future__ import annotations

import re
from datetime import datetime
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
    settings_fields = (Field("api_key", type="secret", required=True),)
    parcel_fields = (Field("postal_code", placeholder="53113"),)
    default_daily_budget = 250
    pickup_hold_days = 7

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

    async def track(self, request: TrackRequest, http: httpx.AsyncClient) -> TrackingResult:
        api_key = request.settings.get("api_key")
        if not api_key:
            raise CarrierError("not_configured")
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
                headers={"DHL-API-Key": api_key, "Accept": "application/json"},
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


MODULE = DhlDe()
