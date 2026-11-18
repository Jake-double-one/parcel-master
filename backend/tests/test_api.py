import json
from pathlib import Path

from fastapi.testclient import TestClient

from parcelmaster.app import SECRET_MASK, create_app

from .conftest import make_config, new_shipment


def test_create_shows_status_last_check_and_carrier_link(client):
    created = new_shipment(client, "demo-0003", title="Headphones")
    assert created.status_code == 201
    # The first lookup runs in the background; asking for a refresh waits for it.
    shipment = client.post(f"/api/shipments/{created.json()['id']}/refresh").json()
    parcel = shipment["parcels"][0]
    assert parcel["tracking_number"] == "DEMO0003"  # normalized
    assert shipment["status"] == "out_for_delivery"
    assert shipment["last_checked_at"] is not None
    assert shipment["eta_from"] is not None
    assert parcel["tracking_url"].endswith("/DEMO0003")
    assert parcel["events"][0]["status"] == "out_for_delivery"  # newest first


def test_several_parcels_form_one_shipment(client):
    shipment_id = new_shipment(client, "DEMO0005", "DEMO0002", title="Order").json()["id"]
    shipment = client.post(f"/api/shipments/{shipment_id}/refresh").json()
    assert shipment["parcel_count"] == 2
    assert shipment["finished_count"] == 1
    assert shipment["status"] == "in_transit"
    assert [p["status"] for p in shipment["parcels"]] == ["delivered", "in_transit"]


def test_duplicates_are_rejected_with_the_existing_title(client):
    new_shipment(client, "DEMO0002", title="First")
    again = new_shipment(client, "demo 0002", title="Second")
    assert again.status_code == 409
    assert again.json()["detail"]["error"] == "duplicate"
    assert again.json()["detail"]["title"] == "First"
    twice = new_shipment(client, "DEMO0009", "DEMO0009")
    assert twice.status_code == 409


def test_edit_uses_same_payload_and_keeps_history_of_unchanged_parcels(client):
    shipment = new_shipment(client, "DEMO0002", title="Old name").json()
    shipment = client.post(f"/api/shipments/{shipment['id']}/refresh").json()
    kept = shipment["parcels"][0]
    updated = client.put(
        f"/api/shipments/{shipment['id']}",
        json={
            "title": "New name",
            "direction": "return",
            "note": "note",
            "parcels": [
                {"id": kept["id"], "carrier": "demo", "tracking_number": "DEMO0002"},
                {"carrier": "demo", "tracking_number": "DEMO0001"},
            ],
        },
    ).json()
    assert updated["title"] == "New name"
    assert updated["direction"] == "return"
    assert updated["parcels"][0]["id"] == kept["id"]
    assert updated["parcels"][0]["last_checked_at"] == kept["last_checked_at"]
    assert len(updated["parcels"][0]["events"]) == len(kept["events"])
    assert len(updated["parcels"]) == 2

    removed = client.put(
        f"/api/shipments/{shipment['id']}",
        json={
            "title": "New name",
            "parcels": [
                {
                    "carrier": "demo",
                    "tracking_number": "DEMO0001",
                    "id": updated["parcels"][1]["id"],
                }
            ],
        },
    ).json()
    assert [p["tracking_number"] for p in removed["parcels"]] == ["DEMO0001"]


def test_validation(client):
    assert client.post("/api/shipments", json={"title": "x", "parcels": []}).status_code == 422
    unknown = client.post(
        "/api/shipments", json={"parcels": [{"carrier": "nope", "tracking_number": "12345"}]}
    )
    assert unknown.json()["detail"]["error"] == "unknown_carrier"
    assert (
        client.put(
            "/api/shipments/999",
            json={"parcels": [{"carrier": "demo", "tracking_number": "DEMO0001"}]},
        ).status_code
        == 404
    )


