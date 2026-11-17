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
from .tracking import Notice, in_window, timezone

log = logging.getLogger(__name__)

STATUS_KINDS = ("out_for_delivery", "ready_for_pickup", "delivered", "problem")


def in_quiet_hours(settings: dict[str, Any], now: datetime | None = None) -> bool:
    return in_window(settings["notifications"]["quiet_hours"], timezone(settings), now)


def wanted(settings: dict[str, Any], kind: str) -> bool:
    events = settings["notifications"]["events"]
    if kind in ("stale", "pickup_reminder", "delivery_window"):
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
        body = t(language, f"notify.{notice.kind}.body", **params)
        if body == f"notify.{notice.kind}.body":  # a status without its own sentence
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
        for notice in notices:
            if not wanted(settings, notice.kind):
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
        accepted = sum(1 for url in urls if sender.add(url))
        ok = bool(accepted) and bool(sender.notify(title=message["title"], body=message["body"]))
        return {"ok": ok, "targets": accepted, "rejected": len(urls) - accepted}

    def _push(self, subscriptions: list[dict[str, Any]], message: dict[str, Any]) -> dict[str, Any]:
        from pywebpush import WebPushException, webpush

        keys = vapid_keys(self.store)
        sent = 0
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
                else:
                    log.warning("web push failed: %s", error)
        return {"ok": sent > 0, "sent": sent, "subscriptions": len(subscriptions)}
