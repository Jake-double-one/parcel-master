import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from parcelmaster.carriers.base import CarrierError, TrackRequest
from parcelmaster.carriers.de.dhl import MODULE as DHL
from parcelmaster.carriers.de.dhl import parse_response, parse_web_response
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
    [(404, "not_found"), (429, "rate_limited"), (503, "unavailable")],
)
async def test_dhl_errors_become_readable_codes(status, code):
    with pytest.raises(CarrierError) as caught:
        await run_dhl(lambda req: httpx.Response(status, json={}), api_key="secret")
    assert caught.value.code == code


def test_dhl_website_answer_is_mapped_to_the_common_model():
    result = parse_web_response(json.loads((FIXTURES / "dhl_web_transit.json").read_text()))
    assert result.status is Status.IN_TRANSIT
    assert result.description.startswith("Die Sendung ist in der Region des Empfängers")
    assert result.eta_from == datetime(2026, 11, 18, 0, 0)
    assert result.eta_to == datetime(2026, 11, 18, 23, 59)
    assert [event.status for event in result.events] == [
        Status.IN_TRANSIT,
        Status.IN_TRANSIT,
        Status.IN_TRANSIT,
        Status.REGISTERED,
    ]
    assert result.events[0].timestamp.isoformat() == "2026-11-18T02:11:56+01:00"


def web_answer(**changes):
    payload = json.loads((FIXTURES / "dhl_web_transit.json").read_text())
    details = payload["sendungen"][0]["sendungsdetails"]
    for key, value in changes.items():
        if key in details["sendungsverlauf"]:
            details["sendungsverlauf"][key] = value
        else:
            details[key] = value
    return payload


def test_dhl_website_states():
    assert parse_web_response(web_answer(fortschritt=4)).status is Status.OUT_FOR_DELIVERY
    delivered = parse_web_response(web_answer(fortschritt=5, istZugestellt=True))
    assert delivered.status is Status.DELIVERED
    assert delivered.eta_from is None
    assert parse_web_response(web_answer(ruecksendung=True)).status is Status.RETURNED
    with pytest.raises(CarrierError) as caught:
        parse_web_response({"sendungen": [{"id": "1", "sendungNichtGefunden": {}}]})
    assert caught.value.code == "not_found"
    with pytest.raises(CarrierError) as caught:
        parse_web_response({"sendungen": [], "rateLimited": True})
    assert caught.value.code == "rate_limited"


@pytest.mark.anyio
async def test_dhl_without_key_reads_the_website_and_adds_the_postal_code():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        assert "DHL-API-Key" not in req.headers
        if req.url.path.endswith("/config"):
            return httpx.Response(200, json={"verfolgenCsrfToken": "tok"})
        if req.url.path.endswith("/shipment"):
            assert req.headers["verfolgen-csrf-token"] == "tok"
            assert json.loads(req.content)["zip"] == "50667"
            return httpx.Response(200, json=web_answer(fortschritt=4))
        assert req.url.params["piececode"] == "00340434161094042557"
        return httpx.Response(200, json=web_answer())

    result = await run_dhl(handler)
    assert result.status is Status.OUT_FOR_DELIVERY
    assert [path.rsplit("/", 1)[1] for _, path in calls] == ["search", "config", "shipment"]


@pytest.mark.anyio
async def test_dhl_website_keeps_the_plain_answer_if_the_postal_code_step_fails():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/search"):
            return httpx.Response(200, json=web_answer())
        return httpx.Response(403, text="no")

    assert (await run_dhl(handler)).status is Status.IN_TRANSIT


@pytest.mark.anyio
async def test_dhl_website_turning_us_away_is_reported():
    with pytest.raises(CarrierError) as caught:
        await run_dhl(lambda req: httpx.Response(404, text="<html>Not Found</html>"))
    assert caught.value.code == "unavailable"
    assert "404" in caught.value.detail


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


@pytest.mark.anyio
async def test_dhl_falls_back_to_the_website_if_the_key_is_refused():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "api-eu.dhl.com":
            return httpx.Response(401, json={})
        if req.url.path.endswith("/search"):
            return httpx.Response(200, json=web_answer())
        return httpx.Response(403, text="no")

    assert (await run_dhl(handler, api_key="refused")).status is Status.IN_TRANSIT
