"""SQLite storage. One :class:`Store` is one database: the real one on disk,
or a throwaway in-memory one per demo visitor."""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .status import FINAL, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS shipments (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT 'incoming',
    note TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    reference TEXT NOT NULL DEFAULT '',
    archived_at TEXT,
    deleted_at TEXT,
    suggested_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS parcels (
    id INTEGER PRIMARY KEY,
    shipment_id INTEGER NOT NULL REFERENCES shipments(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    carrier TEXT NOT NULL,
    tracking_number TEXT NOT NULL,
    extra TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'unknown',
    description TEXT NOT NULL DEFAULT '',
    location TEXT,
    eta_from TEXT,
    eta_to TEXT,
    last_event_at TEXT,
    delivered_at TEXT,
    pickup_ready_at TEXT,
    last_checked_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    error_count INTEGER NOT NULL DEFAULT 0,
    next_check_at TEXT,
    stale_notified_at TEXT,
    pickup_reminded_at TEXT,
    remind INTEGER NOT NULL DEFAULT 0,
    reminded_for TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS parcels_by_shipment ON parcels(shipment_id);
CREATE INDEX IF NOT EXISTS parcels_due ON parcels(next_check_at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    parcel_id INTEGER NOT NULL REFERENCES parcels(id) ON DELETE CASCADE,
    timestamp TEXT,
    status TEXT NOT NULL,
    description TEXT NOT NULL,
    location TEXT
);
CREATE INDEX IF NOT EXISTS events_by_parcel ON events(parcel_id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS usage (
    carrier TEXT NOT NULL,
    day TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (carrier, day)
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "language": "en",
    "timezone": "Europe/Berlin",
    "polling": {
        "interval_minutes": 120,
        "stale_days": 4,
        "auto_archive_days": 3,
        # Ask carriers less where it costs nothing: not at night, rarely while a
        # parcel is only announced or days away, and an importer's parcels only
        # once they are out for delivery.
        # "last_call" asks everything once more as the night rest begins.
        "saver": {"enabled": True, "from": "22:00", "to": "06:00", "last_call": True},
    },
    "carriers": {},
    "notifications": {
        "apprise_urls": [],
        "push_enabled": True,
        # Minutes before the delivery window opens that a parcel's bell rings.
        "reminder_minutes": 30,
        "events": {
            "any_change": False,
            "out_for_delivery": True,
            "ready_for_pickup": True,
            "delivered": True,
            "delivery_window": True,
            "stops": True,
            "step": False,
            "discovered": True,
            "problem": True,
            "stale": True,
            "pickup_reminder": True,
        },
        "quiet_hours": {"enabled": False, "from": "22:00", "to": "07:00"},
    },
    # What the companion scripts do with parcels they see and we do not track:
    # "button" takes one only on a click in the carrier's page, "suggest" lists
    # it for the user to decide, "auto" tracks it right away.
    "discovery": {"mode": "suggest"},
}

PARCEL_UPDATE_COLUMNS = frozenset(
    {
        "status",
        "description",
        "location",
        "eta_from",
        "eta_to",
        "last_event_at",
        "delivered_at",
        "pickup_ready_at",
        "last_checked_at",
        "last_success_at",
        "last_error",
        "error_count",
        "next_check_at",
        "stale_notified_at",
        "pickup_reminded_at",
        "remind",
        "reminded_for",
    }
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``override`` into a copy of ``base``."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class Store:
    def __init__(self, path: Path | None) -> None:
        """Open the database at ``path``; ``None`` gives a private in-memory one."""
        self._lock = threading.RLock()
        if path is None:
            self._db = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        with self._lock, self._db:
            self._db.executescript(SCHEMA)
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(shipments)")}
            for column in ("deleted_at", "suggested_at"):
                if column not in columns:  # a database from before the trash
                    self._db.execute(f"ALTER TABLE shipments ADD COLUMN {column} TEXT")
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(parcels)")}
            if "remind" not in columns:  # a database from before the reminder bell
                self._db.execute("ALTER TABLE parcels ADD COLUMN remind INTEGER NOT NULL DEFAULT 0")
                self._db.execute("ALTER TABLE parcels ADD COLUMN reminded_for TEXT")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- settings ---------------------------------------------------------

    def get_value(self, key: str) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set_value(self, key: str, value: Any) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def get_settings(self) -> dict[str, Any]:
        return merge(DEFAULT_SETTINGS, self.get_value("app") or {})

    def save_settings(self, settings: dict[str, Any]) -> None:
        self.set_value("app", settings)

    # -- shipments --------------------------------------------------------

    def _parcel(self, row: sqlite3.Row, events: list[dict[str, Any]]) -> dict[str, Any]:
        parcel = dict(row)
        parcel["extra"] = json.loads(parcel["extra"] or "{}")
        parcel["last_error"] = json.loads(parcel["last_error"]) if parcel["last_error"] else None
        parcel["events"] = events
        return parcel

    def _load(self, shipment_rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        if not shipment_rows:
            return []
        ids = [row["id"] for row in shipment_rows]
        marks = ",".join("?" * len(ids))
        parcel_rows = self._db.execute(
            f"SELECT * FROM parcels WHERE shipment_id IN ({marks}) ORDER BY position, id", ids
        ).fetchall()
        parcel_ids = [row["id"] for row in parcel_rows]
        events: dict[int, list[dict[str, Any]]] = {}
        if parcel_ids:
            event_marks = ",".join("?" * len(parcel_ids))
            for event in self._db.execute(
                f"SELECT parcel_id, timestamp, status, description, location FROM events "
                f"WHERE parcel_id IN ({event_marks}) ORDER BY timestamp DESC, id DESC",
                parcel_ids,
            ):
                item = dict(event)
                events.setdefault(item.pop("parcel_id"), []).append(item)
        by_shipment: dict[int, list[dict[str, Any]]] = {}
        for row in parcel_rows:
            by_shipment.setdefault(row["shipment_id"], []).append(
                self._parcel(row, events.get(row["id"], []))
            )
        return [{**dict(row), "parcels": by_shipment.get(row["id"], [])} for row in shipment_rows]

    def list_shipments(self, archived: bool) -> list[dict[str, Any]]:
        condition = "IS NOT NULL" if archived else "IS NULL"
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM shipments WHERE archived_at {condition} "
                "AND deleted_at IS NULL ORDER BY created_at DESC"
            ).fetchall()
            return self._load(rows)

    def list_trash(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM shipments WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC"
            ).fetchall()
            return self._load(rows)

    def find_number(self, tracking_number: str) -> dict[str, Any] | None:
        """Any shipment holding this number, wherever it lies: open, archived or binned."""
        with self._lock:
            row = self._db.execute(
                "SELECT s.* FROM parcels p JOIN shipments s ON s.id = p.shipment_id "
                "WHERE p.tracking_number=? ORDER BY s.deleted_at IS NOT NULL, s.id LIMIT 1",
                (tracking_number,),
            ).fetchone()
        return dict(row) if row else None

    def get_shipment(self, shipment_id: int) -> dict[str, Any] | None:
        with self._lock:
            rows = self._db.execute("SELECT * FROM shipments WHERE id=?", (shipment_id,)).fetchall()
            loaded = self._load(rows)
        return loaded[0] if loaded else None

    def get_parcel(self, parcel_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM parcels WHERE id=?", (parcel_id,)).fetchone()
            return self._parcel(row, []) if row else None

    def find_duplicate(
        self, carrier: str, tracking_number: str, exclude_shipment: int | None
    ) -> dict[str, Any] | None:
        """An open shipment that already tracks this number, if any."""
        with self._lock:
            row = self._db.execute(
                "SELECT s.id, s.title FROM parcels p JOIN shipments s ON s.id = p.shipment_id "
                "WHERE p.carrier=? AND p.tracking_number=? AND s.archived_at IS NULL "
                "AND s.deleted_at IS NULL AND s.id IS NOT ?",
                (carrier, tracking_number, exclude_shipment),
            ).fetchone()
        return dict(row) if row else None

    def save_shipment(
        self, shipment_id: int | None, data: dict[str, Any], parcels: list[dict[str, Any]]
    ) -> int:
        """Create or update a shipment and make its parcels match ``parcels``.

        A parcel whose carrier and number are unchanged keeps its history;
        any other change starts it from scratch.
        """
        now = now_iso()
        with self._lock, self._db:
            if shipment_id is None:
                cursor = self._db.execute(
                    "INSERT INTO shipments(title, direction, note, source, reference, "
                    "suggested_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        data["title"],
                        data["direction"],
                        data["note"],
                        data.get("source", "manual"),
                        data.get("reference", ""),
                        now if data.get("suggested") else None,
                        data.get("created_at") or now,
                        now,
                    ),
                )
                shipment_id = int(cursor.lastrowid)
            else:
                self._db.execute(
                    "UPDATE shipments SET title=?, direction=?, note=?, updated_at=? WHERE id=?",
                    (data["title"], data["direction"], data["note"], now, shipment_id),
                )
            existing = {
                row["id"]: row
                for row in self._db.execute(
                    "SELECT id, carrier, tracking_number FROM parcels WHERE shipment_id=?",
                    (shipment_id,),
                )
            }
            kept: set[int] = set()
            for position, parcel in enumerate(parcels):
                extra = json.dumps(parcel.get("extra") or {})
                old = existing.get(parcel.get("id") or -1)
                unchanged = (
                    old is not None
                    and old["carrier"] == parcel["carrier"]
                    and old["tracking_number"] == parcel["tracking_number"]
                )
                if unchanged:
                    kept.add(old["id"])
                    self._db.execute(
                        "UPDATE parcels SET position=?, extra=? WHERE id=?",
                        (position, extra, old["id"]),
                    )
                else:
                    self._db.execute(
                        "INSERT INTO parcels(shipment_id, position, carrier, tracking_number, "
                        "extra, next_check_at, created_at) VALUES(?,?,?,?,?,?,?)",
                        (
                            shipment_id,
                            position,
                            parcel["carrier"],
                            parcel["tracking_number"],
                            extra,
                            now,
                            parcel.get("created_at") or now,
                        ),
                    )
            for parcel_id in existing.keys() - kept:
                self._db.execute("DELETE FROM parcels WHERE id=?", (parcel_id,))
        return shipment_id

    def find_by_reference(self, source: str, reference: str) -> dict[str, Any] | None:
        """The shipment an importer filed for this order, archived or not."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM shipments WHERE source=? AND reference=? ORDER BY id LIMIT 1",
                (source, reference),
            ).fetchall()
            loaded = self._load(rows)
        return loaded[0] if loaded else None

    def find_order(self, source: str, order: str) -> list[dict[str, Any]]:
        """Every shipment filed for an order: the order itself and its parcels of their own."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM shipments WHERE source=? AND (reference=? OR reference LIKE ?) "
                "ORDER BY id",
                (source, order, order.replace("%", "") + "#%"),
            ).fetchall()
            return self._load(rows)

    def delete_parcel(self, parcel_id: int) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM parcels WHERE id=?", (parcel_id,))

    def move_parcel(self, parcel_id: int, shipment_id: int) -> None:
        """Hand a parcel, with its history, to another shipment."""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE parcels SET shipment_id=? WHERE id=?", (shipment_id, parcel_id)
            )

    def add_parcel(
        self, shipment_id: int, carrier: str, tracking_number: str, extra: dict[str, str]
    ) -> int:
        now = now_iso()
        with self._lock, self._db:
            position = self._db.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM parcels WHERE shipment_id=?",
                (shipment_id,),
            ).fetchone()[0]
            cursor = self._db.execute(
                "INSERT INTO parcels(shipment_id, position, carrier, tracking_number, extra, "
                "next_check_at, created_at) VALUES(?,?,?,?,?,?,?)",
                (shipment_id, position, carrier, tracking_number, json.dumps(extra), now, now),
            )
        return int(cursor.lastrowid)

    def retarget_parcel(
        self, parcel_id: int, carrier: str, tracking_number: str, extra: dict[str, str]
    ) -> None:
        """Point a parcel at another carrier or number; it is due for a lookup at once."""
        with self._lock, self._db:
            self._db.execute(
                "UPDATE parcels SET carrier=?, tracking_number=?, extra=?, next_check_at=?, "
                "last_error=NULL, error_count=0 WHERE id=?",
                (carrier, tracking_number, json.dumps(extra), now_iso(), parcel_id),
            )

    def set_parcel_extra(self, parcel_id: int, extra: dict[str, str]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE parcels SET extra=? WHERE id=?", (json.dumps(extra), parcel_id)
            )

    def parcel_events(self, parcel_id: int) -> list[dict[str, Any]]:
        """A parcel's events, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT timestamp, status, description, location FROM events "
                "WHERE parcel_id=? ORDER BY timestamp, id",
                (parcel_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_archived(self, shipment_id: int, archived: bool) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE shipments SET archived_at=?, updated_at=? WHERE id=?",
                (now_iso() if archived else None, now_iso(), shipment_id),
            )
        return cursor.rowcount > 0

    def set_deleted(self, shipment_id: int, deleted: bool) -> bool:
        """Into the bin or back out of it."""
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE shipments SET deleted_at=?, updated_at=? WHERE id=?",
                (now_iso() if deleted else None, now_iso(), shipment_id),
            )
        return cursor.rowcount > 0

    def accept_suggestion(self, shipment_id: int) -> bool:
        """A found shipment becomes one of the user's own; its parcels are due at once."""
        now = now_iso()
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE shipments SET suggested_at=NULL, updated_at=? "
                "WHERE id=? AND suggested_at IS NOT NULL",
                (now, shipment_id),
            )
            self._db.execute(
                "UPDATE parcels SET next_check_at=? WHERE shipment_id=?", (now, shipment_id)
            )
        return cursor.rowcount > 0

    def trash_older_than(self, deleted_before: str) -> list[int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM shipments WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                (deleted_before,),
            ).fetchall()
        return [row["id"] for row in rows]

    def delete_shipment(self, shipment_id: int) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute("DELETE FROM shipments WHERE id=?", (shipment_id,))
        return cursor.rowcount > 0

    # -- tracking ---------------------------------------------------------

    def update_parcel(self, parcel_id: int, **fields: Any) -> None:
        unknown = fields.keys() - PARCEL_UPDATE_COLUMNS
        if unknown:
            raise ValueError(f"not updatable: {sorted(unknown)}")
        if "last_error" in fields and fields["last_error"] is not None:
            fields["last_error"] = json.dumps(fields["last_error"])
        assignments = ", ".join(f"{column}=?" for column in fields)
        with self._lock, self._db:
            self._db.execute(
                f"UPDATE parcels SET {assignments} WHERE id=?", (*fields.values(), parcel_id)
            )

    def replace_events(self, parcel_id: int, events: list[dict[str, Any]]) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM events WHERE parcel_id=?", (parcel_id,))
            self._db.executemany(
                "INSERT INTO events(parcel_id, timestamp, status, description, location) "
                "VALUES(?,?,?,?,?)",
                [
                    (parcel_id, e["timestamp"], e["status"], e["description"], e["location"])
                    for e in events
                ],
            )

    def due_parcels(self, now: str | None, limit: int) -> list[int]:
        """Unfinished parcels whose next lookup is due; without ``now`` all of them."""
        final = ",".join(f"'{status}'" for status in FINAL)
        with self._lock:
            rows = self._db.execute(
                "SELECT p.id FROM parcels p JOIN shipments s ON s.id = p.shipment_id "
                "WHERE s.archived_at IS NULL AND s.deleted_at IS NULL "
                f"AND s.suggested_at IS NULL AND p.status NOT IN ({final}) "
                "AND (? IS NULL OR p.next_check_at IS NULL OR p.next_check_at <= ?) "
                "ORDER BY p.next_check_at LIMIT ?",
                (now, now, limit),
            ).fetchall()
        return [row["id"] for row in rows]

    def open_parcels(self, suggested: bool = False) -> list[dict[str, Any]]:
        """Every parcel of a shipment that is not archived, with its shipment title.

        Found shipments the user has not taken yet only count when ``suggested``.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT p.*, s.title AS shipment_title, s.suggested_at FROM parcels p "
                "JOIN shipments s ON s.id = p.shipment_id "
                "WHERE s.archived_at IS NULL AND s.deleted_at IS NULL"
                + ("" if suggested else " AND s.suggested_at IS NULL")
            ).fetchall()
            result = []
            for row in rows:
                parcel = self._parcel(row, [])
                result.append(parcel)
            return result

    def shipments_to_archive(self, delivered_before: str) -> list[int]:
        """Open shipments whose parcels were all finished before the cut-off."""
        with self._lock:
            rows = self._db.execute(
                "SELECT s.id FROM shipments s WHERE s.archived_at IS NULL "
                "AND s.deleted_at IS NULL AND s.suggested_at IS NULL "
                "AND EXISTS (SELECT 1 FROM parcels p WHERE p.shipment_id = s.id) "
                "AND NOT EXISTS (SELECT 1 FROM parcels p WHERE p.shipment_id = s.id AND "
                "(p.status NOT IN (?, ?) OR p.delivered_at IS NULL OR p.delivered_at > ?))",
                (Status.DELIVERED, Status.RETURNED, delivered_before),
            ).fetchall()
        return [row["id"] for row in rows]

    # -- per-module daily budget -----------------------------------------

    def usage_today(self, carrier: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT count FROM usage WHERE carrier=? AND day=?",
                (carrier, date.today().isoformat()),
            ).fetchone()
        return int(row["count"]) if row else 0

    def count_usage(self, carrier: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO usage(carrier, day, count) VALUES(?, ?, 1) "
                "ON CONFLICT(carrier, day) DO UPDATE SET count = count + 1",
                (carrier, date.today().isoformat()),
            )
            self._db.execute("DELETE FROM usage WHERE day < date('now', '-30 day')")

    # -- push subscriptions and held-back notifications -------------------

    def add_push_subscription(self, subscription: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO push_subscriptions(endpoint, data, created_at) VALUES(?,?,?) "
                "ON CONFLICT(endpoint) DO UPDATE SET data=excluded.data",
                (subscription["endpoint"], json.dumps(subscription), now_iso()),
            )

    def remove_push_subscription(self, endpoint: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))

    def push_subscriptions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM push_subscriptions").fetchall()
        return [json.loads(row["data"]) for row in rows]

    def hold_notification(self, payload: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO outbox(created_at, payload) VALUES(?, ?)",
                (now_iso(), json.dumps(payload)),
            )

    def take_held_notifications(self) -> list[dict[str, Any]]:
        with self._lock, self._db:
            rows = self._db.execute("SELECT payload FROM outbox ORDER BY id").fetchall()
            self._db.execute("DELETE FROM outbox")
        return [json.loads(row["payload"]) for row in rows]
