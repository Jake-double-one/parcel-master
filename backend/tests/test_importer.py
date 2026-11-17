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
from parcelmaster.carriers.de.amazon import parse_eta, parse_status, to_shipments
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


def test_a_number_the_user_already_tracks_is_not_added_twice(world):
    store = world[0]
    configure_post(store)
    store.save_shipment(
        None,
        {"title": "By hand", "direction": "incoming", "note": ""},
        [{"carrier": "test.post", "tracking_number": "PO123456"}],
    )
    sync(world, order(carrier_name="Post", tracking_number="PO123456"))
    numbers = [p["tracking_number"] for s in store.list_shipments(False) for p in s["parcels"]]
    assert numbers.count("PO123456") == 1


def test_failed_sign_in_is_not_retried_until_the_credentials_change(world):
    store, registry, shop, tmp_path = world
    own = store.get_settings()["carriers"]["test.shop"]
    assert importer.due(store, shop, own)
    shop.answer = CarrierError("unauthorized")
    asyncio.run(importer.run(store, registry, shop, tmp_path))
    assert importer.get_state(store, shop.id)["last_error"]["code"] == "unauthorized"
    assert not importer.due(store, shop, own)
    assert importer.due(store, shop, {**own, "password": "new"})


def test_other_errors_are_retried_after_the_interval(world):
    store, registry, shop, tmp_path = world
    own = store.get_settings()["carriers"]["test.shop"]
    shop.answer = CarrierError("unavailable")
    asyncio.run(importer.run(store, registry, shop, tmp_path))
    assert not importer.due(store, shop, own)  # not yet
    shop.sync_interval_minutes = 0
    assert importer.due(store, shop, own)
    assert not importer.due(store, shop, {})  # never without credentials


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
    item = SimpleNamespace(title="Stainless steel kettle with a very long product name " * 3)
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
    assert len(shipment.title) <= 80 and shipment.title.endswith("…")
    (parcel,) = shipment.parcels
    assert (parcel.key, parcel.status) == ("SHIP1", Status.IN_TRANSIT)
    assert (parcel.carrier_name, parcel.tracking_number) == ("DHL", "0034")
    assert parcel.url == "https://www.amazon.de/progress-tracker/package?shipmentId=SHIP1"
    assert parcel.eta_from == datetime(2026, 11, 18)
