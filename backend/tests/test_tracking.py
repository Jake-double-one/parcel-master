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
    delivery_window,
    next_delay_minutes,
    periodic_notices,
    refresh_parcel,
    resting,
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


def test_the_saver_asks_less_while_little_can_happen():
    now = datetime.now(UTC)
    soon, far = now + timedelta(hours=20), now + timedelta(days=3)
    # Only announced: every six hours instead of every two.
    assert next_delay_minutes(Status.REGISTERED, 120, now, saver=True) == 360
    assert next_delay_minutes(Status.REGISTERED, 120, now, saver=False) == 120
    # Days away: twice a day, so movement still shows.
    assert next_delay_minutes(Status.IN_TRANSIT, 120, now, far, saver=True) == 720
    # Twenty hours away is today or tomorrow: asked about more often, never less.
    assert next_delay_minutes(Status.IN_TRANSIT, 120, now, soon, saver=True) in (30, 60)
    assert next_delay_minutes(Status.IN_TRANSIT, 120, now, far, saver=False) == 120
    # On the day itself nothing is slowed down.
    assert next_delay_minutes(Status.OUT_FOR_DELIVERY, 120, now, far, saver=True) == 30


def test_night_rest_follows_the_saver_setting():
    def settings(enabled):
        saver = {"enabled": enabled, "from": "22:00", "to": "06:00"}
        return {"timezone": "UTC", "polling": {"saver": saver}}

    assert resting(settings(True), datetime(2026, 1, 1, 23, 30))
    assert resting(settings(True), datetime(2026, 1, 1, 5, 59))
    assert not resting(settings(True), datetime(2026, 1, 1, 6, 0))
    assert not resting(settings(False), datetime(2026, 1, 1, 23, 30))


def test_scheduler_asks_nobody_during_the_night_rest(world, monkeypatch):
    from parcelmaster import scheduler as scheduler_module
    from parcelmaster.scheduler import Scheduler

    store, module, registry, _, parcel_id = world
    module.answers = [TrackingResult(Status.IN_TRANSIT, "On its way")]

    class Quiet:
        async def handle(self, notices):
            pass

        async def flush_held(self):
            pass

    async def run(night):
        monkeypatch.setattr(scheduler_module, "resting", lambda settings: night)
        async with httpx.AsyncClient() as http:
            await Scheduler(store, registry, http, Quiet(), None).run_once()

    asyncio.run(run(night=True))
    assert module.calls == 0  # what is due waits for the morning
    asyncio.run(run(night=False))
    assert module.calls == 1


def test_a_new_delivery_window_is_news(world):
    store, module, registry, _, parcel_id = world
    # Not a parcel the user added a moment ago.
    store._db.execute("UPDATE parcels SET created_at='2026-01-01T00:00:00+00:00'")
    today = datetime.now().replace(hour=11, minute=30, second=0, microsecond=0)
    window = {"eta_from": today, "eta_to": today.replace(hour=13)}
    module.answers = [
        TrackingResult(Status.IN_TRANSIT, "On its way"),
        TrackingResult(Status.OUT_FOR_DELIVERY, "On the van", **window),
        TrackingResult(Status.OUT_FOR_DELIVERY, "On the van", **window),
    ]
    assert refresh(store, registry, parcel_id) == []
    notices = refresh(store, registry, parcel_id)
    assert [n.kind for n in notices] == ["delivery_window", "out_for_delivery"]
    assert notices[0].params == {"window": "11:30–13:30"}
    assert refresh(store, registry, parcel_id) == []  # the same window is not repeated

    notifier = Notifier(store, registry, Translations({}), "mailto:test@example.org")
    message = notifier.compose(notices[0], "de")
    assert message["body"] == "Test Post nennt Zustellfenster: 11:30–13:30"
    assert wanted(store.get_settings(), "delivery_window")


def test_a_date_without_a_time_is_not_a_window():
    from zoneinfo import ZoneInfo

    zone, now = ZoneInfo("Europe/Berlin"), datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
    same = "2026-11-18T00:00:00+00:00"
    assert delivery_window(same, same, zone, now) is None
    assert delivery_window(same, None, zone, now) is None
    assert delivery_window("2026-11-18T09:00:00+00:00", "2026-11-18T11:00:00+00:00", zone, now) == (
        "18.11. 10:00–12:00"
    )


def test_a_parcel_is_not_asked_about_twice_within_seconds(world, monkeypatch):
    store, module, registry, _, parcel_id = world
    monkeypatch.setattr("parcelmaster.tracking.MIN_GAP_SECONDS", 10)
    module.answers = [TrackingResult(Status.IN_TRANSIT, "x")] * 2
    refresh(store, registry, parcel_id)
    refresh(store, registry, parcel_id)
    assert module.calls == 1


