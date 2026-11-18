import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from parcelmaster.carriers.base import CarrierError, TrackRequest
from parcelmaster.carriers.de.dhl import MODULE as DHL
from parcelmaster.carriers.de.dhl import parse_parcel_de, parse_response, parse_web_response
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

    result = await run_dhl(handler, api_key="secret", prefer="api")
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
        await run_dhl(lambda req: httpx.Response(status, json={}), api_key="secret", prefer="api")
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
async def test_dhl_without_key_asks_the_website_once():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path.rsplit("/", 1)[1])
        assert "DHL-API-Key" not in req.headers
        assert req.url.params["piececode"] == "00340434161094042557"
        return httpx.Response(200, json=web_answer())

    assert (await run_dhl(handler)).status is Status.IN_TRANSIT
    assert calls == ["search"]


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

    assert (await run_dhl(handler, api_key="refused", prefer="api")).status is Status.IN_TRANSIT


@pytest.mark.anyio
async def test_dhl_prefers_the_website_and_keeps_the_api_in_reserve():
    hosts = []

    def handler(req: httpx.Request) -> httpx.Response:
        hosts.append(req.url.host)
        if req.url.host == "www.dhl.de":
            return httpx.Response(404, text="<html>Not Found</html>")
        return httpx.Response(200, json=json.loads((FIXTURES / "dhl_transit.json").read_text()))

    assert (await run_dhl(handler, api_key="k")).status is Status.OUT_FOR_DELIVERY
    assert hosts[0] == "www.dhl.de"
    assert hosts[-1] == "api-eu.dhl.com"


def test_dhl_budget_depends_on_the_way_it_asks():
    assert DHL.budget({}) == 1000
    assert DHL.budget({"api_key": "k"}) == 1000
    assert DHL.budget({"api_key": "k", "prefer": "api"}) == 250
    assert DHL.budget({"api_key": "k", "api_secret": "s", "prefer": "api"}) == 1000
    assert not DHL.scarce({"api_key": "k", "api_secret": "s", "prefer": "api"})
    assert DHL.source({"api_key": "k"}) == "website_api"
    assert DHL.source({"prefer": "api"}) == "website"


def test_dhl_reads_the_stops_left_from_live_tracking():
    from parcelmaster.carriers.de.dhl import live_stops

    assert live_stops({"state": "OK", "countdownTextKey": "CD20"}) == (20, True)
    assert live_stops({"state": "ok", "countdownTextKey": "CD01"}) == (1, False)
    assert live_stops({"state": "ok", "countdownTextKey": "CD02", "countdown": 4}) == (4, False)
    assert live_stops({"state": "ok", "countdownTextKey": "CD02"}) == (9, True)
    assert live_stops({"state": "notavailable", "countdownTextKey": "CD20"}) == (None, False)
    assert live_stops({"state": "ok"}) == (None, False)

    payload = web_answer(fortschritt=4)
    payload["sendungen"][0]["sendungsdetails"]["liveTracking"] = {
        "state": "ok",
        "countdownTextKey": "CD10",
        "timeframe": {"from": "2026-11-18T12:30:00+01:00", "to": "2026-11-18T14:00:00+01:00"},
    }
    result = parse_web_response(payload)
    assert (result.stops_left, result.stops_about) == (10, True)
    assert result.eta_from.isoformat() == "2026-11-18T12:30:00+01:00"


@pytest.mark.anyio
async def test_dhl_follows_the_round_with_one_visit_for_all_parcels_of_a_van(monkeypatch):
    visits = []

    async def visit(tracking_number, postal_code):
        visits.append((tracking_number, postal_code))
        result = parse_web_response(web_answer(fortschritt=4))
        result.stops_left, result.stops_about = 20, True
        return result

    monkeypatch.setattr("parcelmaster.carriers.de.dhl._visit_page", visit)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=web_answer(fortschritt=4))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        first = await DHL.track(request(live="on"), http)
        second = await DHL.track(request(live="on", number="00340434161094042558"), http)
        quiet = await DHL.track(request(number="00340434161094042559"), http)  # off by default
    assert visits == [("00340434161094042557", "50667")]  # the second parcel rides along
    assert (first.stops_left, second.stops_left, quiet.stops_left) == (20, 20, None)
    assert first.again_in_minutes == 5


