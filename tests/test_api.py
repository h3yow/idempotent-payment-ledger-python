"""HTTP API: existing endpoints stay usable; payment flow works end to end."""

import pytest
from fastapi.testclient import TestClient

from main import app
from tests.conftest import explain


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_existing_root_endpoint_still_works(client):
    assert client.get("/").json() == {"Hello": "World"}


def test_full_payment_flow_over_http(client):
    key = "api-flow-key"
    created = client.post(
        "/payments", json={"idempotency_key": key, "amount": 100, "currency": "CNY"}
    )
    assert created.status_code == 201
    pid = created.json()["payment_id"]

    replay = client.post(
        "/payments", json={"idempotency_key": key, "amount": 100, "currency": "CNY"}
    )
    explain(key, replay.json()["status"], "HTTP replay returns same payment_id with replayed=True")
    assert replay.json()["payment_id"] == pid and replay.json()["replayed"] is True

    conflict = client.post(
        "/payments", json={"idempotency_key": key, "amount": 101, "currency": "CNY"}
    )
    explain(key, "REJECTED", f"conflict error={conflict.json()['error']}")
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "IDEMPOTENCY_CONFLICT"

    assert client.post(f"/payments/{pid}/confirm").json()["status"] == "CONFIRMED"
    cb = client.post(
        "/callbacks",
        json={
            "event_id": "api-evt-1",
            "payment_id": pid,
            "event_type": "SUCCEEDED",
            "amount": 100,
            "sequence": 1,
        },
    )
    assert cb.json()["action"] == "applied"
    dup = client.post(
        "/callbacks",
        json={
            "event_id": "api-evt-1",
            "payment_id": pid,
            "event_type": "SUCCEEDED",
            "amount": 100,
            "sequence": 1,
        },
    )
    explain(key, "SUCCEEDED", f"duplicate HTTP callback => action={dup.json()['action']}")
    assert dup.json()["action"] == "duplicate_ignored"

    entries = client.get("/ledger/entries").json()
    assert len([e for e in entries if e["payment_id"] == pid]) == 2
    report = client.get("/reconcile").json()
    explain(key, "RECONCILED", f"diff types={ [d['diff_type'] for d in report['diffs']] }")
    assert any(
        d["payment_id"] == pid and d["diff_type"] == "MISSING_IN_CHANNEL"
        for d in report["diffs"]
    )