def test_tries_have_an_hourly_ceiling_that_failures_count_towards(world, monkeypatch):
    store, module, registry, _, parcel_id = world
    monkeypatch.setattr("parcelmaster.tracking.TRIES_PER_HOUR", 4)
    module.answers = [CarrierError("unavailable")] * 20
    for _ in range(20):
        refresh(store, registry, parcel_id)
    assert module.calls == 4
    assert store.usage_today("test") == 0  # none of them was an answer
    parcel = store.get_parcel(parcel_id)
    assert parcel["last_error"]["code"] == "too_many_tries"
    assert parcel["next_check_at"] < (datetime.now(UTC) + timedelta(minutes=61)).isoformat()


def test_a_failure_is_retried_soon_and_never_rarer_than_the_usual_interval(world):
    store, module, registry, _, parcel_id = world
    waits = []
    module.answers = [CarrierError("unavailable")] * 6
    for _ in range(6):
        refresh(store, registry, parcel_id)
        due = datetime.fromisoformat(store.get_parcel(parcel_id)["next_check_at"])
        waits.append(round((due - datetime.now(UTC)).total_seconds() / 60))
    assert waits[:4] == [5, 15, 30, 60]
    assert max(waits) <= 120  # the default interval


def test_stops_left_are_kept_and_reported_at_marks_on_the_way_down(world, monkeypatch):
    store, module, registry, _, parcel_id = world

    def van(stops, about=False):
        return TrackingResult(
            Status.OUT_FOR_DELIVERY, "On the van", stops_left=stops, stops_about=about
        )

    # This round takes more lookups than the test budget.
    monkeypatch.setattr(type(module), "default_daily_budget", None)
    module.answers = [van(20, True), van(20, True), van(10, True), van(4), van(1)]
    module.answers.append(TrackingResult(Status.DELIVERED, "Done"))
    told = []
    for _ in range(6):
        told.append(
            [
                (n.params["count"], n.params["variant"])
                for n in refresh(store, registry, parcel_id)
                if n.kind == "stops"
            ]
        )
    assert told == [[(20, "about")], [], [(10, "about")], [(4, "body")], [(1, "one")], []]
    assert "stops_left" not in store.get_parcel(parcel_id)["extra"]  # gone once delivered


def test_a_module_may_ask_to_be_asked_again_soon(world):
    store, module, registry, _, parcel_id = world
    module.answers = [TrackingResult(Status.OUT_FOR_DELIVERY, "x", again_in_minutes=0.1)]
    refresh(store, registry, parcel_id)
    due = datetime.fromisoformat(store.get_parcel(parcel_id)["next_check_at"])
    assert 0.9 <= (due - datetime.now(UTC)).total_seconds() / 60 <= 1.1  # never under a minute


def test_a_window_drifting_by_minutes_is_not_reported_each_time(world, monkeypatch):
    store, module, registry, _, parcel_id = world
    monkeypatch.setattr(type(module), "default_daily_budget", None)
    store._db.execute("UPDATE parcels SET created_at='2026-01-01T00:00:00+00:00'")

    def van(start, end):
        day = datetime.now().replace(second=0, microsecond=0)
        return TrackingResult(
            Status.OUT_FOR_DELIVERY,
            "On the van",
            eta_from=day.replace(hour=start[0], minute=start[1]),
            eta_to=day.replace(hour=end[0], minute=end[1]),
        )

    module.answers = [
        van((12, 30), (14, 0)),
        van((12, 40), (14, 10)),  # ten minutes later: not worth a message
        van((12, 50), (14, 20)),  # twenty minutes from what was said: it is
        van((13, 0), (13, 30)),  # narrowed down
    ]
    told = [
        [
            n.params["window"][-11:]
            for n in refresh(store, registry, parcel_id)
            if n.kind == "delivery_window"
        ]
        for _ in range(4)
    ]
    assert told == [["12:30–14:00"], [], ["12:50–14:20"], ["13:00–13:30"]]