def test_archive_and_delete(client):
    shipment_id = new_shipment(client, "DEMO0005").json()["id"]
    client.post(f"/api/shipments/{shipment_id}/archive", json={"archived": True})
    assert client.get("/api/shipments").json() == []
    assert len(client.get("/api/shipments?archived=true").json()) == 1
    # An archived number may be tracked again.
    assert new_shipment(client, "DEMO0005").status_code == 201
    client.post(f"/api/shipments/{shipment_id}/archive", json={"archived": False})
    assert client.delete(f"/api/shipments/{shipment_id}").status_code == 204
    assert client.delete(f"/api/shipments/{shipment_id}").status_code == 404


def test_detect_finds_carrier_in_numbers_and_links(client):
    def detect(text):
        return client.post("/api/detect", json={"text": text}).json()["matches"]

    assert detect("demo 12345") == [{"carrier": "demo", "tracking_number": "DEMO12345"}]
    assert detect("Your parcel 00340434161094042557 is on its way") == [
        {"carrier": "de.dhl", "tracking_number": "00340434161094042557"}
    ]
    link = (
        "https://www.dhl.de/de/privatkunden/pakete-empfangen/verfolgen.html?piececode=JJD1234567890"
    )
    assert detect(f"Track here: {link}") == [
        {"carrier": "de.dhl", "tracking_number": "JJD1234567890"}
    ]
    assert detect("hello there") == []


def test_secrets_are_masked_and_survive_an_unrelated_save(client):
    saved = client.put(
        "/api/settings",
        json={"carriers": {"de.dhl": {"api_key": "top-secret", "daily_budget": 100}}},
    ).json()
    assert saved["carriers"]["de.dhl"] == {
        "api_key": SECRET_MASK,
        "api_secret": "",
        "daily_budget": 100,
    }
    # The browser sends the mask back together with another change.
    client.put(
        "/api/settings",
        json={"language": "de", "carriers": {"de.dhl": {"api_key": SECRET_MASK}}},
    )
    carrier = next(c for c in client.get("/api/carriers").json() if c["id"] == "de.dhl")
    assert carrier["configured"] is True
    assert carrier["daily_budget"] == 100
    assert "top-secret" not in client.get("/api/settings").text
    cleared = client.put("/api/settings", json={"carriers": {"de.dhl": {"api_key": ""}}}).json()
    assert cleared["carriers"]["de.dhl"]["api_key"] == ""


def test_settings_are_validated_and_bounded(client):
    saved = client.put(
        "/api/settings",
        json={"polling": {"interval_minutes": 1, "stale_days": 99, "auto_archive_days": "x"}},
    ).json()
    assert saved["polling"] == {
        "interval_minutes": 15,
        "stale_days": 30,
        "auto_archive_days": 3,
        "saver": {"enabled": True, "from": "22:00", "to": "06:00"},  # on unless switched off
    }
    off = {"polling": {"saver": {"enabled": False, "from": "23:00", "to": "05:30"}}}
    assert (
        client.put("/api/settings", json=off).json()["polling"]["saver"] == off["polling"]["saver"]
    )
    bad = {"polling": {"saver": {"enabled": True, "from": "late", "to": "05:30"}}}
    assert client.put("/api/settings", json=bad).json()["detail"]["error"] == "invalid_saver_hours"
    assert client.put("/api/settings", json={"timezone": "Mars/Olympus"}).status_code == 422
    bad_quiet = {
        "notifications": {"quiet_hours": {"enabled": True, "from": "25:00", "to": "07:00"}}
    }
    assert client.put("/api/settings", json=bad_quiet).status_code == 422
    assert client.put("/api/settings", json={"language": "xx"}).json()["language"] == "en"


def test_languages_fall_back_to_english(client, tmp_path):
    meta = client.get("/api/meta").json()
    assert {"code": "de", "name": "Deutsch"} in meta["languages"]
    german = client.get("/api/i18n/de").json()
    assert german["status.delivered"] == "Zugestellt"
    assert german["carrier.de.dhl.settings.api_key.label"] == "API-Schlüssel"
    # An unknown language is simply English.
    assert client.get("/api/i18n/fr").json()["status.delivered"] == "Delivered"


