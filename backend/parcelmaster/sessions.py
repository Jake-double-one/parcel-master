"""The optional password wall and the demo mode.

Without ``PM_PASSWORD`` every request is trusted, which suits a deployment
behind an authenticating reverse proxy. With it, a signed cookie marks a
logged-in browser. A demo visitor gets a cookie that maps to a private
in-memory database, so nothing they do touches real data.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .db import Store

COOKIE = "pm_session"
LOGIN_TTL = 30 * 24 * 3600
DEMO_TTL = 3 * 3600
MAX_DEMO_SESSIONS = 200
MAX_FAILURES = 5
FAILURE_WINDOW = 300
#: Failed logins from all clients together within FAILURE_WINDOW before every
#: further attempt waits. Client addresses can be made up, the total cannot.
FREE_FAILURES = 20
PAUSE_SECONDS = 2.0
#: Client addresses remembered at most; the oldest go first.
MAX_CLIENTS = 10_000
REVOKED_KEY = "revoked_sessions"


class Signer:
    """Signs session cookies.

    The key depends on the password as well, so a new ``PM_PASSWORD`` ends
    every session made under the old one. A logout ends its own cookie.
    """

    def __init__(self, store: Store, password: str | None = None) -> None:
        secret = store.get_value("session_secret")
        if not secret:
            secret = secrets.token_hex(32)
            store.set_value("session_secret", secret)
        bound = hashlib.sha256((password or "").encode()).hexdigest()
        self._key = (secret + bound).encode()
        self._store = store

    def sign(self, kind: str, subject: str, ttl: int) -> str:
        payload = f"{kind}.{subject}.{int(time.time()) + ttl}"
        return f"{payload}.{self._mac(payload)}"

    def verify(self, token: str | None) -> tuple[str, str] | None:
        """Return (kind, subject) for a valid, unexpired token."""
        if not token or token.count(".") != 3:
            return None
        payload, _, mac = token.rpartition(".")
        if not hmac.compare_digest(mac, self._mac(payload)):
            return None
        kind, subject, expires = payload.split(".")
        if not expires.isdigit() or int(expires) < time.time():
            return None
        if mac in (self._store.get_value(REVOKED_KEY) or {}):
            return None
        return kind, subject

    def revoke(self, token: str | None) -> None:
        """End a cookie for good, e.g. on logout; kept until it would expire anyway."""
        if not self.verify(token):
            return
        assert token is not None
        payload, _, mac = token.rpartition(".")
        now = time.time()
        revoked = {
            key: until
            for key, until in (self._store.get_value(REVOKED_KEY) or {}).items()
            if until > now
        }
        revoked[mac] = int(payload.rsplit(".", 1)[1])
        self._store.set_value(REVOKED_KEY, revoked)

    def _mac(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()


class LoginThrottle:
    """Slows down password guessing.

    Per client address a few failures lead to a pause. As an address can be
    made up (a forwarded header), all failures together count as well: past
    FREE_FAILURES in the window every attempt waits its turn, one at a time.
    That slows guessing to a crawl without locking the owner out.
    """

    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}
        self._all: list[float] = []

    def blocked(self, client: str) -> bool:
        recent = [t for t in self._failures.get(client, []) if t > time.time() - FAILURE_WINDOW]
        if recent:
            self._failures[client] = recent
        else:
            self._failures.pop(client, None)
        return len(recent) >= MAX_FAILURES

    def pause(self) -> float:
        """Seconds the next attempt waits; 0 while few logins failed lately."""
        cutoff = time.time() - FAILURE_WINDOW
        self._all = [t for t in self._all if t > cutoff]
        return PAUSE_SECONDS if len(self._all) >= FREE_FAILURES else 0.0

    def failed(self, client: str) -> None:
        now = time.time()
        if client not in self._failures and len(self._failures) >= MAX_CLIENTS:
            self._failures.pop(next(iter(self._failures)))
        self._failures.setdefault(client, []).append(now)
        self._all.append(now)

    def succeeded(self, client: str) -> None:
        self._failures.pop(client, None)


@dataclass
class DemoSession:
    store: Store
    last_used: float


SAMPLES = (
    # title key, direction, reference, note key, tracking numbers
    ("headphones", "incoming", "", "", ("DEMO100003",)),
    ("order", "incoming", "302-4418205-9930711", "order_note", ("DEMO100015", "DEMO100022")),
    ("locker", "incoming", "", "", ("DEMO100034",)),
    ("return", "return", "", "return_note", ("DEMO100042",)),
    ("gift", "outgoing", "", "", ("DEMO100056",)),
    ("live", "incoming", "", "live_note", ("DEMO100060",)),
    ("cable", "incoming", "", "", ("DEMO100077",)),
    ("book", "incoming", "", "", ("DEMO100085",)),
)

SAMPLE_TEXT = {
    "en": {
        "headphones": "Headphones",
        "order": "Online order",
        "locker": "Hiking boots",
        "return": "Jacket, wrong size",
        "gift": "Birthday gift for Mia",
        "live": "Watch me move",
        "cable": "USB-C cable",
        "book": "Cookbook",
        "order_note": "Arrives in two parcels.",
        "return_note": "Refund once it arrives.",
        "live_note": "This parcel advances one step per minute.",
    },
    "de": {
        "headphones": "Kopfhörer",
        "order": "Online-Bestellung",
        "locker": "Wanderschuhe",
        "return": "Jacke, falsche Größe",
        "gift": "Geburtstagsgeschenk für Mia",
        "live": "Schau mir beim Reisen zu",
        "cable": "USB-C-Kabel",
        "book": "Kochbuch",
        "order_note": "Kommt in zwei Paketen.",
        "return_note": "Erstattung, sobald es ankommt.",
        "live_note": "Dieses Paket kommt jede Minute einen Schritt weiter.",
    },
}


def seed_demo(store: Store, language: str) -> None:
    text = SAMPLE_TEXT.get(language, SAMPLE_TEXT["en"])
    settings = store.get_settings()
    settings["language"] = language if language in SAMPLE_TEXT else "en"
    store.save_settings(settings)
    created = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
    for title, direction, reference, note, numbers in SAMPLES:
        store.save_shipment(
            None,
            {
                "title": text[title],
                "direction": direction,
                "note": text.get(note, ""),
                "source": "demo",
                "reference": reference,
                # The live parcel starts its journey when the visitor arrives.
                "created_at": None if title == "live" else created,
            },
            [
                {
                    "carrier": "demo",
                    "tracking_number": number,
                    "created_at": None if title == "live" else created,
                }
                for number in numbers
            ],
        )


class DemoSessions:
    def __init__(self) -> None:
        self._sessions: dict[str, DemoSession] = {}

    def create(self, language: str) -> str:
        self._expire()
        if len(self._sessions) >= MAX_DEMO_SESSIONS:
            oldest = min(self._sessions, key=lambda key: self._sessions[key].last_used)
            self._sessions.pop(oldest).store.close()
        session_id = secrets.token_hex(16)
        store = Store(None)
        seed_demo(store, language)
        self._sessions[session_id] = DemoSession(store, time.time())
        return session_id

    def get(self, session_id: str) -> Store | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        session.last_used = time.time()
        return session.store

    def _expire(self) -> None:
        cutoff = time.time() - DEMO_TTL
        for key in [k for k, s in self._sessions.items() if s.last_used < cutoff]:
            self._sessions.pop(key).store.close()
