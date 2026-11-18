import asyncio
import re
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from parcelmaster import importer
from parcelmaster.carriers.base import (
    CarrierError,
    CarrierModule,
    Field,
    ImportedParcel,
    ImportedShipment,
    ImporterModule,
)
from parcelmaster.carriers.de.amazon import (
    MODULE as AMAZON,
)
from parcelmaster.carriers.de.amazon import (
    clean_otp_secret,
    parse_eta,
    parse_status,
    to_shipments,
)
from parcelmaster.carriers.registry import Registry
from parcelmaster.db import Store
from parcelmaster.status import Status
from parcelmaster.tracking import NEVER


class Shop(ImporterModule):
    id = "test.shop"
    name = "Shop"
    settings_fields = (Field("email", required=True), Field("password", type="secret"))

    def __init__(self):
        self.answer = []
        self.calls = 0

    def sync(self, request):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Post(CarrierModule):
    id = "test.post"
    name = "Post"
    patterns = (re.compile(r"PO\d{6}"),)
    settings_fields = (Field("api_key", type="secret", required=True),)

    def tracking_url(self, tracking_number, extra, language):
        return f"https://post.example/{tracking_number}"


@pytest.fixture
def world(tmp_path):
    store = Store(None)
    shop, post = Shop(), Post()
    registry = Registry([shop, post], {})
    settings = store.get_settings()
    settings["carriers"] = {"test.shop": {"email": "me@example.org", "password": "pw"}}
    # Most tests here are about the hand-over itself, so it happens at once.
    settings["polling"]["saver"]["enabled"] = False
    store.save_settings(settings)
    return store, registry, shop, tmp_path


def order(reference="302-1", title="Kettle", **parcel):
    values = {"key": "s1", "status": Status.IN_TRANSIT, "description": "Arriving tomorrow"}
    return ImportedShipment(reference, title, [ImportedParcel(**{**values, **parcel})])


def sync(world, *answer):
    store, registry, shop, tmp_path = world
    shop.answer = list(answer)
    return asyncio.run(importer.run(store, registry, shop, tmp_path))


def configure_post(store):
    settings = store.get_settings()
    settings["carriers"]["test.post"] = {"api_key": "k"}
    store.save_settings(settings)


def test_open_order_becomes_a_shipment_tracked_by_the_shop(world):
    store = world[0]
    assert sync(world, order(eta_from=datetime(2026, 11, 18))) == []  # the first sight is not news
    (shipment,) = store.list_shipments(False)
    assert (shipment["source"], shipment["reference"], shipment["title"]) == (
        "Shop",
        "302-1",
        "Kettle",
    )
    (parcel,) = shipment["parcels"]
    assert parcel["carrier"] == "test.shop"
    assert parcel["status"] == "in_transit"
    assert parcel["eta_from"] is not None
    assert parcel["next_check_at"] == NEVER  # no lookups of its own
    assert store.due_parcels("2100-01-01", 10) == []


def test_orders_that_already_arrived_are_not_listed(world):
    sync(world, order(status=Status.DELIVERED, description="Delivered"))
    assert world[0].list_shipments(False) == []


def test_status_change_from_the_shop_is_news_and_history_grows(world):
    store = world[0]
    sync(world, order())
    notices = sync(world, order(status=Status.OUT_FOR_DELIVERY, description="Out for delivery"))
    assert [n.kind for n in notices] == ["out_for_delivery"]
    assert sync(world, order(status=Status.OUT_FOR_DELIVERY, description="Out for delivery")) == []
    parcel = store.list_shipments(False)[0]["parcels"][0]
    assert [e["description"] for e in parcel["events"]] == ["Out for delivery", "Arriving tomorrow"]


