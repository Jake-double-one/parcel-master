from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from parcelmaster.app import create_app
from parcelmaster.config import Config


def make_config(tmp_path: Path, **overrides) -> Config:
    values = {
        "data_dir": tmp_path,
        "password": None,
        "demo": False,
        "demo_carrier": True,
        "scheduler": False,
        "static_dir": None,
        "push_contact": "mailto:test@example.org",
    }
    return Config(**{**values, **overrides})


@pytest.fixture
def client(tmp_path):
    """An open instance (no password) with the demo carrier usable for real data."""
    with TestClient(create_app(make_config(tmp_path))) as test_client:
        yield test_client


def new_shipment(client, *numbers, **fields):
    payload = {
        "title": fields.pop("title", "Test"),
        "parcels": [{"carrier": "demo", "tracking_number": n} for n in numbers],
        **fields,
    }
    return client.post("/api/shipments", json=payload)
