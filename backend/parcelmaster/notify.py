"""Turns notices into messages and sends them via Apprise and web push."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime
from typing import Any

from .carriers.registry import Registry
from .db import Store
from .i18n import Translations
from .status import Status
from .tracking import Notice, in_window, timezone

log = logging.getLogger(__name__)

STATUS_KINDS = ("out_for_delivery", "ready_for_pickup", "delivered", "problem")


def in_quiet_hours(settings: dict[str, Any], now: datetime | None = None) -> bool:
    return in_window(settings["notifications"]["quiet_hours"], timezone(settings), now)


def wanted(settings: dict[str, Any], kind: str) -> bool:
    events = settings["notifications"]["events"]
    if kind in ("stale", "pickup_reminder", "delivery_window", "stops", "discovered", "step"):
        return bool(events.get(kind))
    return bool(events.get("any_change") or (kind in STATUS_KINDS and events.get(kind)))


def vapid_keys(store: Store) -> dict[str, str]:
    """The server's push key pair, created on first use."""
    keys = store.get_value("vapid")
    if keys:
        return keys
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    number = private.private_numbers().private_value.to_bytes(32, "big")
    keys = {
        "public": base64.urlsafe_b64encode(public).rstrip(b"=").decode(),
        "private": base64.urlsafe_b64encode(number).rstrip(b"=").decode(),
    }
    store.set_value("vapid", keys)
    return keys


class Notifier:
    def __init__(
        self, store: Store, registry: Registry, translations: Translations, push_contact: str
    ) -> None:
        self.store = store
        self.registry = registry
        self.translations = translations
        self.push_contact = push_contact

    def compose(self, notice: Notice, language: str) -> dict[str, Any]:
        parcel = notice.parcel
        shipment = self.store.get_shipment(parcel["shipment_id"]) or {}
        module = self.registry.get(parcel["carrier"])
        details = dict(notice.params)
        variant = details.pop("variant", None) or "body"  # a sentence of its own for a case
        if module and details.get("description"):
            details["description"] = self.translations.localize(
                module, str(details["description"]), language
            )
        params = {
            "title": shipment.get("title") or parcel["tracking_number"],
            "carrier": module.name if module else parcel["carrier"],
            "number": parcel["tracking_number"],
            **details,
        }
        t = self.translations.t
        body = t(language, f"notify.{notice.kind}.{variant}", **params)
        if body == f"notify.{notice.kind}.{variant}":  # a status without its own sentence
            body = t(language, "notify.change.body", **params)
        return {
            "kind": notice.kind,
            "title": t(language, "notify.title", **params),
            "body": body,
            "shipment_id": parcel["shipment_id"],
        }

    async def handle(self, notices: list[Notice]) -> None:
        if not notices:
            return
        settings = self.store.get_settings()
        # A step that also changed the status is told once, by the status message.
        told = {
            notice.parcel["id"]
            for notice in notices
            if notice.kind in {str(status) for status in Status} and wanted(settings, notice.kind)
        }
        for notice in notices:
            if not wanted(settings, notice.kind):
                continue
            if notice.kind == "step" and notice.parcel["id"] in told:
                continue
            message = self.compose(notice, settings["language"])
            if in_quiet_hours(settings):
                self.store.hold_notification(message)
            else:
                await self.send(message, settings)

    async def flush_held(self) -> None:
        settings = self.store.get_settings()
        if in_quiet_hours(settings):
            return
        for message in self.store.take_held_notifications():
            await self.send(message, settings)

    async def send_to_device(self, message: dict[str, Any], endpoint: str) -> dict[str, Any]:
        """Push to one subscribed browser only, to try that device out."""
        chosen = [s for s in self.store.push_subscriptions() if s.get("endpoint") == endpoint]
        report: dict[str, Any] = {"apprise": None, "push": None}
        if chosen:
            report["push"] = await asyncio.to_thread(self._push, chosen, message)
        return report

    async def send(self, message: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
        """Deliver on every configured channel; returns what happened per channel."""
        notifications = settings["notifications"]
        urls = [url for url in notifications.get("apprise_urls", []) if url.strip()]
        report: dict[str, Any] = {"apprise": None, "push": None}
        if urls:
            report["apprise"] = await asyncio.to_thread(self._apprise, urls, message)
        if notifications.get("push_enabled"):
            subscriptions = self.store.push_subscriptions()
            if subscriptions:
                report["push"] = await asyncio.to_thread(self._push, subscriptions, message)
        return report

    def _apprise(self, urls: list[str], message: dict[str, Any]) -> dict[str, Any]:
        import apprise

        sender = apprise.Apprise()
        # Apprise says why a service refused only in its own log; listen in.
        with apprise.LogCapture(level=logging.INFO) as heard:
            accepted = sum(1 for url in urls if sender.add(url))
            ok = bool(accepted) and bool(
                sender.notify(title=message["title"], body=message["body"])
            )
            said = [
                line.split(" - ", 2)[-1].strip()
                for line in heard.getvalue().splitlines()
                if " - WARNING - " in line or " - ERROR - " in line
            ]
        rejected = len(urls) - accepted
        if rejected:
            said.insert(0, f"{rejected} address(es) not understood")
        for line in said:
            log.warning("apprise: %s", line)
        if ok and not said:
            log.info("apprise: sent to %d service(s)", accepted)
        return {
            "ok": ok and not rejected,
            "targets": accepted,
            "rejected": rejected,
            "error": "; ".join(said)[:300],
        }

    def _push(self, subscriptions: list[dict[str, Any]], message: dict[str, Any]) -> dict[str, Any]:
        from pywebpush import WebPushException, webpush

        keys = vapid_keys(self.store)
        sent = 0
        problem = ""
        for subscription in subscriptions:
            try:
                webpush(
                    subscription_info=subscription,
                    data=json.dumps(message),
                    vapid_private_key=keys["private"],
                    vapid_claims={"sub": self.push_contact},
                    ttl=24 * 3600,
                )
                sent += 1
            except WebPushException as error:
                status = getattr(error.response, "status_code", None)
                if status in (404, 410):  # the browser dropped this subscription
                    self.store.remove_push_subscription(subscription["endpoint"])
                    problem = problem or "gone"
                else:
                    log.warning("web push failed: %s", error)
                    problem = f"HTTP {status}" if status else type(error).__name__
            except Exception as error:  # noqa: BLE001 - one bad subscription must not stop the rest
                log.warning("web push failed: %s", error)
                problem = type(error).__name__
        return {
            "ok": sent > 0,
            "sent": sent,
            "subscriptions": len(subscriptions),
            "error": problem,
        }