def test_extra_language_file_needs_no_rebuild_and_gaps_use_english(tmp_path):
    (tmp_path / "locales").mkdir()
    (tmp_path / "locales" / "fr.json").write_text(
        '{"meta": {"name": "Français"}, "status": {"delivered": "Livré"}}', "utf-8"
    )
    with TestClient(create_app(make_config(tmp_path))) as client:
        assert {"code": "fr", "name": "Français"} in client.get("/api/meta").json()["languages"]
        french = client.get("/api/i18n/fr").json()
        assert french["status.delivered"] == "Livré"
        assert french["status.in_transit"] == "In transit"


def test_export_and_import_round_trip(client, tmp_path):
    new_shipment(client, "DEMO0002", "DEMO0001", title="Order", direction="outgoing")
    exported = client.get("/api/export").json()
    with TestClient(create_app(make_config(tmp_path / "other"))) as other:
        assert other.post("/api/import", json=exported).json() == {"imported": 1, "skipped": 0}
        # Importing the same file again adds nothing.
        assert other.post("/api/import", json=exported).json() == {"imported": 0, "skipped": 1}
        shipment = other.get("/api/shipments").json()[0]
        assert shipment["title"] == "Order"
        assert shipment["direction"] == "outgoing"
        assert len(shipment["parcels"]) == 2
        assert other.post("/api/import", json={"format": "other"}).status_code == 422


def test_password_wall_and_login(tmp_path):
    with TestClient(create_app(make_config(tmp_path, password="hunter2"))) as client:
        assert client.get("/api/meta").json()["login_required"] is True
        assert client.get("/api/shipments").status_code == 401
        assert client.get("/api/settings").status_code == 401
        wrong = client.post("/api/auth/login", json={"password": "nope"})
        assert wrong.json()["detail"]["error"] == "wrong_password"
        assert client.post("/api/auth/login", json={"password": "hunter2"}).status_code == 200
        assert client.get("/api/shipments").status_code == 200
        assert client.get("/api/meta").json()["login_required"] is False
        client.post("/api/auth/logout")
        assert client.get("/api/shipments").status_code == 401


def test_login_is_throttled(tmp_path):
    with TestClient(create_app(make_config(tmp_path, password="hunter2"))) as client:
        for _ in range(5):
            client.post("/api/auth/login", json={"password": "guess"})
        blocked = client.post("/api/auth/login", json={"password": "hunter2"})
        assert blocked.status_code == 429


def test_demo_mode_is_isolated_from_real_data(tmp_path):
    config = make_config(tmp_path, password="hunter2", demo=True, demo_carrier=False)
    app = create_app(config)
    with TestClient(app) as owner, TestClient(app) as visitor:
        owner.post("/api/auth/login", json={"password": "hunter2"})
        owner.put("/api/settings", json={"carriers": {"de.dhl": {"api_key": "top-secret"}}})
        owner.post(
            "/api/shipments",
            json={
                "title": "Private",
                "parcels": [{"carrier": "de.dhl", "tracking_number": "003404341610"}],
            },
        )

        assert visitor.get("/api/meta").json()["demo_available"] is True
        assert visitor.post("/api/auth/demo", json={"language": "de"}).status_code == 200
        meta = visitor.get("/api/meta").json()
        assert meta["demo"] is True and meta["login_required"] is False

        demo_shipments = visitor.get("/api/shipments").json()
        titles = [s["title"] for s in demo_shipments]
        assert "Private" not in titles and "Kopfhörer" in titles
        assert all(s["last_checked_at"] for s in demo_shipments)  # looked up on first view
        assert [c["id"] for c in visitor.get("/api/carriers").json()] == ["demo"]
        assert "top-secret" not in visitor.get("/api/settings").text
        assert "de.dhl" not in visitor.get("/api/settings").json()["carriers"]

        # A visitor can play, but only inside their own copy.
        visitor.post(
            "/api/shipments",
            json={"title": "Mine", "parcels": [{"carrier": "demo", "tracking_number": "DEMO5551"}]},
        )
        visitor.put("/api/settings", json={"polling": {"interval_minutes": 15}})
        assert visitor.post("/api/notifications/test").status_code == 403
        assert visitor.get("/api/push/key").status_code == 403
        rejected = visitor.post(
            "/api/shipments",
            json={"parcels": [{"carrier": "de.dhl", "tracking_number": "003404341611"}]},
        )
        assert rejected.json()["detail"]["error"] == "unknown_carrier"

        assert [s["title"] for s in owner.get("/api/shipments").json()] == ["Private"]
        assert owner.get("/api/settings").json()["polling"]["interval_minutes"] == 120
        # The real owner never sees the demo carrier.
        assert "demo" not in [c["id"] for c in owner.get("/api/carriers").json()]


