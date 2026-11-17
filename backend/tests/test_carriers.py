import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from parcelmaster.carriers.base import CarrierError, TrackRequest
from parcelmaster.carriers.de.dhl import MODULE as DHL
from parcelmaster.carriers.de.dhl import parse_response
from parcelmaster.carriers.demo import MODULE as DEMO
from parcelmaster.carriers.registry import discover
from parcelmaster.status import Status, aggregate

FIXTURES = Path(__file__).parent / "fixtures"


def request(number="00340434161094042557", **settings):
    return TrackRequest(
        number, {"postal_code": "50667"}, settings, "de", datetime.now().astimezone()
    )


def test_discovery_finds_modules_and_their_texts():
    registry = discover()
    assert {"de.dhl", "demo"} <= {module.id for module in registry.all()}
    assert registry.locales["de"]["carrier.de.dhl.settings.api_key.label"] == "API-Schlüssel"


def test_dhl_recognises_its_numbers_and_links():
    assert DHL.matches("0034 0434 1610 9404 2557")
    assert DHL.matches("JJD000390007812345678")
    assert not DHL.matches("DEMO1234")
    link = (
        "https://www.dhl.de/de/privatkunden/pakete-empfangen/verfolgen.html?piececode=003404341610"
    )
    assert DHL.extract_from_url(link) == "003404341610"
    assert DHL.extract_from_url("https://example.com/?piececode=003404341610") is None
    assert "piececode=123&zip=50667" in DHL.tracking_url("123", {"postal_code": "50667"}, "de")


def test_dhl_response_is_mapped_to_the_common_model():
    result = parse_response(json.loads((FIXTURES / "dhl_transit.json").read_text()))
    # The API says "transit"; only the text reveals it is on the van.
    assert result.status is Status.OUT_FOR_DELIVERY
    assert result.location == "Köln"
    assert result.eta_from == datetime(2026, 11, 16, 11, 30)
    assert result.eta_to == datetime(2026, 11, 16, 13, 30)
    assert [event.status for event in result.events] == [
        Status.OUT_FOR_DELIVERY,
        Status.IN_TRANSIT,
        Status.REGISTERED,
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Die Sendung liegt in der PACKSTATION 123 zur Abholung bereit.", Status.READY_FOR_PICKUP),
        ("The shipment is ready for pick-up at the parcel shop.", Status.READY_FOR_PICKUP),
        ("Die Sendung wird zurück an den Absender geschickt.", Status.RETURNED),
        ("Die Sendung wurde im Paketzentrum bearbeitet.", Status.IN_TRANSIT),
    ],
)
def test_dhl_fine_grained_status_from_text(text, expected):
    payload = {"shipments": [{"status": {"statusCode": "transit", "description": text}}]}
    assert parse_response(payload).status is expected


async def run_dhl(handler, **settings):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        return await DHL.track(request(**settings), http)


@pytest.mark.anyio
async def test_dhl_sends_key_and_postal_code():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["key"] = req.headers["DHL-API-Key"]
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json=json.loads((FIXTURES / "dhl_transit.json").read_text()))

    result = await run_dhl(handler, api_key="secret")
    assert result.status is Status.OUT_FOR_DELIVERY
    assert seen["key"] == "secret"
    assert seen["params"]["recipientPostalCode"] == "50667"
    assert seen["params"]["trackingNumber"] == "00340434161094042557"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "unauthorized"), (404, "not_found"), (429, "rate_limited"), (503, "unavailable")],
)
async def test_dhl_errors_become_readable_codes(status, code):
    with pytest.raises(CarrierError) as caught:
        await run_dhl(lambda req: httpx.Response(status, json={}), api_key="secret")
    assert caught.value.code == code


@pytest.mark.anyio
async def test_dhl_without_key_makes_no_request():
    def handler(req):
        raise AssertionError("no request expected")

    with pytest.raises(CarrierError) as caught:
        await run_dhl(handler)
    assert caught.value.code == "not_configured"


@pytest.mark.parametrize(
    ("number", "status"),
    [
        ("DEMO0001", Status.REGISTERED),
        ("DEMO0002", Status.IN_TRANSIT),
        ("DEMO0003", Status.OUT_FOR_DELIVERY),
        ("DEMO0004", Status.READY_FOR_PICKUP),
        ("DEMO0005", Status.DELIVERED),
        ("DEMO0006", Status.PROBLEM),
    ],
)
def test_demo_carrier_scenarios(number, status):
    now = datetime.now().astimezone()
    result = DEMO.simulate(TrackRequest(number, {}, {}, "en", now), now)
    assert result.status is status
    assert result.events[-1].description == result.description


def test_aggregate_prefers_what_needs_attention():
    assert aggregate([Status.DELIVERED, Status.IN_TRANSIT]) is Status.IN_TRANSIT
    assert aggregate([Status.IN_TRANSIT, Status.PROBLEM]) is Status.PROBLEM
    assert aggregate([Status.DELIVERED, Status.DELIVERED]) is Status.DELIVERED
    assert aggregate([]) is Status.UNKNOWN


@pytest.fixture
def anyio_backend():
    return "asyncio"
