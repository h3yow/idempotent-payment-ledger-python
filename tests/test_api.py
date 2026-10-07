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


def test_refund_flow_over_http(client):
    pay = client.post(
        "/payments", json={"idempotency_key": "api-rf-pay", "amount": 500, "currency": "CNY"}
    )
    assert pay.status_code == 201
    pid = pay.json()["payment_id"]
    client.post(f"/payments/{pid}/confirm")
    client.post(
        "/callbacks",
        json={"event_id": "api-rf-evt-pay", "payment_id": pid, "event_type": "SUCCEEDED", "amount": 500},
    )

    created = client.post(
        f"/payments/{pid}/refunds",
        json={"idempotency_key": "api-rf-key", "amount": 200, "currency": "CNY"},
    )
    assert created.status_code == 201
    rid = created.json()["refund_id"]

    replay = client.post(
        f"/payments/{pid}/refunds",
        json={"idempotency_key": "api-rf-key", "amount": 200, "currency": "CNY"},
    )
    explain("api-rf-key", replay.json()["status"], "HTTP refund replay returns same refund_id")
    assert replay.json()["refund_id"] == rid and replay.json()["replayed"] is True

    conflict = client.post(
        f"/payments/{pid}/refunds",
        json={"idempotency_key": "api-rf-key", "amount": 201, "currency": "CNY"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "IDEMPOTENCY_CONFLICT"

    over = client.post(
        f"/payments/{pid}/refunds",
        json={"idempotency_key": "api-rf-over", "amount": 400, "currency": "CNY"},
    )
    explain("api-rf-over", "REJECTED", f"200+400 > 500 => {over.json()['error']}")
    assert over.status_code == 409
    assert over.json()["error"] == "REFUND_EXCEEDS_PAYMENT"

    emit = client.post(
        "/channel/emit-refund",
        json={"refund_id": rid, "payment_id": pid, "amount": 200, "currency": "CNY"},
    )
    assert emit.status_code == 201
    cb = client.post(
        "/callbacks",
        json={
            "event_id": "api-rf-evt-ref",
            "payment_id": pid,
            "refund_id": rid,
            "event_type": "SUCCEEDED",
            "amount": 200,
            "currency": "CNY",
        },
    )
    assert cb.json()["action"] == "applied"
    assert client.get(f"/refunds/{rid}").json()["status"] == "SUCCEEDED"

    entries = client.get("/ledger/entries").json()
    assert len([e for e in entries if e.get("refund_id") == rid]) == 2
