"""Parcel Master: self-hosted parcel tracking with pluggable carrier modules."""

import os

__version__ = "0.1.0"


def build() -> str:
    """The commit the running image was built from, shortened; "dev" outside an image."""
    return (os.environ.get("PM_BUILD") or "dev")[:7]