@pytest.mark.anyio
async def test_dhl_looks_every_minute_once_the_van_is_almost_there(monkeypatch):
    async def visit(tracking_number, postal_code):
        result = parse_web_response(web_answer(fortschritt=4))
        result.stops_left = 4
        return result

    monkeypatch.setattr("parcelmaster.carriers.de.dhl._visit_page", visit)
    result = await run_dhl(
        lambda req: httpx.Response(200, json=web_answer(fortschritt=4)), live="on"
    )
    assert result.again_in_minutes == 1


@pytest.mark.anyio
async def test_dhl_lets_the_browser_rest_after_a_failed_visit(monkeypatch):
    visits = []

    async def visit(tracking_number, postal_code):
        visits.append(tracking_number)
        return None

    monkeypatch.setattr("parcelmaster.carriers.de.dhl._visit_page", visit)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=web_answer(fortschritt=4))

    for _ in range(3):
        result = await run_dhl(handler, live="on")
        assert result.status is Status.OUT_FOR_DELIVERY  # the plain answer stands
    assert len(visits) == 1


def test_dhl_parcel_de_answer_is_mapped_to_the_common_model():
    result = parse_parcel_de((FIXTURES / "dhl_parcel_de.xml").read_text())
    assert result.status is Status.OUT_FOR_DELIVERY
    assert result.description.startswith("Die Sendung wurde in das Zustellfahrzeug geladen")
    assert (result.eta_from, result.eta_to) == (
        datetime(2026, 11, 18, 12, 30),
        datetime(2026, 11, 18, 14, 0),
    )
    assert [(e.timestamp, e.location) for e in result.events][-2:] == [
        (datetime(2026, 11, 17, 14, 55), "Leipzig"),
        (datetime(2026, 11, 17, 3, 36), None),
    ]
    assert result.events[-1].status is Status.REGISTERED
    with pytest.raises(CarrierError) as caught:
        parse_parcel_de('<data><data name="piece-status-public-list" code="100"/></data>')
    assert caught.value.code == "not_found"


@pytest.mark.anyio
async def test_dhl_with_key_and_secret_asks_parcel_de_tracking():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["host"], seen["auth"] = req.url.host, req.headers["Authorization"]
        seen["xml"] = req.url.params["xml"]
        return httpx.Response(200, text=(FIXTURES / "dhl_parcel_de.xml").read_text())

    result = await run_dhl(handler, api_key="key", api_secret="secret", prefer="api")
    assert result.status is Status.OUT_FOR_DELIVERY
    assert seen["host"] == "api-eu.dhl.com"
    assert seen["auth"] == "Basic a2V5OnNlY3JldA=="  # key:secret
    assert 'piece-code="00340434161094042557" zip-code="50667"' in seen["xml"]
    assert 'request="get-status-for-public-user"' in seen["xml"]


def test_dhl_follows_the_delivery_day_ever_more_closely():
    from parcelmaster.carriers.de.dhl import round_pace

    def at(hour, minute):
        return datetime(2026, 11, 18, hour, minute)

    wide = (at(12, 30), at(14, 0))
    assert round_pace(None, None, at(9, 0)) == 10  # no window yet
    assert round_pace(at(0, 0), datetime(2026, 11, 18, 23, 59), at(9, 0)) == 10  # only a day
    assert round_pace(*wide, at(11, 0)) == 10
    assert round_pace(*wide, at(12, 0)) == 5  # half an hour before it starts
    assert round_pace(*wide, at(14, 30)) == 5  # late, but still on its way
    assert round_pace(*wide, at(17, 0)) == 10
    narrow = (at(13, 10), at(13, 40))
    assert round_pace(*narrow, at(12, 45)) == 5
    assert round_pace(*narrow, at(12, 55)) == 1  # narrowed down and nearly due
