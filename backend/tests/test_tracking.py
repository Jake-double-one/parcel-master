import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from parcelmaster.carriers.base import CarrierError, CarrierModule, Field, TrackingResult
from parcelmaster.carriers.registry import Registry
from parcelmaster.db import Store
from parcelmaster.i18n import Translations
from parcelmaster.notify import Notifier, in_quiet_hours, wanted
from parcelmaster.status import Status
from parcelmaster.tracking import (
    auto_archive,
    next_delay_minutes,
    periodic_notices,
    refresh_parcel,
)


class Scripted(CarrierModule):
    """Answers with whatever the test queued."""

    id = "test"
    name = "Test Post"
    settings_fields = (Field("api_key", type="secret", required=True),)
    default_daily_budget = 3
    pickup_hold_days = 7

    def __init__(self):
        self.answers = []
        self.calls = 0

    def tracking_url(self, tracking_number, extra, language):
        return f"https://example.org/{tracking_number}"

    async def track(self, request, http):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def world():
    store = Store(None)
    module = Scripted()
    registry = Registry([module], {})
    settings = store.get_settings()
    settings["carriers"]["test"] = {"api_key": "k"}
    store.save_settings(settings)
    shipment_id = store.save_shipment(
        None,
        {"title": "Boots", "direction": "incoming", "note": ""},
        [{"carrier": "test", "tracking_number": "T1"}],
    )
    parcel_id = store.get_shipment(shipment_id)["parcels"][0]["id"]
    return store, module, registry, shipment_id, parcel_id


def refresh(store, registry, parcel_id):
    async def run():
        async with httpx.AsyncClient() as http:
            return await refresh_parcel(store, registry, http, parcel_id)

    return asyncio.run(run())


def test_first_answer_is_recorded_but_is_not_news(world):
    store, module, registry, _, parcel_id = world
    module.answers = [TrackingResult(Status.IN_TRANSIT, "On its way")]
    assert refresh(store, registry, parcel_id) == []
    parcel = store.get_parcel(parcel_id)
    assert parcel["status"] == "in_transit"
    assert parcel["last_checked_at"] and parcel["next_check_at"]


def test_status_change_produces_a_notice_and_delivery_stops_polling(world):
    store, module, registry, _, parcel_id = world
    module.answers = [
        TrackingResult(Status.IN_TRANSIT, "On its way"),
        TrackingResult(Status.IN_TRANSIT, "Still on its way"),
        TrackingResult(Status.DELIVERED, "Handed over"),
    ]
    refresh(store, registry, parcel_id)
    assert refresh(store, registry, parcel_id) == []  # same status: no notice
    notices = refresh(store, registry, parcel_id)
    assert [notice.kind for notice in notices] == ["delivered"]
    parcel = store.get_parcel(parcel_id)
    assert parcel["delivered_at"] is not None
    assert parcel["next_check_at"] is None
    assert store.due_parcels(datetime.now(UTC).isoformat(), 10) == []


def test_carrier_errors_are_kept_for_display_and_retried(world):
    store, module, registry, _, parcel_id = world
    module.answers = [CarrierError("unauthorized")]
    assert refresh(store, registry, parcel_id) == []
    parcel = store.get_parcel(parcel_id)
    assert parcel["last_error"]["code"] == "unauthorized"
    assert parcel["status"] == "unknown"
    assert parcel["next_check_at"] > datetime.now(UTC).isoformat()


def test_daily_budget_stops_lookups_until_tomorrow(world):
    store, module, registry, _, parcel_id = world
    module.answers = [TrackingResult(Status.IN_TRANSIT, "x")] * 5
    for _ in range(5):
        refresh(store, registry, parcel_id)
    assert module.calls == 3  # default_daily_budget
    assert store.get_parcel(parcel_id)["last_error"]["code"] == "budget_exhausted"


def test_missing_key_costs_no_lookup(world):
    store, module, registry, _, parcel_id = world
    settings = store.get_settings()
    settings["carriers"]["test"] = {}
    store.save_settings(settings)
    refresh(store, registry, parcel_id)
    assert module.calls == 0
    assert store.get_parcel(parcel_id)["last_error"]["code"] == "not_configured"