def test_parcel_is_handed_over_once_the_carrier_is_known_and_set_up(world):
    store = world[0]
    sync(world, order())
    before = store.list_shipments(False)[0]["parcels"][0]

    # The carrier is known, but its module has no key yet: the shop keeps the parcel.
    sync(world, order(carrier_name="Post", tracking_number="PO123456"))
    waiting = store.list_shipments(False)[0]["parcels"][0]
    assert waiting["id"] == before["id"]
    assert (waiting["carrier"], waiting["tracking_number"]) == ("test.shop", "PO123456")
    assert waiting["extra"]["carrier_name"] == "Post"

    configure_post(store)
    sync(world, order(carrier_name="Post", tracking_number="po 123456"))
    handed = store.list_shipments(False)[0]["parcels"][0]
    assert handed["id"] == before["id"]  # still the same entry in the overview
    assert (handed["carrier"], handed["tracking_number"]) == ("test.post", "PO123456")
    assert handed["extra"]["import_key"] == "s1"
    assert store.due_parcels("2100-01-01", 10) == [handed["id"]]  # the carrier is asked next
    # Until the carrier answers, the shop's status stays visible.
    assert handed["status"] == "in_transit"


def test_second_parcel_joins_the_same_shipment(world):
    store = world[0]
    sync(world, order())
    two = ImportedShipment(
        "302-1",
        "Kettle",
        [
            ImportedParcel("s1", Status.DELIVERED, "Delivered"),
            ImportedParcel("s2", Status.IN_TRANSIT, "Arriving Friday"),
        ],
    )
    sync(world, two)
    (shipment,) = store.list_shipments(False)
    assert [p["status"] for p in shipment["parcels"]] == ["delivered", "in_transit"]


def test_archived_and_renamed_shipments_are_left_alone(world):
    store = world[0]
    sync(world, order())
    shipment = store.list_shipments(False)[0]
    store.save_shipment(
        shipment["id"],
        {"title": "My kettle", "direction": "incoming", "note": ""},
        shipment["parcels"],
    )
    sync(world, order(title="Kettle 1.7 l stainless"))
    assert store.list_shipments(False)[0]["title"] == "My kettle"

    store.set_archived(shipment["id"], True)
    sync(world, order())
    assert store.list_shipments(False) == []
    assert len(store.list_shipments(True)) == 1


def test_a_number_the_user_already_tracks_is_not_looked_up_twice(world):
    store = world[0]
    configure_post(store)
    store.save_shipment(
        None,
        {"title": "By hand", "direction": "incoming", "note": ""},
        [{"carrier": "test.post", "tracking_number": "PO123456"}],
    )
    sync(world, order(carrier_name="Post", tracking_number="PO123456"))
    by_title = {s["title"]: s["parcels"] for s in store.list_shipments(False)}
    # The order still shows up, but its parcel stays with the shop instead of
    # asking the carrier a second time.
    assert [(p["carrier"], p["tracking_number"]) for p in by_title["Kettle"]] == [
        ("test.shop", "PO123456")
    ]
    assert [p["carrier"] for p in by_title["By hand"]] == ["test.post"]


def test_two_orders_in_one_box_both_show_their_parcel(world):
    store = world[0]
    box = {"status": Status.IN_TRANSIT, "carrier_name": "Post", "tracking_number": "PO123456"}
    sync(world, order("302-1", "Kettle", key="a", **box), order("302-2", "Cable", key="b", **box))
    shipments = {s["reference"]: s["parcels"] for s in store.list_shipments(False)}
    assert len(shipments["302-1"]) == 1 and len(shipments["302-2"]) == 1

    # Once the carrier is set up, one of them is handed over; the other stays visible.
    configure_post(store)
    sync(world, order("302-1", "Kettle", key="a", **box), order("302-2", "Cable", key="b", **box))
    shipments = {s["reference"]: s["parcels"] for s in store.list_shipments(False)}
    assert [p["carrier"] for p in shipments["302-1"]] == ["test.post"]
    assert [p["carrier"] for p in shipments["302-2"]] == ["test.shop"]
    assert all(p[0]["tracking_number"] == "PO123456" for p in shipments.values())


CREDENTIALS = {"email": "me@example.org", "password": "pw"}


def connect(world, answer, credentials=CREDENTIALS):
    store, registry, shop, tmp_path = world
    shop.answer = answer
    return asyncio.run(importer.connect(store, registry, shop, tmp_path, credentials))


