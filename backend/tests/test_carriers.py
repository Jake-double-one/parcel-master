import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from parcelmaster.carriers.base import CarrierError, TrackRequest
from parcelmaster.carriers.de.dhl import MODULE as DHL
from parcelmaster.carriers.de.dhl import parse_parcel_de, parse_response, parse_web_response
from parcelmaster.carriers.de.gls import MODULE as GLS
from parcelmaster.carriers.de.gls import parse_response as gls_parse
from parcelmaster.carriers.de.gls import read_window as gls_window
from parcelmaster.carriers.de.gls import step_status as gls_step
from parcelmaster.carriers.de.hermes import MODULE as HERMES
from parcelmaster.carriers.de.hermes import parse_response as hermes_parse
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
    # The script brings these on: lookups of our own wait an hour after its last word.
    # Ten stops and more: the usual pace. Fewer: only the script is listened to.
    ((_, far),) = DHL.take_from_companion(payload)
    assert (far.stops_left, far.hold_minutes, far.again_in_minutes) == (10, None, None)
    payload["sendungen"][0]["sendungsdetails"]["liveTracking"]["countdownTextKey"] = "CD02"
    ((_, live),) = DHL.take_from_companion(payload)
    assert (live.stops_left, live.hold_minutes, live.again_in_minutes) == (9, 60, None)
    del payload["sendungen"][0]["sendungsdetails"]["liveTracking"]
    ((_, plain),) = DHL.take_from_companion(payload)
    assert plain.hold_minutes is None
    assert result.eta_from.isoformat() == "2026-11-18T12:30:00+01:00"


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


# -- Hermes ---------------------------------------------------------------


def hermes_answer(**changes):
    shipment = json.loads((FIXTURES / "hermes_transit.json").read_text())[0]
    return [{**shipment, **changes}]


def hermes_step(text, code="", mood="HAPPY"):
    step = {"status": mood, "parcelStatus": code, "historyText": text}
    return {**step, "timestamp": "2026-11-24T08:00:00Z"}


def test_hermes_recognises_its_numbers_and_links():
    assert HERMES.matches("H1000730000826301046")
    assert HERMES.matches("1234 5678 9012 34")
    assert not HERMES.matches("JJD000390016886395774")
    link = "https://www.myhermes.de/empfangen/sendungsverfolgung/sendungsinformation"
    assert HERMES.extract_from_url(f"{link}#H1000730000826301046") == "H1000730000826301046"
    assert HERMES.extract_from_url("https://example.org/#H1000730000826301046") is None
    assert HERMES.tracking_url("12345678901234", {}, "de") == f"{link}#12345678901234"