def test_polling_adapts_to_the_status():
    recent = datetime.now(UTC)
    old = recent - timedelta(days=5)
    assert next_delay_minutes(Status.OUT_FOR_DELIVERY, 120, recent) == 30
    assert next_delay_minutes(Status.IN_TRANSIT, 120, recent) == 120
    assert next_delay_minutes(Status.IN_TRANSIT, 120, old) == 240
    assert next_delay_minutes(Status.READY_FOR_PICKUP, 120, recent) == 360
    assert next_delay_minutes(Status.DELIVERED, 120, recent) is None
    assert next_delay_minutes(Status.OUT_FOR_DELIVERY, 20, recent) == 15  # never below the floor


def test_standstill_and_pickup_reminders_fire_once(world):
    store, module, registry, _, parcel_id = world
    long_ago = (datetime.now(UTC) - timedelta(days=6)).isoformat(timespec="seconds")
    store.update_parcel(parcel_id, status="in_transit", last_event_at=long_ago)
    assert [n.kind for n in periodic_notices(store, registry)] == ["stale"]
    assert periodic_notices(store, registry) == []

    store.update_parcel(parcel_id, status="ready_for_pickup", pickup_ready_at=long_ago)
    notices = periodic_notices(store, registry)
    assert [n.kind for n in notices] == ["pickup_reminder"]
    assert notices[0].params["days"] in (0, 1)
    assert periodic_notices(store, registry) == []


def test_delivered_shipments_are_archived_after_the_set_days(world):
    store, _, _, shipment_id, parcel_id = world
    four_days = (datetime.now(UTC) - timedelta(days=4)).isoformat(timespec="seconds")
    store.update_parcel(parcel_id, status="delivered", delivered_at=four_days)
    assert auto_archive(store) == 1
    assert store.get_shipment(shipment_id)["archived_at"] is not None


def test_recent_or_open_shipments_stay(world):
    store, _, _, shipment_id, parcel_id = world
    assert auto_archive(store) == 0  # still open
    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    store.update_parcel(parcel_id, status="delivered", delivered_at=yesterday)
    assert auto_archive(store) == 0


def test_quiet_hours_cross_midnight():
    settings = {
        "timezone": "UTC",
        "notifications": {"quiet_hours": {"enabled": True, "from": "22:00", "to": "07:00"}},
    }
    assert in_quiet_hours(settings, datetime(2026, 1, 1, 23, 30))
    assert in_quiet_hours(settings, datetime(2026, 1, 1, 6, 59))
    assert not in_quiet_hours(settings, datetime(2026, 1, 1, 12, 0))
    settings["notifications"]["quiet_hours"]["enabled"] = False
    assert not in_quiet_hours(settings, datetime(2026, 1, 1, 23, 30))


def test_event_switches_decide_what_is_sent():
    events = {"any_change": False, "delivered": True, "problem": False, "stale": True}
    settings = {"notifications": {"events": events}}
    assert wanted(settings, "delivered")
    assert not wanted(settings, "problem")
    assert not wanted(settings, "in_transit")
    assert wanted(settings, "stale")
    events["any_change"] = True
    assert wanted(settings, "in_transit")


def test_messages_are_translated_and_held_during_quiet_hours(world):
    store, module, registry, _, parcel_id = world
    notifier = Notifier(store, registry, Translations({}), "mailto:test@example.org")
    sent = []

    async def fake_send(message, settings):
        sent.append(message)

    notifier.send = fake_send
    settings = store.get_settings()
    settings["language"] = "de"
    settings["notifications"]["quiet_hours"] = {"enabled": True, "from": "00:00", "to": "23:59"}
    store.save_settings(settings)

    module.answers = [
        TrackingResult(Status.IN_TRANSIT, "Unterwegs"),
        TrackingResult(Status.DELIVERED, "Übergeben"),
    ]
    refresh(store, registry, parcel_id)
    asyncio.run(notifier.handle(refresh(store, registry, parcel_id)))
    assert sent == []  # quiet: held back

    settings["notifications"]["quiet_hours"]["enabled"] = False
    store.save_settings(settings)
    asyncio.run(notifier.flush_held())
    assert [(m["title"], m["body"]) for m in sent] == [("Boots", "Zugestellt von Test Post.")]
    asyncio.run(notifier.flush_held())
    assert len(sent) == 1