def test_demo_is_off_unless_enabled(client):
    assert client.post("/api/auth/demo", json={}).status_code == 404


def test_importer_modules_are_listed_but_not_selectable_by_hand(client):
    amazon = next(c for c in client.get("/api/carriers").json() if c["id"] == "de.amazon")
    assert amazon["kind"] == "importer"
    assert amazon["configured"] is False
    assert [f["key"] for f in amazon["settings_fields"]] == ["email", "password", "otp_secret"]
    by_hand = client.post(
        "/api/shipments",
        json={"parcels": [{"carrier": "de.amazon", "tracking_number": "302-1234567"}]},
    )
    assert by_hand.json()["detail"]["error"] == "unknown_carrier"
    # Without credentials a sync does nothing but say so.
    state = client.post("/api/importers/de.amazon/sync").json()
    assert state["last_error"]["code"] == "not_configured"
    assert client.post("/api/importers/de.dhl/sync").status_code == 404


def test_importer_accounts_are_connected_not_saved(client):
    # Saving the settings form never stores an importer's credentials.
    client.put(
        "/api/settings",
        json={"carriers": {"de.amazon": {"email": "a@example.org", "password": "x"}}},
    )
    saved = client.get("/api/settings").json()["carriers"]["de.amazon"]
    assert saved == {"sync_minutes": None}  # the account never comes back to the browser
    amazon = next(c for c in client.get("/api/carriers").json() if c["id"] == "de.amazon")
    assert amazon["sync"]["account"] is None

    missing = client.post("/api/importers/de.amazon/connect", json={"email": "a@example.org"})
    assert missing.json()["detail"]["error"] == "missing_credentials"
    assert client.post("/api/importers/de.dhl/connect", json={}).status_code == 404
    assert client.post("/api/importers/de.amazon/disconnect").json()["account"] is None


def test_connecting_reports_why_the_sign_in_failed(client, monkeypatch):
    from parcelmaster.carriers.base import CarrierError
    from parcelmaster.carriers.de.amazon import MODULE

    def refuse(request):
        raise CarrierError("sign_in_failed", "Amazon said no")

    monkeypatch.setattr(MODULE, "sync", refuse)
    credentials = {"email": "a@example.org", "password": "x", "unknown": "dropped"}
    failed = client.post("/api/importers/de.amazon/connect", json=credentials)
    assert failed.status_code == 400
    assert failed.json()["detail"] == {
        "error": "sign_in",
        "reason": "sign_in_failed",
        "message": "Amazon said no",
    }
    # Trying again at once is refused, so a typo cannot hammer the service.
    assert client.post("/api/importers/de.amazon/connect", json=credentials).status_code == 429

    monkeypatch.setattr(MODULE, "sync", lambda request: [])
    client.app.state.pm.store.set_value("importers", {})
    connected = client.post("/api/importers/de.amazon/connect", json=credentials)
    assert connected.json()["account"] == "a@example.org"
    stored = client.app.state.pm.store.get_settings()["carriers"]["de.amazon"]
    assert stored == {"email": "a@example.org", "password": "x"}
    assert "x" not in client.get("/api/settings").json()["carriers"]["de.amazon"].values()


