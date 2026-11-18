"""Amazon.de orders, read with the amazon-orders library.

Amazon has no tracking API for customers, so this module signs in to the
account like a browser would and reads the order history. The library asks
amazon.de for English pages whatever language the account uses; the texts
parsed below are therefore the English ones.
"""

from __future__ import annotations

import importlib.util
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

from parcelmaster.carriers.base import (
    CarrierError,
    Field,
    ImportedParcel,
    ImportedShipment,
    ImporterModule,
    SyncRequest,
    TrackingEvent,
)
from parcelmaster.status import Status

log = logging.getLogger(__name__)

DOMAIN = "amazon.de"
ORDER_NUMBER = re.compile(r"\d{3}-\d{7}-\d{7}")
#: Upper limit of tracking pages read per sync, to stay a polite visitor.
MAX_TRACKING_PAGES = 40

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


def _prepare_browser() -> None:
    """Make the library's browser start reliably inside a container.

    Containers give Chromium a tiny /dev/shm by default, and pages crash once
    it is full. The library offers no setting for launch arguments, so the
    one place where it builds them is wrapped.
    """
    from amazonauth.contrib.browser.playwright import PlaywrightBrowserLauncher

    if getattr(PlaywrightBrowserLauncher, "_parcelmaster_prepared", False):
        return
    original = PlaywrightBrowserLauncher._launch_options

    def launch_options(headless: bool, args: Any, env: Any) -> dict[str, Any]:
        options = original(headless, args, env)
        options["args"] = [*(options.get("args") or []), "--disable-dev-shm-usage"]
        return options

    PlaywrightBrowserLauncher._launch_options = staticmethod(launch_options)
    PlaywrightBrowserLauncher._parcelmaster_prepared = True


def clean_otp_secret(value: str | None) -> str | None:
    """The authenticator secret in the form the library needs, or None if empty.

    Accepts the key as Amazon shows it (groups of letters and digits) and a
    whole otpauth:// link. Anything else, such as a six-digit code, is
    rejected before a sign-in is attempted.
    """
    text = (value or "").strip()
    if not text:
        return None
    if text.lower().startswith("otpauth://"):
        text = (parse_qs(urlparse(text).query).get("secret") or [""])[0]
    secret = re.sub(r"[\s\-]", "", text).upper().rstrip("=")
    if len(secret) < 16 or not re.fullmatch(r"[A-Z2-7]+", secret):
        raise CarrierError("invalid_otp_secret")
    return secret


def _first_line(error: Exception) -> str:
    """The library's message without its hints for people at a terminal."""
    text = str(error).split("\n-->")[0].strip()
    return (text.splitlines()[0] if text else type(error).__name__)[:300]


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


_DAY = r"(?:\d{1,2}(?:st|nd|rd|th)? [a-z]{3,9}|[a-z]{3,9} \d{1,2}(?:st|nd|rd|th)?)"
_WHEN = rf"(today|tomorrow|yesterday|{'|'.join(_WEEKDAYS)}|(?:[a-z]+, )?{_DAY})"
_BY = r"(?: by (\d{1,2})(?::(\d{2}))? ?([ap])\.?m\.?)?"

#: Amazon's wording, lower-cased, and the phrase it stands for. The German
#: (or any other) wording lives in this module's locale files.
_PHRASES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), key)
    for pattern, key in (
        (rf"delivered {_WHEN}", "delivered_on"),
        (r"delivered", "delivered"),
        (r"(?:your )?package (?:was )?delivered", "delivered"),
        (rf"(?:now )?arriving {_WHEN}{_BY}", "arriving"),
        (rf"(?:now )?arriving {_WHEN} - {_WHEN}", "arriving_between"),
        (rf"(?:now )?expected (?:by )?{_WHEN}", "expected"),
        (r"out for delivery", "out_for_delivery"),
        (r"not yet (?:dispatched|shipped)", "not_dispatched"),
        (r"preparing for (?:dispatch|shipment)", "preparing"),
        (r"(?:dispatched|shipped)", "dispatched"),
        (r"on the way", "on_the_way"),
        (r"on the way,? but running late", "late"),
        (r"running late", "late"),
        (r"delayed", "delayed"),
        (r"delivery attempted", "attempted"),
        (r"ready for (?:pick-?up|collection)", "pickup"),
        (r"refunded", "refunded"),
        (r"return (?:complete|received)", "return_complete"),
        (r"return started", "return_started"),
        (r"carrier picked up the package", "picked_up"),
        (r"package arrived at (?:a|the) carrier facility", "arrived_carrier"),
        (r"package (?:has )?left the carrier facility", "left_carrier"),
        (r"package arrived at an amazon facility", "arrived_amazon"),
        (r"package (?:has )?left an amazon facility", "left_amazon"),
        (
            r"package (?:has )?left (?:the )?seller facility and is in transit to carrier",
            "left_seller",
        ),
        (r"package (?:was )?handed (?:directly )?to (?:a )?(?:resident|customer)", "handed_over"),
        (r"package (?:was )?left (?:in|at|near|with) .+", "left_somewhere"),
    )
)