def test_connecting_checks_the_sign_in_and_keeps_only_working_credentials(world):
    store, registry, shop, tmp_path = world
    settings = store.get_settings()
    settings["carriers"] = {}
    store.save_settings(settings)

    error = connect(world, CarrierError("sign_in_failed", "wrong password"))
    assert (error.code, error.detail) == ("sign_in_failed", "wrong password")
    assert "test.shop" not in store.get_settings()["carriers"]  # nothing was kept
    assert not importer.get_state(store, shop.id).get("connected")
    assert not importer.due(store, shop, CREDENTIALS)

    assert connect(world, [order()]) is None
    assert store.get_settings()["carriers"]["test.shop"] == CREDENTIALS
    state = importer.get_state(store, shop.id)
    assert (state["connected"], state["account"], state["found"]) == (True, "me@example.org", 1)
    assert len(store.list_shipments(False)) == 1  # the check already imported


def test_only_a_connected_account_is_synced_on_its_own(world):
    store, registry, shop, tmp_path = world
    assert not importer.due(store, shop, CREDENTIALS)  # saved, but never checked
    connect(world, [])
    assert not importer.due(store, shop, CREDENTIALS)  # just synced
    shop.sync_interval_minutes = 0
    assert importer.due(store, shop, CREDENTIALS)


def test_a_sign_in_that_starts_failing_is_not_retried_automatically(world):
    store, registry, shop, tmp_path = world
    connect(world, [])
    shop.sync_interval_minutes = 0
    shop.answer = CarrierError("sign_in_failed")
    asyncio.run(importer.run(store, registry, shop, tmp_path))
    assert importer.get_state(store, shop.id)["last_error"]["code"] == "sign_in_failed"
    assert not importer.due(store, shop, CREDENTIALS)
    # Asking by hand still works, and a success lifts the block.
    shop.answer = []
    asyncio.run(importer.run(store, registry, shop, tmp_path))
    assert importer.due(store, shop, CREDENTIALS)


def test_other_errors_are_retried_after_the_interval(world):
    store, registry, shop, tmp_path = world
    connect(world, [])
    shop.answer = CarrierError("unavailable")
    asyncio.run(importer.run(store, registry, shop, tmp_path))
    assert not importer.due(store, shop, CREDENTIALS)  # not yet
    shop.sync_interval_minutes = 0
    assert importer.due(store, shop, CREDENTIALS)


def test_disconnecting_forgets_the_account_but_keeps_the_shipments(world):
    store, registry, shop, tmp_path = world
    connect(world, [order()])
    session_file = tmp_path / "modules" / shop.id / "cookies.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text("{}")

    importer.disconnect(store, shop, tmp_path)
    assert "test.shop" not in store.get_settings()["carriers"]
    assert importer.get_state(store, shop.id) == {}
    assert not session_file.exists()
    assert len(store.list_shipments(False)) == 1


def test_amazon_status_lines():
    assert parse_status("Delivered 14 November") is Status.DELIVERED
    assert parse_status("Arriving tomorrow") is Status.IN_TRANSIT
    assert parse_status("Out for delivery") is Status.OUT_FOR_DELIVERY
    assert parse_status("Not yet dispatched") is Status.REGISTERED
    assert parse_status("Running late") is Status.PROBLEM
    assert parse_status("Ready for collection") is Status.READY_FOR_PICKUP
    assert parse_status("Refunded") is Status.RETURNED
    assert parse_status(None) is Status.UNKNOWN


def test_amazon_arrival_dates():
    today = date(2026, 11, 17)  # a Tuesday
    assert parse_eta("Arriving tomorrow", today) == datetime(2026, 11, 18)
    assert parse_eta("Arriving today by 9pm", today) == datetime(2026, 11, 17)
    assert parse_eta("Arriving Thursday", today) == datetime(2026, 11, 19)
    assert parse_eta("Now arriving 20 November", today) == datetime(2026, 11, 20)
    assert parse_eta("Arriving November 20 - November 22", today) == datetime(2026, 11, 20)
    assert parse_eta("Arriving 3 January", date(2026, 12, 20)) == datetime(2027, 1, 3)
    assert parse_eta("Delivered 14 November", today) is None