def test_hermes_history_is_sorted_and_carries_the_window():
    result = hermes_parse(hermes_answer())
    assert result.status is Status.IN_TRANSIT
    assert result.description == "Die Sendung wurde im Hermes Logistikzentrum sortiert."
    assert [event.status for event in result.events] == [
        Status.IN_TRANSIT,
        Status.IN_TRANSIT,
        Status.REGISTERED,
    ]
    assert result.eta_from.isoformat() == "2026-11-24T09:00:00+00:00"
    assert result.eta_to.isoformat() == "2026-11-24T13:00:00+00:00"


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        (
            {"parcelProgress": [hermes_step("Die Sendung befindet sich in der Zustellung.")]},
            Status.OUT_FOR_DELIVERY,
        ),
        ({"parcelProgress": [hermes_step("Die Sendung wurde zugestellt.")]}, Status.DELIVERED),
        ({"parcelAttributes": {"delivered": True}}, Status.DELIVERED),
        ({"viewParameters": {"readyForCollection": True}}, Status.READY_FOR_PICKUP),
        ({"viewParameters": {"collected": True}}, Status.DELIVERED),
        (
            {
                "parcelProgress": [
                    hermes_step("Die Sendung liegt im PaketShop zur Abholung bereit.")
                ]
            },
            Status.READY_FOR_PICKUP,
        ),
        (
            {
                "parcelProgress": [
                    hermes_step("Der Empfänger wurde nicht angetroffen.", mood="UNHAPPY")
                ]
            },
            Status.PROBLEM,
        ),
        ({"viewParameters": {"deliveryFailedAttempt": "2026-11-24T10:00:00Z"}}, Status.PROBLEM),
        (
            {"parcelAttributes": {"returnDeliveredTimestamp": "2026-11-24T10:00:00Z"}},
            Status.RETURNED,
        ),
        ({"parcelProgress": [hermes_step("", code="RETURN_DELIVERED_TO_SENDER")]}, Status.RETURNED),
        ({"parcelProgress": []}, Status.REGISTERED),
        ({"viewParameters": {"readyForParcelBoxCollection": True}}, Status.READY_FOR_PICKUP),
        ({"parcelAttributes": {"shipmentStopped": True}}, Status.PROBLEM),
        # Not delivered yet, whatever the word "zugestellt" suggests.
        (
            {
                "parcelProgress": [
                    hermes_step("Die Sendung wird voraussichtlich morgen zugestellt.")
                ]
            },
            Status.IN_TRANSIT,
        ),
        (
            {"parcelProgress": [hermes_step("Die Sendung konnte nicht zugestellt werden.")]},
            Status.PROBLEM,
        ),
        # Loaded onto the van, as Hermes really words it: "wurde" is the loading.
        (
            {
                "parcelAttributes": {"delivered": False},
                "parcelProgress": [
                    hermes_step(
                        "Die Sendung wurde ins Zustellfahrzeug geladen und wird "
                        "voraussichtlich heute zugestellt.",
                        code="DELIVERY_TOUR_STARTED",
                    )
                ],
            },
            Status.OUT_FOR_DELIVERY,
        ),
        (
            {
                "parcelProgress": [
                    hermes_step(
                        "Die Sendung wurde ins Zustellfahrzeug geladen und wird "
                        "voraussichtlich heute zugestellt."
                    )
                ]
            },
            Status.OUT_FOR_DELIVERY,
        ),
        ({"parcelProgress": [hermes_step("Die Sendung wurde zugestellt.")]}, Status.DELIVERED),
    ],
)
def test_hermes_status(changes, expected):
    result = hermes_parse(hermes_answer(**changes))
    assert result.status is expected
    if expected in (Status.DELIVERED, Status.RETURNED):
        assert result.eta_from is None


@pytest.mark.anyio
async def test_hermes_asks_the_public_search_in_the_users_language():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["language"] = str(req.url), req.headers["X-Language"]
        return httpx.Response(200, json=hermes_answer())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await HERMES.track(request("H1000730000826301046"), http)
    assert result.status is Status.IN_TRANSIT
    assert seen["url"].endswith("/tnt/v2/shipments/search/H1000730000826301046")
    assert seen["language"] == "de"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "code"),
    [(400, "not_found"), (404, "not_found"), (429, "rate_limited"), (503, "unavailable")],
)
async def test_hermes_errors_become_readable_codes(status, code):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(status))
    ) as http:
        with pytest.raises(CarrierError) as caught:
            await HERMES.track(request("H1000730000826301046"), http)
    assert caught.value.code == code


