"""Amazon.de orders, read with the amazon-orders library.

Amazon has no tracking API for customers, so this module signs in to the
account like a browser would and reads the order history. The library asks
amazon.de for English pages whatever language the account uses; the texts
parsed below are therefore the English ones.
"""

from __future__ import annotations

import contextlib
import importlib.util
import re
from datetime import date, datetime, timedelta
from typing import Any

from parcelmaster.carriers.base import (
    CarrierError,
    Field,
    ImportedParcel,
    ImportedShipment,
    ImporterModule,
    SyncRequest,
)
from parcelmaster.status import Status

DOMAIN = "amazon.de"

_MONTHS = {
    name: index
    for index, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

_RULES: tuple[tuple[re.Pattern[str], Status], ...] = (
    (re.compile(r"\b(return(ed)?|refund(ed)?)\b", re.I), Status.RETURNED),
    (
        re.compile(r"\b(delivered|handed (directly )?to|left (in|at|with))\b", re.I),
        Status.DELIVERED,
    ),
    (
        re.compile(r"ready for (pick-?up|collection)|available for (pick-?up|collection)", re.I),
        Status.READY_FOR_PICKUP,
    ),
    (re.compile(r"out for delivery", re.I), Status.OUT_FOR_DELIVERY),
    (
        re.compile(
            r"delayed|running late|undeliverable|may be lost|attempted|problem|unable to deliver",
            re.I,
        ),
        Status.PROBLEM,
    ),
    (
        re.compile(r"not yet (dispatched|shipped)|preparing|order (received|placed)", re.I),
        Status.REGISTERED,
    ),
    (
        re.compile(r"arriving|expected|dispatched|shipped|on (the|its) way|in transit", re.I),
        Status.IN_TRANSIT,
    ),
)


def _first_line(error: Exception) -> str:
    """The library's message without its hints for people at a terminal."""
    return str(error).split("\n-->")[0].strip()[:300]


def parse_status(text: str | None) -> Status:
    """Map Amazon's one-line delivery status onto the shared statuses."""
    if not text:
        return Status.UNKNOWN
    for pattern, status in _RULES:
        if pattern.search(text):
            return status
    return Status.UNKNOWN


def parse_eta(text: str | None, today: date) -> datetime | None:
    """The day a status like "Arriving tomorrow" or "Arriving 20 November" names."""
    if not text or not re.search(r"arriving|expected", text, re.I):
        return None
    lower = text.lower()

    def at(day: date) -> datetime:
        return datetime(day.year, day.month, day.day)

    if "today" in lower:
        return at(today)
    if "tomorrow" in lower:
        return at(today + timedelta(days=1))
    dated = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\b", lower) or None
    month_first = re.search(r"\b([a-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?\b", lower)
    for match, day_group, month_group in ((dated, 1, 2), (month_first, 2, 1)):
        if match and match.group(month_group) in _MONTHS:
            month = _MONTHS[match.group(month_group)]
            try:
                candidate = date(today.year, month, int(match.group(day_group)))
            except ValueError:
                continue
            # "Arriving 3 January" seen in December means next year.
            if candidate < today - timedelta(days=60):
                candidate = candidate.replace(year=today.year + 1)
            return at(candidate)
    for index, name in enumerate(_WEEKDAYS):
        if name in lower:
            return at(today + timedelta(days=(index - today.weekday()) % 7))
    return None


def to_shipments(orders: list[Any], tracking: dict[tuple[str, str], Any], today: date):
    """Turn the library's orders into what Parcel Master files.

    ``tracking`` maps (order number, shipment id) to the library's Tracking
    object for the shipments whose carrier page could be read.
    """
    shipments: list[ImportedShipment] = []
    for order in orders:
        number = getattr(order, "order_number", None)
        if not number or getattr(order, "cancelled", False):
            continue
        parcels: list[ImportedParcel] = []
        titles: list[str] = []
        for index, shipment in enumerate(getattr(order, "shipments", None) or [], start=1):
            text = (getattr(shipment, "delivery_status", None) or "").strip()
            shipment_id = getattr(shipment, "shipment_id", None)
            titles += [item.title for item in getattr(shipment, "items", []) if item.title]
            if not text and not shipment_id:
                continue  # Amazon no longer shows this shipment: long delivered
            found = tracking.get((number, shipment_id)) if shipment_id else None
            link = getattr(shipment, "tracking_link", None) or ""
            if link.startswith("/"):
                link = f"https://www.{DOMAIN}{link}"
            parcels.append(
                ImportedParcel(
                    key=shipment_id or f"{number}/{index}",
                    status=parse_status(text),
                    description=text,
                    eta_from=parse_eta(text, today),
                    carrier_name=getattr(found, "carrier", None),
                    tracking_number=getattr(found, "tracking_number", None),
                    url=link
                    or f"https://www.{DOMAIN}/gp/your-account/order-details?orderID={number}",
                )
            )
        if not parcels:
            continue
        if not titles:
            titles = [item.title for item in getattr(order, "items", []) if item.title]
        title = titles[0] if titles else ""
        if len(title) > 80:
            title = title[:77].rstrip() + "…"
        shipments.append(ImportedShipment(reference=number, title=title, parcels=parcels))
    return shipments


class AmazonDe(ImporterModule):
    id = "de.amazon"
    country = "de"
    name = "Amazon"
    settings_fields = (
        Field("email", required=True),
        Field("password", type="secret", required=True),
        Field("otp_secret", type="secret"),
    )
    sync_interval_minutes = 180

    def sync(self, request: SyncRequest) -> list[ImportedShipment]:
        # Imported here so that a missing or broken library only affects this module.
        from amazonauth.exception import (
            AmazonAuthChallengeUnsolvedError,
            AmazonAuthError,
            AmazonAuthSignInError,
        )
        from amazonauth.session import AmazonSession, IODefault
        from amazonorders.conf import AmazonOrdersConfig
        from amazonorders.exception import AmazonOrdersError
        from amazonorders.orders import AmazonOrders

        class NeedsPerson(Exception):
            pass

        class NoPrompt(IODefault):
            """There is nobody at a keyboard: a question means we cannot go on."""

            def echo(self, msg: str, **kwargs: Any) -> None:
                pass

            def prompt(self, msg: str, type: Any = None, **kwargs: Any) -> Any:
                raise NeedsPerson(msg)

        settings = request.settings
        request.state_dir.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if importlib.util.find_spec("playwright"):
            # Amazon sometimes answers the sign-in with a JavaScript check that a
            # plain HTTP client cannot run. The library then runs it in a headless
            # browser and carries the cookies back.
            data = {
                "browser": "chromium",
                "auth_forms_classes": [
                    "amazonauth.contrib.browser.playwright.PlaywrightAcicForm",
                    "amazonauth.contrib.browser.playwright.PlaywrightJSAuthForm",
                ],
            }
        config = AmazonOrdersConfig(
            config_path=str(request.state_dir / "config.yml"),
            data={
                **data,
                "domain": DOMAIN,
                "cookie_jar_path": str(request.state_dir / "cookies.json"),
                "output_dir": str(request.state_dir / "output"),
                "request_timeout": 30,
            },
        )
        session = AmazonSession(
            settings.get("email"),
            settings.get("password"),
            io=NoPrompt(),
            config=config,
            otp_secret_key=(settings.get("otp_secret") or "").replace(" ", "") or None,
        )
        try:
            session.login()
            client = AmazonOrders(session)
            orders = client.get_orders(time_filter="last30")
            tracking: dict[tuple[str, str], Any] = {}
            for order in orders:
                for shipment in order.shipments or []:
                    open_status = parse_status(shipment.delivery_status) not in (
                        Status.DELIVERED,
                        Status.RETURNED,
                    )
                    if shipment.shipment_id and open_status:
                        # Without the carrier page the status from the order list still stands.
                        with contextlib.suppress(AmazonOrdersError):
                            tracking[(order.order_number, shipment.shipment_id)] = (
                                client.get_tracking(order.order_number, shipment.shipment_id)
                            )
        except NeedsPerson as error:
            code = "needs_code" if "passcode" in str(error).lower() else "needs_person"
            raise CarrierError(code) from error
        except AmazonAuthChallengeUnsolvedError as error:
            raise CarrierError("needs_person", type(error).__name__) from error
        except AmazonAuthSignInError as error:
            # Wrong password, an unexpected page, a blocked address: Amazon's own
            # words are the only clue, so they are passed on.
            raise CarrierError("sign_in_failed", _first_line(error)) from error
        except (AmazonAuthError, AmazonOrdersError) as error:
            raise CarrierError("unavailable", _first_line(error)) from error
        return to_shipments(orders, tracking, date.today())


MODULE = AmazonDe()