def test_amazon_orders_are_turned_into_shipments():
    item = SimpleNamespace(title="Stainless steel kettle with a very long product name " * 5)
    orders = [
        SimpleNamespace(
            order_number="302-1111111-2222222",
            cancelled=False,
            items=[item],
            shipments=[
                SimpleNamespace(
                    delivery_status="Arriving tomorrow",
                    shipment_id="SHIP1",
                    tracking_link="/progress-tracker/package?shipmentId=SHIP1",
                    items=[item],
                ),
                SimpleNamespace(
                    delivery_status=None, shipment_id=None, tracking_link=None, items=[]
                ),
            ],
        ),
        SimpleNamespace(order_number="302-9", cancelled=True, items=[], shipments=[]),
    ]
    tracking = {
        ("302-1111111-2222222", "SHIP1"): SimpleNamespace(carrier="DHL", tracking_number="0034")
    }
    (shipment,) = to_shipments(orders, tracking, date(2026, 11, 17))
    assert shipment.reference == "302-1111111-2222222"
    assert len(shipment.title) <= 200 and shipment.title.endswith("…")
    (parcel,) = shipment.parcels
    assert (parcel.key, parcel.status) == ("SHIP1", Status.IN_TRANSIT)
    assert (parcel.carrier_name, parcel.tracking_number) == ("DHL", "0034")
    assert parcel.url == "https://www.amazon.de/progress-tracker/package?shipmentId=SHIP1"
    assert parcel.eta_from == datetime(2026, 11, 18)


def test_two_step_secret_is_checked_before_any_sign_in(tmp_path):
    key = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
    assert clean_otp_secret("jbsw y3dp ehpk 3pxp jbsw y3dp ehpk 3pxp") == key
    assert clean_otp_secret(f"otpauth://totp/Amazon:me?secret={key}&issuer=Amazon") == key
    assert clean_otp_secret("") is None and clean_otp_secret(None) is None
    for wrong in ("123456", "not a key at all, sorry!", "otpauth://totp/Amazon:me"):
        with pytest.raises(CarrierError) as caught:
            clean_otp_secret(wrong)
        assert caught.value.code == "invalid_otp_secret"

    # The module refuses such a key itself, without contacting Amazon.
    from parcelmaster.carriers.base import SyncRequest

    request = SyncRequest(
        {"email": "a@example.org", "password": "x", "otp_secret": "123456"},
        "en",
        tmp_path / "state",
    )
    with pytest.raises(CarrierError) as caught:
        AMAZON.sync(request)
    assert caught.value.code == "invalid_otp_secret"
    assert not (tmp_path / "state").exists()


TRACKING_PAGE = """
<div class="pt-main-container">
  <div id="tracking-events-container">
    <h2 class="tracking-event-carrier-header">Delivered by DHL</h2>
    <div class="a-row tracking-event-date-header">Tuesday, 17 November</div>
    <div class="a-row">
      <span class="tracking-event-time">8:07 PM</span>
      <span class="tracking-event-message">Package delivered</span>
      <span class="tracking-event-location">Cologne, DE</span>
    </div>
    <div class="a-row">
      <span class="tracking-event-time">7:12 AM</span>
      <span class="tracking-event-message">Out for delivery</span>
    </div>
    <div class="a-row tracking-event-date-header">Monday, 16 November</div>
    <div class="a-row">
      <span class="tracking-event-time">22:40</span>
      <span class="tracking-event-message">Package arrived at a carrier facility</span>
      <span class="tracking-event-location">Hagen, DE</span>
    </div>
  </div>
</div>
"""


