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


def test_full_refund_flow_over_http(client):
    key = "api-refund-pay-key"
    rkey = "api-refund-key"
    created = client.post(
        "/payments", json={"idempotency_key": key, "amount": 100, "currency": "CNY"}
    )
    pid = created.json()["payment_id"]
    client.post(f"/payments/{pid}/confirm")
    pay_evt = client.post(
        "/channel/emit", json={"payment_id": pid, "amount": 100, "succeeded": True}
    ).json()
    client.post(
        "/callbacks",
        json={
            "event_id": pay_evt["event_id"],
            "payment_id": pid,
            "event_type": "SUCCEEDED",
            "amount": 100,
            "sequence": pay_evt["sequence"],
        },
    )

    # Partial refund: 40.
    refund = client.post(
        f"/payments/{pid}/refunds",
        json={"idempotency_key": rkey, "amount": 40, "currency": "CNY"},
    )
    assert refund.status_code == 201
    rid = refund.json()["refund_id"]

    replay = client.post(
        f"/payments/{pid}/refunds",
        json={"idempotency_key": rkey, "amount": 40, "currency": "CNY"},
    )
    explain(rkey, replay.json()["status"], "HTTP refund replay same refund_id, replayed=True")
    assert replay.json()["refund_id"] == rid and replay.json()["replayed"] is True

    conflict = client.post(
        f"/payments/{pid}/refunds",
        json={"idempotency_key": rkey, "amount": 41, "currency": "CNY"},
    )
    explain(rkey, "REJECTED", f"refund content conflict => {conflict.json()['error']}")
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "IDEMPOTENCY_CONFLICT"

    # Over-refund: 40 reserved, trying 61 must be rejected distinctly.
    over = client.post(
        f"/payments/{pid}/refunds",
        json={"idempotency_key": rkey + "-over", "amount": 61, "currency": "CNY"},
    )
    explain(rkey + "-over", "REJECTED", f"over refund => {over.json()['error']}")
    assert over.status_code == 422
    assert over.json()["error"] == "REFUND_AMOUNT_EXCEEDED"

    # Refund callback succeeds.
    refund_evt = client.post(
        "/channel/emit_refund",
        json={
            "payment_id": pid,
            "refund_id": rid,
            "amount": 40,
            "succeeded": True,
            "currency": "CNY",
        },
    ).json()
    cb = client.post(
        "/callbacks",
        json={
            "event_id": refund_evt["event_id"],
            "payment_id": pid,
            "refund_id": rid,
            "event_type": "REFUND_SUCCEEDED",
            "amount": 40,
            "sequence": refund_evt["sequence"],
            "currency": "CNY",
        },
    )
    assert cb.json()["action"] == "applied"
    dup = client.post(
        "/callbacks",
        json={
            "event_id": refund_evt["event_id"],
            "payment_id": pid,
            "refund_id": rid,
            "event_type": "REFUND_SUCCEEDED",
            "amount": 40,
            "sequence": refund_evt["sequence"],
            "currency": "CNY",
        },
    )
    explain(rkey, "SUCCEEDED", f"duplicate refund callback => {dup.json()['action']}")
    assert dup.json()["action"] == "duplicate_ignored"

    # Remaining 60 refundable.
    rest = client.post(
        f"/payments/{pid}/refunds",
        json={"idempotency_key": rkey + "-rest", "amount": 60, "currency": "CNY"},
    )
    assert rest.status_code == 201

    entries = client.get("/ledger/entries").json()
    refund_entries = [e for e in entries if e.get("refund_id") == rid]
    assert len(refund_entries) == 2
    report = client.get("/reconcile").json()
    explain(rkey, "RECONCILED", f"refund matched={rid in report['matched']} diffs={report['diffs']}")
    assert rid in report["matched"] and pid in report["matched"]
