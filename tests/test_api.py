"""End-to-end HTTP tests: existing endpoints stay usable, error codes are
distinguishable and the ledger stays balanced through the API."""
import logging

import pytest
from fastapi.testclient import TestClient

from main import create_app

log = logging.getLogger("test.api")


@pytest.fixture
def client():
    return TestClient(create_app())


def test_existing_endpoints_available(client):
    assert client.get("/").json() == {"Hello": "World"}
    assert client.get("/payments/health").json() == {"status": "ok"}


def test_full_http_flow_with_replay(client):
    payload = {"idempotency_key": "http-1", "amount": 100, "currency": "CNY"}
    r1 = client.post("/payments", json=payload)
    r2 = client.post("/payments", json=payload)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["payment_id"] == r2.json()["payment_id"]
    pid = r1.json()["payment_id"]
    confirmed = client.post(f"/payments/{pid}/confirm").json()
    assert confirmed["status"] == "SUCCEEDED"
    log.info("key=%s payment=%s status=%s", "http-1", pid, confirmed["status"])
    assert client.get("/ledger/balance").json()["net_balance"] == 0
    assert client.get("/reconcile").json()["diff_count"] == 0


def test_conflict_vs_not_found_distinguishable(client):
    client.post(
        "/payments",
        json={"idempotency_key": "http-2", "amount": 100, "currency": "CNY"},
    )
    conflict = client.post(
        "/payments",
        json={"idempotency_key": "http-2", "amount": 200, "currency": "CNY"},
    )
    missing = client.get("/payments/pay_does_not_exist")
    log.info(
        "conflict=%s not_found=%s basis=distinct error codes",
        conflict.json()["error"]["code"], missing.json()["error"]["code"],
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "PAYMENT_NOT_FOUND"


def test_illegal_transition_via_http(client):
    r = client.post(
        "/payments",
        json={"idempotency_key": "http-3", "amount": 50, "currency": "CNY"},
    )
    pid = r.json()["payment_id"]
    client.post(f"/payments/{pid}/confirm")
    again = client.post(f"/payments/{pid}/confirm")
    log.info(
        "key=%s payment=%s duplicate_confirm_code=%s",
        "http-3", pid, again.json()["error"]["code"],
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "INVALID_STATE_TRANSITION"