def test_amazon_tracking_page_becomes_a_history():
    from bs4 import BeautifulSoup

    from parcelmaster.carriers.de.amazon import parse_events

    page = BeautifulSoup(TRACKING_PAGE, "html.parser")
    events = parse_events(page, date(2026, 11, 17))
    assert [(e.timestamp, e.status, e.description, e.location) for e in events] == [
        (
            datetime(2026, 11, 16, 22, 40),
            Status.IN_TRANSIT,
            "Package arrived at a carrier facility",
            "Hagen, DE",
        ),
        (datetime(2026, 11, 17, 7, 12), Status.OUT_FOR_DELIVERY, "Out for delivery", None),
        (datetime(2026, 11, 17, 20, 7), Status.DELIVERED, "Package delivered", "Cologne, DE"),
    ]
    # A page without a history leaves the stored one alone.
    assert (
        parse_events(
            BeautifulSoup("<div>Arriving tomorrow</div>", "html.parser"), date(2026, 11, 17)
        )
        is None
    )
    assert parse_events(None, date(2026, 11, 17)) is None


def test_amazon_parcels_list_their_articles():
    kettle = SimpleNamespace(title="Kettle  1.7 l", quantity=1)
    filters = SimpleNamespace(title="Water filter", quantity=3)
    orders = [
        SimpleNamespace(
            order_number="302-1",
            cancelled=False,
            items=[kettle, filters],
            shipments=[
                SimpleNamespace(
                    delivery_status="Arriving tomorrow",
                    shipment_id="S1",
                    tracking_link=None,
                    items=[kettle, filters],
                )
            ],
        )
    ]
    (shipment,) = to_shipments(orders, {}, date(2026, 11, 17))
    assert shipment.parcels[0].items == ["Kettle 1.7 l", "3 × Water filter"]
    assert shipment.parcels[0].events is None


def test_contents_and_full_history_from_the_shop_are_stored(world):
    from parcelmaster.carriers.base import TrackingEvent

    store = world[0]
    sync(world, order(items=["Kettle", "3 × Water filter"]))
    parcel = store.list_shipments(False)[0]["parcels"][0]
    assert parcel["extra"]["items"] == '["Kettle", "3 × Water filter"]'
    assert len(parcel["events"]) == 1  # no history from the shop yet: one line per change

    history = [
        TrackingEvent(
            datetime(2026, 11, 16, 22, 40), Status.IN_TRANSIT, "Arrived at facility", "Hagen"
        ),
        TrackingEvent(datetime(2026, 11, 17, 7, 12), Status.OUT_FOR_DELIVERY, "Out for delivery"),
    ]
    sync(
        world,
        order(
            status=Status.OUT_FOR_DELIVERY,
            description="Out for delivery",
            items=["Kettle"],
            events=history,
        ),
    )
    parcel = store.list_shipments(False)[0]["parcels"][0]
    assert [(e["description"], e["location"]) for e in parcel["events"]] == [
        ("Out for delivery", None),
        ("Arrived at facility", "Hagen"),
    ]
    assert parcel["last_event_at"].startswith("2026-11-17T06:12")  # Berlin time, stored as UTC


def test_hand_over_fills_the_carriers_fields_from_the_users_defaults(world):
    store = world[0]
    post = world[1].get("test.post")
    post.parcel_fields = (Field("postal_code"),)
    try:
        settings = store.get_settings()
        settings["carriers"]["test.post"] = {"api_key": "k", "default_postal_code": "50667"}
        store.save_settings(settings)
        sync(world, order(carrier_name="Post", tracking_number="PO123456"))
        parcel = store.list_shipments(False)[0]["parcels"][0]
        assert parcel["carrier"] == "test.post"
        assert parcel["extra"]["postal_code"] == "50667"

        # What the user corrects by hand survives the next sync.
        store.set_parcel_extra(parcel["id"], {**parcel["extra"], "postal_code": "10115"})
        sync(world, order(carrier_name="Post", tracking_number="PO123456"))
        assert store.list_shipments(False)[0]["parcels"][0]["extra"]["postal_code"] == "10115"
    finally:
        post.parcel_fields = ()