def test_live_news_stand_against_an_ordinary_answer_for_a_while(world, monkeypatch):
    store, module, registry, _, parcel_id = world
    monkeypatch.setattr(type(module), "default_daily_budget", None)
    day = datetime.now().replace(second=0, microsecond=0)
    plan = {"eta_from": day.replace(hour=12, minute=30), "eta_to": day.replace(hour=14, minute=0)}
    live = {"eta_from": day.replace(hour=12, minute=40), "eta_to": day.replace(hour=14, minute=10)}
    module.answers = [
        TrackingResult(Status.OUT_FOR_DELIVERY, "On the van", stops_left=10, **live),
        TrackingResult(Status.OUT_FOR_DELIVERY, "On the van", **plan),  # knows only the plan
    ]
    refresh(store, registry, parcel_id)
    assert [n.kind for n in refresh(store, registry, parcel_id)] == []  # nothing said twice
    parcel = store.get_parcel(parcel_id)
    assert parcel["extra"]["stops_left"] == "10"
    live_from = parcel["eta_from"]

    # Once the live news are old, the ordinary answer counts again.
    monkeypatch.setattr("parcelmaster.tracking.LIVE_STAYS", timedelta(0))
    module.answers = [TrackingResult(Status.OUT_FOR_DELIVERY, "On the van", **plan)]
    refresh(store, registry, parcel_id)
    parcel = store.get_parcel(parcel_id)
    assert "stops_left" not in parcel["extra"]
    assert parcel["eta_from"] < live_from  # back to the plan, ten minutes earlier


def test_a_parcel_is_asked_about_more_often_on_its_expected_day():
    from zoneinfo import ZoneInfo

    from parcelmaster.tracking import DELIVERY_DAY_MINUTES

    berlin = ZoneInfo("Europe/Berlin")
    today = datetime.now(berlin).replace(hour=0, minute=0, second=0, microsecond=0)
    for status in (Status.IN_TRANSIT, Status.REGISTERED):
        assert (
            next_delay_minutes(status, 120, None, today, saver=True, zone=berlin)
            == DELIVERY_DAY_MINUTES
        )
    tomorrow = today + timedelta(days=1)
    # The day before it is asked about hourly; further away the interval holds.
    assert next_delay_minutes(Status.IN_TRANSIT, 120, None, tomorrow, zone=berlin) == 60
    assert next_delay_minutes(Status.IN_TRANSIT, 30, None, tomorrow, zone=berlin) == 30
    later = today + timedelta(days=2)
    assert next_delay_minutes(Status.IN_TRANSIT, 120, None, later, zone=berlin) == 120


def test_postal_codes_are_masked_in_the_log():
    import logging

    from parcelmaster.journal import JOURNAL, setup

    setup()
    logging.getLogger("parcelmaster.carriers.de.dhl").warning(
        'answer <data _zip-code="12345"> for %s', "zip-code%3D%2212345%22"
    )
    logging.getLogger("httpx").info("GET /track?trackingNumber=1&recipientPostalCode=12345")
    lines = [entry["message"] for entry in list(JOURNAL.entries)[-2:]]
    assert all("12345" not in line for line in lines)
    assert '_zip-code="***"' in lines[0] and "recipientPostalCode=***" in lines[1]


def test_a_parcel_of_a_carrier_switched_off_is_not_looked_up(world):
    store, module, registry, _, parcel_id = world
    settings = store.get_settings()
    settings["carriers"]["test"]["enabled"] = False
    store.save_settings(settings)
    assert refresh(store, registry, parcel_id) == []
    after = store.get_parcel(parcel_id)
    assert after["last_error"]["code"] == "carrier_off" and after["error_count"] == 0
    assert module.calls == 0


def test_parcels_closed_on_the_vans_loading_are_taken_up_again():
    from parcelmaster.tracking import reopen_mistaken

    store = Store(None)
    loaded = (
        "Die Sendung wurde ins Zustellfahrzeug geladen und wird voraussichtlich heute zugestellt."
    )
    ids = []
    for number, text in (("H1", loaded), ("H2", "Die Sendung wurde zugestellt.")):
        shipment_id = store.save_shipment(
            None,
            {"title": number, "direction": "incoming", "note": ""},
            [{"carrier": "de.hermes", "tracking_number": number}],
        )
        parcel_id = store.get_shipment(shipment_id)["parcels"][0]["id"]
        store.update_parcel(parcel_id, status="delivered", delivered_at="2026-11-24T09:31:00+00:00")
        store.replace_events(
            parcel_id,
            [
                {
                    "timestamp": "2026-11-24T07:06:00+00:00",
                    "status": "in_transit",
                    "description": "Die Sendung ist in der Zielregion angekommen.",
                    "location": None,
                },
                {
                    "timestamp": "2026-11-24T09:15:00+00:00",
                    "status": "delivered",
                    "description": text,
                    "location": None,
                },
            ],
        )
        ids.append(parcel_id)
    assert reopen_mistaken(store) == 1
    wrong, right = (store.get_parcel(parcel_id) for parcel_id in ids)
    assert wrong["status"] == "in_transit" and wrong["delivered_at"] is None
    assert store.due_parcels(datetime.now(UTC).isoformat(), 10) == [ids[0]]
    assert right["status"] == "delivered"
    assert reopen_mistaken(store) == 0
