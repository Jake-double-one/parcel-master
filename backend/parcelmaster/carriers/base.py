"""The contract between Parcel Master and a carrier module.

A module is a package below ``parcelmaster/carriers/<country>/<name>/`` that
exposes ``MODULE``, an instance of a :class:`CarrierModule` subclass. Its
optional ``locales/<lang>.json`` files hold the labels for its own fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from ..status import Status


@dataclass(frozen=True)
class Field:
    """A value the user enters: a module setting or a per-parcel extra."""

    key: str
    type: str = "text"  # "text" or "secret"
    required: bool = False
    placeholder: str = ""


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
    #: Unique id, by convention "<country>.<name>", e.g. "de.dhl".
    id: str = ""
    #: ISO country code of the market this module serves, or "" for none.
    country: str = ""
    name: str = ""
    #: Tracking-number shapes this carrier uses, matched against the
    #: normalized number. Used to suggest a carrier while typing.
    patterns: tuple[re.Pattern[str], ...] = ()
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

    def is_configured(self, settings: dict[str, Any]) -> bool:
        return all(settings.get(f.key) for f in self.settings_fields if f.required)

    async def track(self, request: TrackRequest, http: httpx.AsyncClient) -> TrackingResult:
        raise NotImplementedError