def test_amazon_status_lines_are_translated_and_unknown_ones_kept():
    from parcelmaster.carriers.registry import discover
    from parcelmaster.i18n import Translations

    registry = discover()
    translations = Translations(registry.locales)
    amazon = registry.get("de.amazon")

    def german(text):
        return translations.localize(amazon, text, "de")

    assert german("Delivered today") == "Heute zugestellt"
    assert german("Delivered 14 November") == "Am 14. November zugestellt"
    assert german("Arriving tomorrow") == "Kommt morgen"
    assert german("Arriving tomorrow by 9 PM") == "Kommt morgen bis 21:00 Uhr"
    assert german("Arriving Thursday") == "Kommt am Donnerstag"
    assert german("Now arriving 20 November") == "Kommt am 20. November"
    assert german("Arriving November 20 - November 22") == (
        "Kommt zwischen dem 20. November und dem 22. November"
    )
    assert german("Out for delivery") == "In Zustellung"
    assert german("Package arrived at a carrier facility.") == (
        "Paket ist beim Paketdienst angekommen"
    )
    # Unknown wording and unknown languages fall back to Amazon's own text.
    assert german("Something Amazon never said before") == "Something Amazon never said before"
    assert translations.localize(amazon, "Delivered today", "en") == "Delivered today"
    assert translations.localize(amazon, "Delivered today", "fr") == "Delivered today"
    # Carriers that answer in the user's language are passed through.
    assert translations.localize(registry.get("de.dhl"), "Zugestellt", "de") == "Zugestellt"


def test_translated_status_reaches_overview_and_notifications(world):
    from parcelmaster.i18n import Translations
    from parcelmaster.notify import Notifier
    from parcelmaster.tracking import Notice

    store, registry, shop, _ = world
    shop.localize = lambda text, phrase: {"Out for delivery": "In Zustellung"}.get(text, text)
    try:
        settings = store.get_settings()
        settings["language"] = "de"
        store.save_settings(settings)
        sync(world, order())
        parcel = store.list_shipments(False)[0]["parcels"][0]
        notifier = Notifier(store, registry, Translations({}), "mailto:test@example.org")
        message = notifier.compose(
            Notice("problem", parcel, {"description": "Out for delivery"}), "de"
        )
        assert message["body"] == "Problem bei Shop: In Zustellung"
    finally:
        del shop.localize


def test_orders_that_arrived_recently_are_listed_too(world):
    from datetime import UTC, timedelta

    from parcelmaster.carriers.de.amazon import parse_delivery_day

    store = world[0]
    now = datetime.now(UTC)
    today = order(
        "302-today",
        status=Status.DELIVERED,
        description="Delivered today",
        status_time=now.replace(tzinfo=None) - timedelta(hours=3),
    )
    last_week = order(
        "302-old",
        status=Status.DELIVERED,
        description="Delivered 9 November",
        status_time=now.replace(tzinfo=None) - timedelta(days=8),
    )
    undated = order("302-undated", status=Status.DELIVERED, description="Delivered")
    sync(world, today, last_week, undated)

    (shipment,) = store.list_shipments(False)
    assert shipment["reference"] == "302-today"
    parcel = shipment["parcels"][0]
    assert parcel["status"] == "delivered"
    # Arrival is dated by the service, so the shipment is archived on schedule.
    assert parcel["delivered_at"] < now.isoformat()
    assert parcel["events"][0]["timestamp"] == parcel["delivered_at"]

    assert parse_delivery_day("Delivered today", date(2026, 11, 17)) == datetime(2026, 11, 17)
    assert parse_delivery_day("Delivered 14 November", date(2026, 11, 17)) == datetime(2026, 11, 14)
    assert parse_delivery_day("Arriving today", date(2026, 11, 17)) is None


