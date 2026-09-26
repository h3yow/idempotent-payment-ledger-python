"""Idempotent acceptance: replay, conflict and concurrent same-key submission."""
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import IdempotencyConflictError, IdempotencyInProgressError

log = logging.getLogger("test.idempotency")


def test_same_key_same_payload_replays(service):
    code1, body1 = service.create_payment("key-A", 100, "CNY")
    code2, body2 = service.create_payment("key-A", 100, "CNY")
    log.info(
        "key=%s first=%s replay=%s basis=stored response returned for identical payload",
        "key-A", body1["payment_id"], body2["payment_id"],
    )
    assert code1 == code2 == 201
    assert body1["payment_id"] == body2["payment_id"]
    assert body2["replayed"] is True
    assert len(service.list_payments()) == 1


def test_same_key_different_payload_rejected(service):
    service.create_payment("key-B", 100, "CNY")
    with pytest.raises(IdempotencyConflictError) as exc:
        service.create_payment("key-B", 200, "CNY")
    log.info(
        "key=%s status=rejected code=%s basis=fingerprint mismatch",
        "key-B", exc.value.code,
    )
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    assert exc.value.code != "PAYMENT_NOT_FOUND"  # distinguishable


def test_concurrent_same_key_single_acceptance(service):
    # Hold the first claim open until every worker is contending for the key,
    # forcing the in_progress race window wide instead of racing on CPU speed.
    import threading

    barrier = threading.Barrier(16)
    original_begin = service.idem.begin

    def contended_begin(key, fp):
        try:
            record, replay = original_begin(key, fp)
            if record.in_progress:  # winner: hold the open claim until all arrive
                barrier.wait(timeout=5)
            return record, replay
        except IdempotencyInProgressError:
            # losers synchronize too, then report the in-progress conflict
            barrier.wait(timeout=5)
            raise

    service.idem.begin = contended_begin
    results = []

    def submit(i):
        try:
            code, body = service.create_payment("key-C", 500, "CNY")
            results.append(("ok", body["payment_id"]))
        except IdempotencyInProgressError:
            results.append(("in_progress", None))

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(submit, range(16)))

    ok_ids = {pid for kind, pid in results if kind == "ok"}
    log.info(
        "key=%s ok=%d in_progress=%d distinct_payment_ids=%d basis=one claim wins",
        "key-C", sum(1 for k, _ in results if k == "ok"),
        sum(1 for k, _ in results if k == "in_progress"), len(ok_ids),
    )
    assert len(ok_ids) == 1  # only one payment created
    assert sum(1 for k, _ in results if k == "in_progress") == 15
    assert len(service.list_payments()) == 1

    # Retries after the winner completes must replay the same result.
    code, body = service.create_payment("key-C", 500, "CNY")
    assert code == 201 and body["replayed"] is True
    assert len(service.list_payments()) == 1


def test_concurrent_confirm_posts_ledger_once(service):
    _, body = service.create_payment("key-D", 300, "CNY")
    pid = body["payment_id"]

    outcomes = []
    def do_confirm(_):
        try:
            service.confirm(pid)
            outcomes.append("confirmed")
        except Exception as e:
            outcomes.append(getattr(e, "code", type(e).__name__))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(do_confirm, range(16)))

    entries = service.ledger.entries_for(pid)
    log.info(
        "key=%s payment=%s status=%s entries=%d outcomes=%s basis=ledger pair posted exactly once",
        "key-D", pid, service.get_payment(pid).status.value, len(entries), outcomes,
    )
    assert len(entries) == 2  # exactly one balanced pair
    assert service.ledger.net_balance() == 0