def test_editing_an_imported_shipment_keeps_what_the_importer_noted(client):
    store = client.app.state.pm.store
    shipment_id = store.save_shipment(
        None,
        {
            "title": "Kettle",
            "direction": "incoming",
            "note": "",
            "source": "Amazon",
            "reference": "302-1",
        },
        [],
    )
    store.add_parcel(
        shipment_id,
        "de.amazon",
        "SHIP1",
        {"import_key": "SHIP1", "url": "https://x", "items": '["Kettle", "2 × Filter"]'},
    )
    store.add_parcel(shipment_id, "demo", "DEMO0002", {"import_key": "SHIP2"})
    shipment = next(s for s in client.get("/api/shipments").json() if s["id"] == shipment_id)
    assert shipment["parcels"][0]["tracking_url"] == "https://x"
    assert shipment["parcels"][0]["site_name"] == "Amazon"
    assert shipment["parcels"][0]["contents"] == ["Kettle", "2 × Filter"]
    assert shipment["parcels"][1]["contents"] == []

    edited = client.put(
        f"/api/shipments/{shipment_id}",
        json={
            "title": "My kettle",
            "parcels": [
                {"id": p["id"], "carrier": p["carrier"], "tracking_number": p["tracking_number"]}
                for p in shipment["parcels"]
            ],
        },
    ).json()
    assert edited["title"] == "My kettle"
    assert edited["source"] == "Amazon" and edited["reference"] == "302-1"
    assert [p["extra"].get("import_key") for p in edited["parcels"]] == ["SHIP1", "SHIP2"]


def test_refreshing_a_shipment_syncs_the_service_that_still_tracks_it(client, monkeypatch):
    from parcelmaster.carriers.base import ImportedParcel, ImportedShipment
    from parcelmaster.carriers.de.amazon import MODULE
    from parcelmaster.status import Status

    answers = [
        [
            ImportedShipment(
                "302-1", "Kettle", [ImportedParcel("S1", Status.IN_TRANSIT, "Dispatched")]
            )
        ],
        [
            ImportedShipment(
                "302-1",
                "Kettle",
                [ImportedParcel("S1", Status.OUT_FOR_DELIVERY, "Out for delivery")],
            )
        ],
    ]
    calls = []

    def sync(request):
        calls.append(1)
        return answers[min(len(calls), len(answers)) - 1]

    monkeypatch.setattr(MODULE, "sync", sync)
    client.post(
        "/api/importers/de.amazon/connect", json={"email": "a@example.org", "password": "x"}
    )
    shipment = client.get("/api/shipments").json()[0]
    assert shipment["status"] == "in_transit"

    # Right after a sync the service is not asked again.
    same = client.post(f"/api/shipments/{shipment['id']}/refresh").json()
    assert same["status"] == "in_transit" and len(calls) == 1

    states = client.app.state.pm.store.get_value("importers")
    states["de.amazon"]["last_attempt_at"] = "2020-01-01T00:00:00+00:00"
    client.app.state.pm.store.set_value("importers", states)
    fresh = client.post(f"/api/shipments/{shipment['id']}/refresh").json()
    assert len(calls) == 2
    assert fresh["status"] == "out_for_delivery"
    assert fresh["parcels"][0]["description"] == "Out for delivery"