def test_an_old_amazon_order_without_parcels_is_kept_when_asked_for():
    item = SimpleNamespace(title="Kettle", quantity=1)
    old = SimpleNamespace(order_number="302-1", cancelled=False, items=[item], shipments=[])
    assert to_shipments([old], {}, date(2026, 11, 17)) == []
    (shipment,) = to_shipments([old], {}, date(2026, 11, 17), keep_empty=True)
    assert shipment.title == "Kettle"
    assert [(p.status, p.items) for p in shipment.parcels] == [(Status.DELIVERED, ["Kettle"])]
    assert (
        AMAZON.extract_from_url(
            "https://www.amazon.de/gp/your-account/order-details?orderID=302-1234567-7654321"
        )
        == "302-1234567-7654321"
    )
    assert AMAZON.extract_from_url("https://example.com/?orderID=302-1234567-7654321") is None


def test_with_the_saver_a_parcel_goes_to_a_scarce_carrier_only_once_it_matters(world, monkeypatch):
    store = world[0]
    configure_post(store)
    monkeypatch.setattr(Post, "scarce", lambda self, settings: True)
    settings = store.get_settings()
    settings["polling"]["saver"]["enabled"] = True
    store.save_settings(settings)

    def parcel():
        return store.list_shipments(False)[0]["parcels"][0]

    shipped = {"carrier_name": "Post", "tracking_number": "PO123456"}
    sync(world, order(**shipped))
    assert parcel()["carrier"] == "test.shop"  # on its way: the shop's word is enough
    assert store.due_parcels("2100-01-01", 10) == []  # and costs no lookup

    sync(world, order(status=Status.OUT_FOR_DELIVERY, description="Out for delivery", **shipped))
    assert parcel()["carrier"] == "test.post"  # today the carrier knows the time window

    # Once handed over it stays there, whatever the shop says next.
    sync(world, order(**shipped))
    assert parcel()["carrier"] == "test.post"


def test_with_the_saver_a_carrier_without_scarce_lookups_takes_over_at_once(world):
    store = world[0]
    configure_post(store)
    settings = store.get_settings()
    settings["polling"]["saver"]["enabled"] = True
    store.save_settings(settings)
    sync(world, order(carrier_name="Post", tracking_number="PO123456"))
    assert store.list_shipments(False)[0]["parcels"][0]["carrier"] == "test.post"


def test_a_parcel_without_a_tracking_page_is_still_read():
    from types import SimpleNamespace

    from parcelmaster.carriers.de.amazon import _read_tracking

    # No link at all, and a link whose page cannot be parsed: neither may stop the sync.
    bare = SimpleNamespace(shipment_id=None, tracking_link=None)
    assert _read_tracking(None, None, "028-1", bare) is None
    linked = SimpleNamespace(shipment_id=None, tracking_link="/gp/your-account/ship-track?itemId=1")
    client = SimpleNamespace(config=None, _parse_tracking=lambda parsed, config: None)
    session = SimpleNamespace(get=lambda url: SimpleNamespace(parsed=None))
    assert _read_tracking(client, session, "028-1", linked) is None

    from bs4 import BeautifulSoup

    # The card from amazon.de, outside of what the library keeps of the page.
    html = (
        '<section class="pt-card delivery-card"><h3>Versendet mit DHL</h3>'
        '<div class="pt-delivery-card-trackingId">Trackingnummer JJD000390016811040829</div>'
        "</section>"
    )
    session = SimpleNamespace(
        get=lambda url: SimpleNamespace(parsed=BeautifulSoup(html, "html.parser"))
    )
    from parcelmaster.carriers.de.amazon import tracking_facts

    found = _read_tracking(client, session, "028-1", linked)
    assert tracking_facts(found) == ("DHL", "JJD000390016811040829")


