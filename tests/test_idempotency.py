"""Idempotent acceptance: replay, conflict and concurrent same-key submission."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import IdempotencyConflict, ValidationError
from tests.conftest import explain


def test_replay_same_content_returns_same_result(service, ledger):
    first = service.create_payment("key-replay", 500, "CNY")
    replay = service.create_payment("key-replay", 500, "CNY")
    explain("key-replay", replay["status"], "same key + same content => same payment_id, replayed=True")
    assert replay["payment_id"] == first["payment_id"]
    assert replay["replayed"] is True
    assert first["replayed"] is False


def test_same_key_different_content_rejected_and_distinguishable(service):
    service.create_payment("key-conflict", 500, "CNY")
    with pytest.raises(IdempotencyConflict) as exc_info:
        service.create_payment("key-conflict", 600, "CNY")
    explain("key-conflict", "REJECTED", "same key + different content => IDEMPOTENCY_CONFLICT")
    assert exc_info.value.code == "IDEMPOTENCY_CONFLICT"
    # Distinguishable from other failure reasons.
    with pytest.raises(ValidationError) as other:
        service.create_payment("key-bad", -1, "CNY")
    explain("key-bad", "REJECTED", "negative amount => VALIDATION_ERROR, distinct from conflict")
    assert other.value.code != IdempotencyConflict.code


def test_concurrent_same_key_single_funds_movement(service, ledger):
    workers = 16
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(
            pool.map(
                lambda _: service.create_payment("key-race", 700, "CNY"),
                range(workers),
            )
        )
    payment_ids = {r["payment_id"] for r in results}
    explain("key-race", results[0]["status"], f"{workers} concurrent submissions => one payment, owners={sum(1 for r in results if not r['replayed'])}")
    assert len(payment_ids) == 1
    assert sum(1 for r in results if not r["replayed"]) == 1

    pid = payment_ids.pop()
    service.confirm_payment(pid)
    service.apply_channel_result(pid, True)
    from app.payments import CLEARING_ACCOUNT

    posted = ledger.net_amount(pid, CLEARING_ACCOUNT)
    explain("key-race", "SUCCEEDED", f"net clearing amount={posted}: exactly one funds movement")
    assert posted == -700


def test_replay_after_completion_still_consistent(service):
    first = service.create_payment("key-late-replay", 100, "CNY")
    service.confirm_payment(first["payment_id"])
    service.apply_channel_result(first["payment_id"], True)
    replay = service.create_payment("key-late-replay", 100, "CNY")
    explain("key-late-replay", replay["status"], "replay after terminal state returns original acceptance result")
    assert replay["payment_id"] == first["payment_id"]
    assert replay["replayed"] is True
