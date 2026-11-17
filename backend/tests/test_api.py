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
    assert saved["carriers"]["de.dhl"] == {"api_key": SECRET_MASK, "daily_budget": 100}
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
    assert saved["polling"] == {"interval_minutes": 15, "stale_days": 30, "auto_archive_days": 3}
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