def test_dhl_offers_the_users_postal_code_as_default_for_new_parcels(client):
    def dhl():
        return next(c for c in client.get("/api/carriers").json() if c["id"] == "de.dhl")

    assert dhl()["parcel_defaults"] == {}
    assert [f["key"] for f in dhl()["settings_fields"]] == [
        "api_key",
        "api_secret",
        "prefer",
        "default_postal_code",
    ]
    client.put("/api/settings", json={"carriers": {"de.dhl": {"default_postal_code": " 50667 "}}})
    assert dhl()["parcel_defaults"] == {"postal_code": "50667"}
    assert dhl()["configured"] is True  # works without a key, by reading dhl.de
    assert (
        client.get("/api/settings").json()["carriers"]["de.dhl"]["default_postal_code"] == "50667"
    )


def test_a_deleted_import_does_not_come_back_with_the_next_sync(client, monkeypatch):
    from parcelmaster.carriers.base import ImportedParcel, ImportedShipment
    from parcelmaster.carriers.de.amazon import MODULE
    from parcelmaster.status import Status

    found = [
        ImportedShipment(
            "302-1", "Kettle", [ImportedParcel("S1", Status.IN_TRANSIT, "Dispatched")]
        ),
        ImportedShipment("302-2", "Cable", [ImportedParcel("S2", Status.IN_TRANSIT, "Dispatched")]),
    ]
    monkeypatch.setattr(MODULE, "sync", lambda request: found)
    client.post(
        "/api/importers/de.amazon/connect", json={"email": "a@example.org", "password": "x"}
    )
    shipments = {s["reference"]: s["id"] for s in client.get("/api/shipments").json()}
    assert set(shipments) == {"302-1", "302-2"}

    client.delete(f"/api/shipments/{shipments['302-1']}")
    states = client.app.state.pm.store.get_value("importers")
    states["de.amazon"]["last_attempt_at"] = "2020-01-01T00:00:00+00:00"
    client.app.state.pm.store.set_value("importers", states)
    client.post("/api/importers/de.amazon/sync")
    assert [s["reference"] for s in client.get("/api/shipments").json()] == ["302-2"]


def test_an_amazon_order_can_be_added_by_its_number_whatever_its_state(client, monkeypatch):
    from parcelmaster.carriers.base import CarrierError, ImportedParcel, ImportedShipment
    from parcelmaster.carriers.de.amazon import MODULE
    from parcelmaster.status import Status

    number = "028-0001289-9273121"
    amazon = next(c for c in client.get("/api/carriers").json() if c["id"] == "de.amazon")
    assert amazon["by_reference"] is True

    def detect(text):
        return client.post("/api/detect", json={"text": text}).json()["matches"]

    wanted = [{"carrier": "de.amazon", "tracking_number": number}]
    assert detect(number) == wanted
    assert detect(f"Your order {number} has shipped") == wanted
    assert detect(f"https://www.amazon.de/gp/your-account/order-details?orderID={number}") == wanted

    def import_order(reference=number):
        return client.post("/api/importers/de.amazon/import", json={"reference": reference})

    assert import_order("12345").json()["detail"]["error"] == "invalid_reference"
    not_connected = import_order().json()["detail"]
    assert (not_connected["error"], not_connected["reason"]) == ("import_failed", "not_connected")

    monkeypatch.setattr(MODULE, "sync", lambda request: [])
    client.post(
        "/api/importers/de.amazon/connect", json={"email": "a@example.org", "password": "x"}
    )

    def fetch(request, reference):
        if reference != number:
            raise CarrierError("not_found")
        # Delivered weeks ago: the regular sync would never list it.
        return ImportedShipment(
            number,
            "Kettle",
            [ImportedParcel(f"{number}/1", Status.DELIVERED, "", items=["Kettle"])],
        )

    monkeypatch.setattr(MODULE, "fetch", fetch)
    assert import_order("028-0000000-0000000").json()["detail"]["reason"] == "not_found"

    created = import_order()
    assert created.status_code == 201
    shipment = created.json()
    assert (shipment["reference"], shipment["status"], shipment["source"]) == (
        number,
        "delivered",
        "Amazon",
    )
    assert shipment["parcels"][0]["contents"] == ["Kettle"]

    # Asking again returns the same shipment, also after it was archived or deleted.
    client.post(f"/api/shipments/{shipment['id']}/archive", json={"archived": True})
    again = import_order().json()
    assert again["id"] == shipment["id"] and again["archived_at"] is None
    client.delete(f"/api/shipments/{shipment['id']}")
    assert import_order().status_code == 201
    assert len(client.get("/api/shipments").json()) == 1


