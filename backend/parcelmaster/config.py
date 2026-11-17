"""Settings that belong to the deployment, read from the environment.

Everything a user changes while the app runs lives in the database instead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    data_dir: Path
    #: Shared password for the login wall; None trusts the reverse proxy.
    password: str | None
    #: Offer the public demo mode (and the demo carrier).
    demo: bool
    #: Make the demo carrier selectable for real data (development only).
    demo_carrier: bool
    #: Run the background polling loop.
    scheduler: bool
    #: Built frontend to serve; None serves the API only.
    static_dir: Path | None
    #: Contact that push services may use to reach the operator.
    push_contact: str

    @classmethod
    def from_env(cls) -> Config:
        static = os.environ.get("PM_STATIC_DIR")
        return cls(
            data_dir=Path(os.environ.get("PM_DATA_DIR", "data")),
            password=os.environ.get("PM_PASSWORD") or None,
            demo=_flag("PM_DEMO"),
            demo_carrier=_flag("PM_DEMO_CARRIER"),
            scheduler=_flag("PM_SCHEDULER", True),
            static_dir=Path(static) if static else None,
            push_contact=os.environ.get("PM_PUSH_CONTACT", "mailto:parcelmaster@localhost"),
        )