@pytest.mark.anyio
async def test_hermes_asks_for_the_live_round_only_with_a_postal_code_on_the_delivery_day():
    calls = []
    on_the_van = hermes_answer(
        parcelProgress=[hermes_step("Die Sendung befindet sich in der Zustellung.")]
    )

    def handler(answer, live):
        def handle(req: httpx.Request) -> httpx.Response:
            calls.append((req.url.path, req.headers.get("X-ZipCode")))
            if "/address/" in req.url.path:
                return live
            return httpx.Response(200, json=answer)

        return handle

    async def track(answer, live, postal_code="50667"):
        calls.clear()
        wanted = TrackRequest(
            "H1000730000826301046", {"postal_code": postal_code}, {}, "de", datetime.now()
        )
        transport = httpx.MockTransport(handler(answer, live))
        async with httpx.AsyncClient(transport=transport) as http:
            return await HERMES.track(wanted, http)

    stops = httpx.Response(200, json={"zipCode": "50667", "livetracking": {"stopsRemaining": 4}})
    result = await track(on_the_van, stops)
    assert (result.stops_left, result.stops_about, result.again_in_minutes) == (4, False, 1)
    assert calls[-1] == ("/tnt/v2/address/H1000730000826301046", "50667")

    many = httpx.Response(200, json={"address": {"livetracking": {"stopsRemaining": 31}}})
    result = await track(on_the_van, many)
    assert (result.stops_left, result.stops_about, result.again_in_minutes) == (30, True, 5)

    # A wrong postal code or no live view costs nothing but the stops.
    for live in (httpx.Response(403), httpx.Response(200, json={"zipCode": "50667"})):
        result = await track(on_the_van, live)
        assert result.status is Status.OUT_FOR_DELIVERY and result.stops_left is None

    # Not on the van, or no postal code: Hermes is not asked a second time.
    await track(hermes_answer(), stops)
    assert len(calls) == 1
    await track(on_the_van, stops, postal_code="")
    assert len(calls) == 1


def test_brand_colours_are_plain_hex():
    import re

    for module in discover().all():
        assert module.colour == "" or re.fullmatch(r"#[0-9a-f]{6}", module.colour), module.id


# -- GLS ------------------------------------------------------------------


def gls_answer(stage="INTRANSIT", **changes):
    payload = json.loads((FIXTURES / "gls_transit.json").read_text())
    parcel = payload["tuStatus"][0]
    parcel["progressBar"]["statusInfo"] = stage
    parcel["progressBar"].update(changes)
    return payload


def test_gls_recognises_its_numbers_and_links():
    assert GLS.matches("10000000001")
    assert GLS.matches("100000000012")
    assert not GLS.matches("1000000000")
    link = "https://www.gls-pakete.de/reach-sendungsverfolgung?match=10000000001"
    assert GLS.extract_from_url(link + "&trackingNumber=10000000001") == "10000000001"
    assert GLS.extract_from_url("https://example.com/?match=10000000001") is None
    assert GLS.tracking_url("10000000001", {}, "de").startswith(link)


def test_gls_reads_stage_sentence_and_window():
    result = gls_parse(gls_answer())
    assert result.status is Status.IN_TRANSIT
    assert result.description == "Das Paket ist unterwegs zum Ziel-Paketzentrum."
    assert result.eta_from == datetime(2026, 11, 25, 12, 45)
    assert result.eta_to == datetime(2026, 11, 25, 16, 0)
    assert result.collect and result.events == []


@pytest.mark.parametrize(
    ("stage", "changes", "status"),
    [
        ("PREADVICE", {}, Status.REGISTERED),
        ("INWAREHOUSE", {}, Status.IN_TRANSIT),
        ("INDELIVERY", {}, Status.OUT_FOR_DELIVERY),
        ("DELIVERED", {}, Status.DELIVERED),
        ("DELIVERED", {"retourFlag": True}, Status.RETURNED),
        ("DELIVEREDPS", {}, Status.READY_FOR_PICKUP),
        ("NOTDELIVERED", {}, Status.PROBLEM),
        ("SOMETHING_NEW", {}, Status.IN_TRANSIT),
    ],
)
def test_gls_stages(stage, changes, status):
    result = gls_parse(gls_answer(stage, **changes))
    assert result.status is status
    if status in (Status.DELIVERED, Status.RETURNED):
        assert result.eta_from is None  # the time named then is no forecast


def test_gls_window_in_both_languages():
    assert gls_window("25-Nov-2026, 12:45 - 16:00") == (
        datetime(2026, 11, 25, 12, 45),
        datetime(2026, 11, 25, 16, 0),
    )
    assert gls_window("25.11.2026") == (datetime(2026, 11, 25), datetime(2026, 11, 25, 23, 59))
    assert gls_window("bald") == (None, None)
    assert gls_window(None) == (None, None)


def test_gls_without_a_parcel_is_not_found():
    with pytest.raises(CarrierError) as caught:
        gls_parse({"tuStatus": []})
    assert caught.value.code == "not_found"