def test_how_often_an_importer_syncs_is_a_setting_and_is_reported(client, monkeypatch):
    from parcelmaster.carriers.de.amazon import MODULE

    def amazon():
        return next(c for c in client.get("/api/carriers").json() if c["id"] == "de.amazon")

    assert amazon()["sync"]["interval_minutes"] == 180
    assert amazon()["sync"]["next_sync_at"] is None  # not connected: nothing scheduled

    monkeypatch.setattr(MODULE, "sync", lambda request: [])
    credentials = {"email": "a@example.org", "password": "x"}
    client.post("/api/importers/de.amazon/connect", json=credentials)
    first = amazon()["sync"]
    assert first["next_sync_at"] > first["last_sync_at"]

    saved = client.put("/api/settings", json={"carriers": {"de.amazon": {"sync_minutes": 60}}})
    # Saving the pace neither exposes nor wipes the account.
    assert saved.json()["carriers"]["de.amazon"] == {"sync_minutes": 60}
    assert client.app.state.pm.store.get_settings()["carriers"]["de.amazon"] == {
        **credentials,
        "sync_minutes": 60,
    }
    faster = amazon()["sync"]
    assert faster["interval_minutes"] == 60 and faster["next_sync_at"] < first["next_sync_at"]
    assert amazon()["sync"]["account"] == "a@example.org"

    # Too eager is slowed down; empty means the module's own pace again.
    client.put("/api/settings", json={"carriers": {"de.amazon": {"sync_minutes": 1}}})
    assert amazon()["sync"]["interval_minutes"] == 30
    client.put("/api/settings", json={"carriers": {"de.amazon": {"sync_minutes": None}}})
    assert amazon()["sync"]["interval_minutes"] == 180

    module = next(m for m in client.get("/api/status").json()["modules"] if m["id"] == "de.amazon")
    assert module["kind"] == "importer" and module["sync"]["interval_minutes"] == 180


def test_automatic_archiving_is_reported_once(client):
    from datetime import UTC, datetime, timedelta

    from parcelmaster.tracking import auto_archive

    store = client.app.state.pm.store
    assert client.post("/api/notices/take").json() == {"auto_archived": 0}
    long_ago = (datetime.now(UTC) - timedelta(days=5)).isoformat(timespec="seconds")
    for number in ("DEMO0005", "DEMO0015"):
        shipment = new_shipment(client, number).json()
        store.update_parcel(shipment["parcels"][0]["id"], status="delivered", delivered_at=long_ago)
    new_shipment(client, "DEMO0002")  # still on its way

    assert auto_archive(store) == 2
    assert client.post("/api/notices/take").json() == {"auto_archived": 2}
    assert client.post("/api/notices/take").json() == {"auto_archived": 0}
    assert len(client.get("/api/shipments").json()) == 1


def test_device_test_only_reaches_that_browser(client, monkeypatch):
    import pywebpush

    reached = []

    def webpush(**kwargs):
        reached.append(kwargs["subscription_info"]["endpoint"])

    monkeypatch.setattr(pywebpush, "webpush", webpush)
    for name in ("one", "two"):
        subscription = {
            "endpoint": f"https://push.example/{name}",
            "keys": {"p256dh": "k", "auth": "a"},
        }
        assert client.post("/api/push/subscriptions", json=subscription).status_code == 204

    report = client.post(
        "/api/notifications/test", json={"endpoint": "https://push.example/two"}
    ).json()
    assert reached == ["https://push.example/two"]
    assert report["push"] == {"ok": True, "sent": 1, "subscriptions": 1, "error": ""}

    reached.clear()
    report = client.post("/api/notifications/test").json()
    assert sorted(reached) == ["https://push.example/one", "https://push.example/two"]
    assert report["push"]["sent"] == 2

    unknown = client.post(
        "/api/notifications/test", json={"endpoint": "https://push.example/none"}
    ).json()
    assert unknown["push"] is None