def _when(text: str, phrase: Any, plain: bool = False) -> str | None:
    """A day as Amazon writes it ("tomorrow", "thursday", "17 november") in the user's words.

    ``plain`` asks for the bare date ("20. November") for use inside a range.
    """
    text = re.sub(r"^[a-z]+, ", "", text)
    if text in ("today", "tomorrow", "yesterday") or text in _WEEKDAYS:
        return phrase(f"word.{text}")
    match = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? ([a-z]{3,9})", text)
    day, month = (match.group(1), match.group(2)) if match else (None, None)
    if not match:
        match = re.fullmatch(r"([a-z]{3,9}) (\d{1,2})(?:st|nd|rd|th)?", text)
        if not match:
            return None
        day, month = match.group(2), match.group(1)
    if month not in _MONTHS:
        return None
    template = (phrase("word.date_plain") if plain else None) or phrase("word.date")
    name = phrase(f"word.month{_MONTHS[month]}")
    return template.replace("{day}", day).replace("{month}", name) if template and name else None


def translate(text: str, phrase: Any) -> str:
    """Amazon's English status line in the user's language; unknown lines stay as they are."""
    line = " ".join(text.lower().split()).rstrip(".")
    for pattern, key in _PHRASES:
        match = pattern.fullmatch(line)
        if not match:
            continue
        template = phrase(f"phrase.{key}")
        if not template:
            return text
        groups = match.groups()
        plain = "{from}" in template
        days = [_when(g, phrase, plain) for g in groups[:2] if g and not g.isdigit() and len(g) > 1]
        if "{when}" in template or "{from}" in template:
            if not days or any(day is None for day in days):
                return text
            template = template.replace("{when}", days[0]).replace("{from}", days[0])
            template = template.replace("{to}", days[-1])
        if key == "arriving" and groups[1]:
            hour = int(groups[1]) % 12 + (12 if groups[3] == "p" else 0)
            by = phrase("phrase.by")
            if by:
                template += " " + by.replace("{time}", f"{hour}:{groups[2] or '00'}")
        return template[:1].upper() + template[1:]
    return text


def _has_class(tag: Any, name: str) -> bool:
    return any(name in value for value in (tag.get("class") or []))


def _clock(text: str) -> tuple[int, int] | None:
    match = re.search(r"\b(\d{1,2})[:.](\d{2})\s*([ap])?\.?m?\b", text.lower())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if match.group(3) == "p" and hour < 12:
        hour += 12
    if match.group(3) == "a" and hour == 12:
        hour = 0
    return (hour, minute) if hour < 24 and minute < 60 else None


