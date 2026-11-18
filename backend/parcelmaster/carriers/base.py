"""The contract between Parcel Master and a carrier module.

A module is a package below ``parcelmaster/carriers/<country>/<name>/`` that
exposes ``MODULE``, an instance of a :class:`CarrierModule` subclass. Its
optional ``locales/<lang>.json`` files hold the labels for its own fields.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from ..status import Status


@dataclass(frozen=True)
class Field:
    """A value the user enters: a module setting or a per-parcel extra."""

    key: str
    type: str = "text"  # "text", "secret" or "choice"
    required: bool = False
    placeholder: str = ""
    #: For a "choice": the values to pick from; the first one is the default.
    options: tuple[str, ...] = ()


@dataclass
class TrackingEvent:
    timestamp: datetime | None
    status: Status
    description: str
    location: str | None = None


@dataclass
class TrackingResult:
    status: Status
    description: str = ""
    location: str | None = None
    events: list[TrackingEvent] = field(default_factory=list)
    eta_from: datetime | None = None
    eta_to: datetime | None = None
    #: Stops the vehicle still makes before this parcel, where the carrier tells.
    stops_left: int | None = None
    #: The number is a lower bound ("a bit more than 20"), not a count.
    stops_about: bool = False
    #: The module's wish to be asked again sooner than usual, e.g. while a van is close.
    again_in_minutes: float | None = None


@dataclass(frozen=True)
class TrackRequest:
    tracking_number: str
    extra: dict[str, str]
    settings: dict[str, Any]
    language: str
    created_at: datetime


class CarrierError(Exception):
    """A lookup failed for a reason the user should see.

    ``code`` is one of: not_configured, unauthorized, not_found, rate_limited,
    unavailable, invalid_response.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


class CarrierModule:
    #: "tracker": the user enters a number and the module looks it up.
    #: "importer": the module finds the shipments itself (see ImporterModule).
    kind: str = "tracker"
    #: Unique id, by convention "<country>.<name>", e.g. "de.dhl".
    id: str = ""
    #: ISO country code of the market this module serves, or "" for none.
    country: str = ""
    name: str = ""
    #: Tracking-number shapes this carrier uses, matched against the
    #: normalized number. Used to suggest a carrier while typing.
    patterns: tuple[re.Pattern[str], ...] = ()
    #: A setting named "default_<key>" pre-fills the parcel field "<key>".
    settings_fields: tuple[Field, ...] = ()
    parcel_fields: tuple[Field, ...] = ()
    #: Lookups per day the carrier allows by default; None means unlimited.
    default_daily_budget: int | None = None
    #: Days a parcel waits at a pickup point before it is sent back.
    pickup_hold_days: int | None = None

    def normalize(self, tracking_number: str) -> str:
        return re.sub(r"[\s\-]", "", tracking_number).upper()

    def matches(self, tracking_number: str) -> bool:
        normalized = self.normalize(tracking_number)
        return any(pattern.fullmatch(normalized) for pattern in self.patterns)

    def extract_from_url(self, url: str) -> str | None:
        """Return the tracking number if ``url`` is one of this carrier's links."""
        return None

    def tracking_url(self, tracking_number: str, extra: dict[str, str], language: str) -> str:
        """Link to this parcel on the carrier's own website."""
        raise NotImplementedError

    def parcel_defaults(self, settings: dict[str, Any]) -> dict[str, str]:
        """Values the user set as defaults for this module's parcel fields."""
        defaults = {f.key: str(settings.get(f"default_{f.key}") or "") for f in self.parcel_fields}
        return {key: value for key, value in defaults.items() if value}

    def localize(self, text: str, phrase: Callable[[str], str | None]) -> str:
        """Put a status text of this carrier into the user's language.

        ``phrase(key)`` returns the text stored under ``key`` in this module's
        locale file for that language, or None. Carriers that already answer
        in the user's language need not do anything.
        """
        return text

    def is_configured(self, settings: dict[str, Any]) -> bool:
        return all(settings.get(f.key) for f in self.settings_fields if f.required)

    def budget(self, settings: dict[str, Any]) -> int | None:
        """Lookups per day unless the user sets a number; may depend on the way it asks."""
        return self.default_daily_budget

    def scarce(self, settings: dict[str, Any]) -> bool:
        """Whether lookups are few enough that parcels of a shop should wait for theirs."""
        return False

    def companion_script(self) -> str | None:
        """A userscript for the user's own browser that passes on what the carrier's
        page shows them; ``__SERVER__`` and ``__HOST__`` stand for this server."""
        return None

    def take_from_companion(self, payload: Any) -> list[tuple[str, TrackingResult]]:
        """Tracking number and result for each parcel in what a companion script sent."""
        return []

    def source(self, settings: dict[str, Any]) -> str | None:
        """Which way the module asks, if it knows more than one; named in ``locales``."""
        return None

    async def track(self, request: TrackRequest, http: httpx.AsyncClient) -> TrackingResult:
        raise NotImplementedError


@dataclass
class ImportedParcel:
    """One parcel as the importing service sees it."""

    #: Stable id of this parcel at the service; used to recognise it again.
    key: str
    status: Status
    description: str = ""
    eta_from: datetime | None = None
    #: When the parcel reached this status, if the service says so.
    status_time: datetime | None = None
    #: Name of the carrier that delivers it, as the service prints it ("DHL").
    carrier_name: str | None = None
    #: That carrier's tracking number, once known.
    tracking_number: str | None = None
    #: Link to this parcel at the service.
    url: str | None = None
    #: What is inside, one line per article.
    items: list[str] = field(default_factory=list)
    #: The parcel's history if the service shows one; None keeps what is stored.
    events: list[TrackingEvent] | None = None


@dataclass
class ImportedShipment:
    #: Order number or similar; one shipment per reference.
    reference: str
    title: str
    parcels: list[ImportedParcel] = field(default_factory=list)


@dataclass(frozen=True)
class SyncRequest:
    settings: dict[str, Any]
    language: str
    #: A directory of its own where the module may keep session files.
    state_dir: Path
    #: Keys of parcels that are filed as arrived: nothing more needs to be read about them.
    settled: frozenset[str] = frozenset()


class ImporterModule(CarrierModule):
    """A service that knows the user's shipments, such as a shop account.

    ``sync`` returns what is currently open there. Parcel Master files each
    entry as a shipment and hands a parcel over to the carrier's own module
    as soon as carrier and tracking number are known and that module is set
    up. Until then the parcel shows the status the service reports.
    """

    kind = "importer"
    #: The settings field that names the account, shown once it is connected.
    account_field: str = "email"
    #: Shape of a reference (an order number) that can be imported on its own,
    #: or None if the service cannot be asked for a single order.
    reference_pattern: re.Pattern[str] | None = None
    #: Minutes between two syncs.
    sync_interval_minutes: int = 180

    def tracking_url(self, tracking_number: str, extra: dict[str, str], language: str) -> str:
        return extra.get("url", "")

    def sync(self, request: SyncRequest) -> list[ImportedShipment]:
        """Fetch the open shipments. Runs in a worker thread and may block."""
        raise NotImplementedError

    def fetch(self, request: SyncRequest, reference: str) -> ImportedShipment:
        """Fetch one order by its reference, whatever its state. May block."""
        raise NotImplementedError