def test_log_shows_what_the_server_did(client):
    import logging

    logging.getLogger("parcelmaster.tracking").warning("de.dhl 123: unavailable (dhl.de: HTTP 404)")
    report = client.get("/api/log").json()
    assert report["level"] == "info"
    newest = report["entries"][0]
    assert newest["level"] == "warning"
    assert newest["source"] == "tracking"
    assert "HTTP 404" in newest["message"]


def test_carrier_health_says_how_the_last_lookup_went_and_which_way_it_asks(client):
    def dhl():
        return next(c for c in client.get("/api/carriers").json() if c["id"] == "de.dhl")["health"]

    assert dhl() == {"state": "untested", "source": "website"}
    client.put("/api/settings", json={"carriers": {"de.dhl": {"api_key": "k"}}})
    assert dhl() == {"state": "untested", "source": "website_api"}
    status = client.get("/api/status").json()
    assert (
        next(m for m in status["modules"] if m["id"] == "de.dhl")["health"]["source"]
        == "website_api"
    )


def test_companion_script_is_made_out_to_this_server_and_its_news_are_applied(client):
    dhl = next(c for c in client.get("/api/carriers").json() if c["id"] == "de.dhl")
    assert dhl["companion"] == "/api/companion/de.dhl.user.js"
    script = client.get(dhl["companion"]).text
    assert 'const SERVER = "http://testserver";' in script
    assert "// @connect      testserver" in script
    assert client.get("/api/companion/demo.user.js").status_code == 404

    shipment = client.post(
        "/api/shipments",
        json={"parcels": [{"carrier": "de.dhl", "tracking_number": "00340434161094042557"}]},
    ).json()
    fixture = Path(__file__).parent / "fixtures" / "dhl_web_transit.json"
    payload = json.loads(fixture.read_text())
    details = payload["sendungen"][0]["sendungsdetails"]
    details["sendungsverlauf"]["fortschritt"] = 4
    details["liveTracking"] = {
        "state": "ok",
        "countdownTextKey": "CD10",
        "timeframe": {"from": "2026-11-18T12:40:00+01:00", "to": "2026-11-18T14:10:00+01:00"},
    }
    taken = client.post("/api/companion/de.dhl", json={"payload": payload}).json()
    assert taken == {"parcels": 1}
    parcel = client.get("/api/shipments").json()[0]["parcels"][0]
    assert shipment["id"] and parcel["status"] == "out_for_delivery"
    assert parcel["stops"] == {"left": 10, "about": True}
    assert parcel["eta_from"] == "2026-11-18T11:40:00+00:00"  # 12:40 in Germany
    # Rubbish and parcels we do not know change nothing.
    assert client.post("/api/companion/de.dhl", json={"payload": "x"}).json() == {"parcels": 0}


def test_a_failed_apprise_test_says_why(client):
    client.put(
        "/api/settings",
        json={"notifications": {"apprise_urls": ["gotify://127.0.0.1:9/token", "nonsense"]}},
    )
    report = client.post("/api/notifications/test").json()["apprise"]
    assert report["ok"] is False
    assert "1 address(es) not understood" in report["error"]
    assert "Gotify" in report["error"]  # Apprise's own words about the refused connection
    log = client.get("/api/log").json()["entries"]
    assert any(e["source"] == "notify" and "Gotify" in e["message"] for e in log)