def _event_day(text: str, today: date) -> date | None:
    """The day a date heading of the tracking page names ("Tuesday, 17 November")."""
    lower = text.lower()
    if "today" in lower:
        return today
    if "yesterday" in lower:
        return today - timedelta(days=1)
    for pattern, day_group, month_group in (
        (r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\b", 1, 2),
        (r"\b([a-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?\b", 2, 1),
    ):
        match = re.search(pattern, lower)
        if match and match.group(month_group) in _MONTHS:
            try:
                day = date(
                    today.year, _MONTHS[match.group(month_group)], int(match.group(day_group))
                )
            except ValueError:
                continue
            # Events lie in the past: a date still ahead belongs to last year.
            return day if day <= today + timedelta(days=1) else day.replace(year=today.year - 1)
    return None


def parse_events(page: Any, today: date) -> list[TrackingEvent] | None:
    """The history on Amazon's tracking page, oldest first; None if it shows none.

    The page lists days as headings, each followed by rows of time, message
    and place. Elements are recognised by the "tracking-event-…" part of
    their class names.
    """
    if page is None:
        return None
    events: list[TrackingEvent] = []
    day: date | None = None
    pending_time: tuple[int, int] | None = None
    for tag in page.find_all(True):
        text = " ".join(tag.get_text(" ", strip=True).split())
        if not text:
            continue
        if _has_class(tag, "tracking-event-date"):
            day = _event_day(text, today) or day
            pending_time = None
        elif _has_class(tag, "tracking-event-time"):
            pending_time = _clock(text)
        elif _has_class(tag, "tracking-event-message"):
            when = None
            if day:
                hour, minute = pending_time or (0, 0)
                when = datetime(day.year, day.month, day.day, hour, minute)
            events.append(TrackingEvent(when, parse_status(text), text))
            pending_time = None
        elif _has_class(tag, "tracking-event-location") and events and not events[-1].location:
            events[-1].location = text
    if not events:
        return None
    # A line like "Package arrived at a carrier facility" matches no status rule.
    for event in events:
        if event.status is Status.UNKNOWN:
            event.status = Status.IN_TRANSIT
    return sorted(events, key=lambda event: event.timestamp or datetime.min)


def parse_delivery_day(text: str, today: date) -> datetime | None:
    """The day in a line like "Delivered today" or "Delivered 14 November"."""
    if not re.match(r"\s*delivered\b", text, re.I):
        return None
    day = _event_day(text, today)
    return datetime(day.year, day.month, day.day) if day else None


def describe_item(item: Any) -> str:
    title = " ".join(str(getattr(item, "title", "") or "").split())
    if len(title) > 120:
        title = title[:117].rstrip() + "…"
    quantity = getattr(item, "quantity", None)
    return f"{quantity} × {title}" if isinstance(quantity, int) and quantity > 1 else title


def _link_shape(link: str | None) -> str:
    """A tracking link without its values: enough to see which kind Amazon handed out."""
    if not link:
        return "no link"
    parsed = urlparse(link)
    return f"{parsed.path}?{'&'.join(sorted(parse_qs(parsed.query)))}"


def _read_tracking(client: Any, session: Any, order_number: str, shipment: Any) -> Any:
    """The carrier page of one parcel, or None if Amazon shows none that can be read.

    The library asks by shipment id. Some links carry none; their page is then
    fetched as it is and read with the same parser.
    """
    link = getattr(shipment, "tracking_link", None)
    shape = _link_shape(link)
    found = None
    try:
        if shipment.shipment_id:
            found = client.get_tracking(order_number, shipment.shipment_id)
        elif link:
            url = f"https://www.{DOMAIN}{link}" if link.startswith("/") else link
            if urlparse(url).hostname == f"www.{DOMAIN}":
                response = session.get(url)
                found = client._parse_tracking(response.parsed, client.config)
    except Exception as error:  # noqa: BLE001 - a parcel without its page is still a parcel
        log.warning(
            "amazon %s: tracking page (%s) failed: %s", order_number, shape, _first_line(error)
        )
        return None
    log.info(
        "amazon %s: tracking link %s, shipment id %s -> carrier %r, number %r, %d history lines",
        order_number,
        shape,
        "yes" if shipment.shipment_id else "no",
        getattr(found, "carrier", None),
        getattr(found, "tracking_number", None),
        len(parse_events(getattr(found, "parsed", None), date.today()) or []),
    )
    return found


def to_shipments(
    orders: list[Any],
    tracking: dict[tuple[str, str], Any],
    today: date,
    keep_empty: bool = False,
):
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
            found = tracking.get((number, shipment_id or f"{number}/{index}"))
            link = getattr(shipment, "tracking_link", None) or ""
            if link.startswith("/"):
                link = f"https://www.{DOMAIN}{link}"
            parcels.append(
                ImportedParcel(
                    key=shipment_id or f"{number}/{index}",
                    status=parse_status(text),
                    description=text,
                    eta_from=parse_eta(text, today),
                    status_time=parse_delivery_day(text, today),
                    carrier_name=getattr(found, "carrier", None),
                    tracking_number=getattr(found, "tracking_number", None),
                    url=link
                    or f"https://www.{DOMAIN}/gp/your-account/order-details?orderID={number}",
                    items=[
                        describe_item(item)
                        for item in getattr(shipment, "items", [])
                        if getattr(item, "title", None)
                    ],
                    events=parse_events(getattr(found, "parsed", None), today),
                )
            )
        if not titles:
            titles = [item.title for item in getattr(order, "items", []) if item.title]
        title = titles[0] if titles else ""
        if len(title) > 80:
            title = title[:77].rstrip() + "…"
        if not parcels:
            if not keep_empty:
                continue
            # Amazon shows no parcel for an old order any more: it arrived long ago.
            parcels.append(
                ImportedParcel(
                    key=f"{number}/1",
                    status=Status.DELIVERED,
                    url=f"https://www.{DOMAIN}/gp/your-account/order-details?orderID={number}",
                    items=[describe_item(i) for i in getattr(order, "items", []) if i.title],
                )
            )
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
    reference_pattern = ORDER_NUMBER

    def localize(self, text: str, phrase: Any) -> str:
        return translate(text, phrase)

    def extract_from_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        if not (parsed.hostname or "").endswith(DOMAIN):
            return None
        match = ORDER_NUMBER.search(parsed.query) or ORDER_NUMBER.search(parsed.path)
        return match.group(0) if match else None

    def sync(self, request: SyncRequest) -> list[ImportedShipment]:
        def work(client: Any, errors: Any) -> list[Any]:
            return client.get_orders(time_filter="last30")

        return self._read(request, work)

    def fetch(self, request: SyncRequest, reference: str) -> ImportedShipment:
        def work(client: Any, errors: Any) -> list[Any]:
            try:
                return [client.get_order(reference)]
            except errors.AmazonOrdersNotFoundError as error:
                raise CarrierError("not_found") from error

        # Asked for by number, an order is wanted even if Amazon shows no parcel for it any more.
        found = self._read(request, work, keep_empty=True)
        if not found:
            raise CarrierError("not_found")
        return found[0]

    def _read(self, request: SyncRequest, work: Any, keep_empty: bool = False):
        """Sign in, let ``work`` fetch orders, and add each parcel's tracking page."""
        # Imported here so that a missing or broken library only affects this module.
        from amazonauth.exception import (
            AmazonAuthChallengeUnsolvedError,
            AmazonAuthError,
            AmazonAuthSignInError,
        )
        from amazonauth.session import AmazonSession, IODefault
        from amazonorders import exception as order_errors
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
        otp_secret = clean_otp_secret(settings.get("otp_secret"))
        request.state_dir.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if importlib.util.find_spec("playwright"):
            # Amazon sometimes answers the sign-in with a JavaScript check that a
            # plain HTTP client cannot run. The library then runs it in a headless
            # browser and carries the cookies back.
            _prepare_browser()
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
            otp_secret_key=otp_secret,
        )
        try:
            session.login()
            client = AmazonOrders(session)
            orders = work(client, order_errors)
            tracking: dict[tuple[str, str], Any] = {}
            for order in orders:
                for index, shipment in enumerate(order.shipments or [], start=1):
                    if len(tracking) >= MAX_TRACKING_PAGES:
                        break
                    # One request per parcel: carrier, number and the history.
                    # Without the carrier page the status from the order list still stands.
                    key = (
                        order.order_number,
                        shipment.shipment_id or f"{order.order_number}/{index}",
                    )
                    found = _read_tracking(client, session, order.order_number, shipment)
                    if found is not None:
                        tracking[key] = found
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
        except Exception as error:
            # Playwright raises its own error type when the browser or a page fails.
            if type(error).__module__.startswith("playwright"):
                raise CarrierError("browser_failed", _first_line(error)) from error
            raise
        return to_shipments(orders, tracking, date.today(), keep_empty)


MODULE = AmazonDe()
