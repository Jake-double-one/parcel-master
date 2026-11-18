"""What the server did lately, kept in memory so it can be shown in the app.

The same lines go to the container log. ``PM_LOG_LEVEL=debug`` adds detail
such as the beginning of an answer a carrier module could not read.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from datetime import UTC, datetime
from typing import Any

KEEP = 400
#: Loggers worth keeping: our own, and one line per outgoing request.
WATCHED = ("parcelmaster", "httpx")


class Journal(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.entries: deque[dict[str, Any]] = deque(maxlen=KEEP)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                error = record.exc_info[1]
                message += f" ({type(error).__name__}: {error})"
            self.entries.append(
                {
                    "time": datetime.fromtimestamp(record.created, UTC).isoformat(
                        timespec="seconds"
                    ),
                    "level": record.levelname.lower(),
                    "source": record.name.removeprefix("parcelmaster."),
                    "message": message[:2000],
                }
            )
        except Exception:  # noqa: BLE001 - logging must never break the caller
            self.handleError(record)


JOURNAL = Journal()


def level_from_env() -> int:
    name = os.environ.get("PM_LOG_LEVEL", "info").strip().upper()
    level = logging.getLevelName(name)
    return level if isinstance(level, int) else logging.INFO


def setup() -> int:
    """Send our log lines to the container log and into the journal; safe to call twice."""
    level = level_from_env()
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    stream.set_name("parcelmaster-stream")
    for name in WATCHED:
        logger = logging.getLogger(name)
        # One line per request is useful; the library's own debug output is not.
        logger.setLevel(level if name == "parcelmaster" else max(level, logging.INFO))
        if JOURNAL not in logger.handlers:
            logger.addHandler(JOURNAL)
        if not any(h.get_name() == "parcelmaster-stream" for h in logger.handlers):
            logger.addHandler(stream)
        logger.propagate = False
    return level