@pytest.mark.parametrize(
    ("text", "status"),
    [
        (
            "Die Paketdaten wurden im GLS IT-System erfasst; "
            "das Paket wurde noch nicht an GLS übergeben.",
            Status.REGISTERED,
        ),
        ("Das Paket wurde durch GLS übernommen.", Status.IN_TRANSIT),
        ("Das Paket ist im Paketzentrum eingetroffen.", Status.IN_TRANSIT),
        ("Das Paket wird voraussichtlich im Laufe des Tages zugestellt.", Status.OUT_FOR_DELIVERY),
        ("Das Paket wurde zugestellt.", Status.DELIVERED),
        ("Das Paket wurde im PaketShop zugestellt.", Status.READY_FOR_PICKUP),
        ("Das Paket konnte nicht zugestellt werden.", Status.PROBLEM),
        ("Das Paket wurde verladen und wird heute zugestellt.", Status.IN_TRANSIT),
    ],
)
def test_gls_sentences_of_the_page(text, status):
    assert gls_step(text) is status


def test_gls_page_history_keeps_times_and_drops_what_is_not_a_step():
    payload = {
        "parcels": [
            {
                "number": "100 0000 0001",
                "events": [
                    {
                        "time": "2026-11-23 17:31:20",
                        "text": "Das Paket wurde durch GLS übernommen.",
                    },
                    {"time": "2026-11-24 09:27:28", "text": "Im Paketzentrum.", "place": "Polen"},
                    {"time": "bald", "text": "x"},
                    "nonsense",
                ],
            },
            {"number": "nope", "events": []},
        ]
    }
    [(number, result)] = GLS.take_from_companion(payload)
    assert number == "10000000001"
    assert [event.description for event in result.events] == [
        "Im Paketzentrum.",
        "Das Paket wurde durch GLS übernommen.",
    ]
    assert result.events[0].timestamp == datetime(2026, 11, 24, 9, 27, 28)
    assert result.location == "Polen" and result.keep_window


def test_gls_live_view_brings_window_and_stops():
    live = {"from": "2026-11-25T12:51:00Z", "to": "2026-11-25T15:11:00Z"}
    step = {"time": "2026-11-25 06:56:00", "text": "Das Paket ist in Zustellung."}
    full = {"number": "10000000001", "events": [step], "window": live, "stops": 7}
    [(_, result)] = GLS.take_from_companion({"parcels": [full]})
    assert result.status is Status.OUT_FOR_DELIVERY and not result.live_only
    assert result.eta_from.isoformat() == "2026-11-25T12:51:00+00:00"
    assert result.stops_left == 7 and result.hold_minutes == 60 and not result.keep_window
    # With the history folded away only the live view is news.
    folded = {"number": "10000000001", "events": [], "window": live, "stops": None}
    [(_, result)] = GLS.take_from_companion({"parcels": [folded]})
    assert result.live_only and result.stops_left is None and result.eta_to is not None
    # Neither history nor live view: nothing to pass on.
    assert GLS.take_from_companion({"parcels": [{"number": "10000000001", "events": []}]}) == []
    odd = {"number": "10000000001", "events": [step], "window": {"from": "x"}, "stops": "50+"}
    [(_, result)] = GLS.take_from_companion({"parcels": [odd]})
    assert result.stops_left is None and result.keep_window


def test_gls_live_map_places():
    step = {"time": "2026-11-25 06:56:00", "text": "Das Paket ist in Zustellung."}
    parcel = {"number": "10000000001", "events": [step], "van": [52.51, 13.41], "home": [52.5, 13]}
    [(_, result)] = GLS.take_from_companion({"parcels": [parcel]})
    assert result.van == (52.51, 13.41) and result.home == (52.5, 13.0)
    for odd in ([52.51], [True, 1], ["52", "13"], [520, 13], "52,13", None):
        [(_, result)] = GLS.take_from_companion({"parcels": [{**parcel, "van": odd}]})
        assert result.van is None and result.home is None  # no van, no map