def test_carrier_and_number_are_read_from_the_words_if_the_markup_is_unknown():
    from types import SimpleNamespace

    from bs4 import BeautifulSoup

    from parcelmaster.carriers.de.amazon import tracking_facts

    def page(html):
        soup = BeautifulSoup(html, "html.parser")
        return SimpleNamespace(carrier=None, tracking_number=None, parsed=soup)

    german = page(
        "<div><h2>Versendet mit DHL</h2><p>Trackingnummer JJD000390016811040829</p></div>"
    )
    assert tracking_facts(german) == ("DHL", "JJD000390016811040829")
    english = page("<div><h2>Shipped with DHL</h2><span>Tracking ID: JJD0003900168</span></div>")
    assert tracking_facts(english) == ("DHL", "JJD0003900168")
    british = page(
        '<div class="tracking-event-carrier-header"><h2>Dispatched with DHL</h2></div>'
        '<div class="pt-delivery-card-trackingId">Tracking ID: JJD0003900168</div>'
    )
    assert tracking_facts(british) == ("DHL", "JJD0003900168")
    assert tracking_facts(page("<p>Arriving Monday</p>")) == (None, None)
    known = SimpleNamespace(carrier="UPS", tracking_number="1Z999", parsed=None)
    assert tracking_facts(known) == ("UPS", "1Z999")
    assert tracking_facts(None) == (None, None)


def test_history_is_read_from_the_window_behind_all_updates():
    from bs4 import BeautifulSoup

    from parcelmaster.carriers.de.amazon import parse_events

    def row(time, message, place):
        return (
            f'<div><span class="tracking-event-time">{time}</span>'
            f'<span class="tracking-event-message">{message}</span>'
            f'<span class="tracking-event-location">{place}</span></div>'
        )

    def day(name):
        return f'<div><span class="tracking-event-date">{name}</span></div>'

    html = (
        '<div id="tracking-events-container">'
        + day("Mittwoch, 18. November")
        + row("9:35", "Sendung ist beim Verteilzentrum eingegangen.", "DE")
        + row("4:32", "Sendung ist beim Verteilzentrum eingegangen.", "XX, XX ")
        + day("Tuesday, November 17")
        + row("2:55 PM", "Package arrived at a carrier facility", "DE")
        + row("", "Package left the shipper facility", "")
        + "</div>"
    )
    events = parse_events(BeautifulSoup(html, "html.parser"), date(2026, 11, 18))
    assert [(e.timestamp, e.location) for e in events] == [
        (datetime(2026, 11, 17, 0, 0), None),
        (datetime(2026, 11, 17, 14, 55), "DE"),
        (datetime(2026, 11, 18, 4, 32), None),
        (datetime(2026, 11, 18, 9, 35), "DE"),
    ]


def test_the_progress_bar_tells_the_step_in_any_language():
    from bs4 import BeautifulSoup

    from parcelmaster.carriers.de.amazon import _settle_status, milestone_status

    def bar(reached, steps=4):
        return BeautifulSoup(
            "".join(
                f'<div class="pt-status-milestone" data-reached="{str(i < reached).lower()}">'
                '<div class="pt-status-milestone-label">x</div></div>'
                for i in range(steps)
            ),
            "html.parser",
        )

    assert milestone_status(bar(2)) is Status.IN_TRANSIT
    assert milestone_status(bar(3)) is Status.OUT_FOR_DELIVERY
    assert milestone_status(bar(4)) is Status.DELIVERED
    assert milestone_status(bar(2, steps=5)) is None  # an unknown bar is not guessed at
    assert _settle_status(Status.UNKNOWN, bar(3)) is Status.OUT_FOR_DELIVERY
    assert _settle_status(Status.PROBLEM, bar(2)) is Status.PROBLEM
    assert _settle_status(Status.IN_TRANSIT, None) is Status.IN_TRANSIT


def test_a_parcel_that_arrived_is_left_alone_by_later_syncs(world):
    from parcelmaster.importer import settled_keys

    store = world[0]
    arrived = {
        "status": Status.DELIVERED,
        "description": "Delivered",
        "status_time": datetime.now(),
    }
    sync(world, order(tracking_number="AB1", **arrived))
    before = store.list_shipments(False)[0]["parcels"][0]
    assert settled_keys(store) == {before["extra"]["import_key"]}
    # The shop's page for it is no longer read, so the next sync knows less. Nothing is lost.
    sync(world, order(**arrived))
    after = store.list_shipments(False)[0]["parcels"][0]
    assert (after["tracking_number"], after["events"]) == (
        before["tracking_number"],
        before["events"],
    )
