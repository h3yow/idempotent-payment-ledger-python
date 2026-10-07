"""Refund acceptance: idempotency, over-refund rejection, concurrency, entries."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import (
    IdempotencyConflict,
    InvalidStateTransition,
    RefundExceedsPayment,
    ValidationError,
)
from app.models import RefundStatus
from app.payments import CLEARING_ACCOUNT, REVENUE_ACCOUNT
from tests.conftest import explain


def _succeeded(service, key, amount, currency="CNY"):
    pid = service.create_payment(key, amount, currency)["payment_id"]
    service.confirm_payment(pid)
    service.apply_channel_result(pid, True)
    return pid


def test_partial_and_full_refund_acceptance(service, refunds):
    pid = _succeeded(service, "rf-accept", 1000)
    r1 = refunds.create_refund(pid, "rf-accept-1", 300, "CNY")
    r2 = refunds.create_refund(pid, "rf-accept-2", 700, "CNY")
    explain("rf-accept-1/2", r1["status"], "300+700 == 1000: partial then full remainder both accepted")
    assert r1["status"] == RefundStatus.CREATED.value
    assert refunds.refunded_amount(pid) == 1000
    with pytest.raises(RefundExceedsPayment) as exc_info:
        refunds.create_refund(pid, "rf-accept-3", 1, "CNY")
    explain("rf-accept-3", "REJECTED", f"cumulative 1000 + 1 > 1000 => {exc_info.value.code}")
    assert exc_info.value.code == "REFUND_EXCEEDS_PAYMENT"


def test_refund_replay_same_content_returns_same_result(service, refunds):
    pid = _succeeded(service, "rf-replay", 500)
    first = refunds.create_refund(pid, "rf-replay-key", 200, "CNY")
    replay = refunds.create_refund(pid, "rf-replay-key", 200, "CNY")
    explain("rf-replay-key", replay["status"], "same key + same content => same refund_id, replayed=True")
    assert replay["refund_id"] == first["refund_id"]
    assert replay["replayed"] is True
    assert first["replayed"] is False
    assert refunds.refunded_amount(pid) == 200  # counted once


def test_refund_same_key_different_content_rejected_and_distinguishable(service, refunds):
    pid = _succeeded(service, "rf-conflict", 500)
    refunds.create_refund(pid, "rf-conflict-key", 200, "CNY")
    with pytest.raises(IdempotencyConflict) as conflict:
        refunds.create_refund(pid, "rf-conflict-key", 300, "CNY")
    explain("rf-conflict-key", "REJECTED", "same key + different amount => IDEMPOTENCY_CONFLICT")
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    # Distinguishable from over-refund and validation failures.
    with pytest.raises(RefundExceedsPayment) as over:
        refunds.create_refund(pid, "rf-over-key", 400, "CNY")
    with pytest.raises(ValidationError) as bad:
        refunds.create_refund(pid, "rf-bad-key", -1, "CNY")
    explain("rf-over-key", "REJECTED", f"over-refund => {over.value.code}; bad amount => {bad.value.code}")
    assert len({conflict.value.code, over.value.code, bad.value.code}) == 3


def test_over_refund_rejected_not_silently_capped(service, refunds, ledger):
    pid = _succeeded(service, "rf-over", 400)
    refunds.create_refund(pid, "rf-over-1", 250, "CNY")
    with pytest.raises(RefundExceedsPayment):
        refunds.create_refund(pid, "rf-over-2", 200, "CNY")
    explain("rf-over-2", "REJECTED", "250+200 > 400 rejected; cumulative stays 250, nothing posted")
    assert refunds.refunded_amount(pid) == 250
    assert all(not e.refund_id for e in ledger.entries())  # no refund entries yet


def test_failed_refund_frees_quota(service, refunds):
    pid = _succeeded(service, "rf-quota", 500)
    r = refunds.create_refund(pid, "rf-quota-1", 500, "CNY")
    refunds.apply_channel_result(r["refund_id"], False, 500, "CNY")
    again = refunds.create_refund(pid, "rf-quota-2", 500, "CNY")
    explain("rf-quota-2", again["status"], "failed refund does not count against the cap")
    assert again["status"] == RefundStatus.CREATED.value


def test_refund_requires_succeeded_payment(service, refunds):
    pid = service.create_payment("rf-early", 100, "CNY")["payment_id"]
    with pytest.raises(InvalidStateTransition):
        refunds.create_refund(pid, "rf-early-key", 50, "CNY")
    explain("rf-early-key", "REJECTED", "refund on CREATED payment is an illegal transition")


def test_refund_currency_must_match_payment(service, refunds):
    pid = _succeeded(service, "rf-ccy", 100, currency="CNY")
    with pytest.raises(ValidationError):
        refunds.create_refund(pid, "rf-ccy-key", 50, "USD")
    explain("rf-ccy-key", "REJECTED", "refund currency USD != payment currency CNY")


def test_concurrent_same_key_refund_single_acceptance(service, refunds):
    pid = _succeeded(service, "rf-race-key", 600)
    workers = 16
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(
            pool.map(
                lambda _: refunds.create_refund(pid, "rf-race", 200, "CNY"),
                range(workers),
            )
        )
    refund_ids = {r["refund_id"] for r in results}
    explain("rf-race", results[0]["status"], f"{workers} concurrent same-key refunds => one refund")
    assert len(refund_ids) == 1
    assert sum(1 for r in results if not r["replayed"]) == 1
    assert refunds.refunded_amount(pid) == 200


def test_concurrent_partial_refunds_never_over_refund(service, refunds, ledger):
    pid = _succeeded(service, "rf-race-cap", 1000)
    workers = 8  # 8 x 300 = 2400 > 1000; exactly 3 fit (900)

    def attempt(i):
        try:
            return refunds.create_refund(pid, f"rf-cap-{i}", 300, "CNY")
        except RefundExceedsPayment:
            return None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(attempt, range(workers)))
    accepted = [r for r in results if r is not None]
    explain("rf-cap-*", "ACCEPTED", f"{len(accepted)}/{workers} accepted, cumulative={refunds.refunded_amount(pid)}")
    assert len(accepted) == 3
    assert refunds.refunded_amount(pid) == 900 <= 1000

    for r in accepted:
        refunds.apply_channel_result(r["refund_id"], True, 300, "CNY")
    total = ledger.balance(CLEARING_ACCOUNT) + ledger.balance(REVENUE_ACCOUNT)
    explain("rf-cap-*", "SUCCEEDED", f"ledger total after 3 refunds = {total}: conserved, no partial postings")
    assert total == 0
    # Payment 1000 in, refunds 900 out: clearing net = -100, revenue net = +100.
    assert ledger.balance(CLEARING_ACCOUNT) == -100
    assert ledger.balance(REVENUE_ACCOUNT) == 100
    # Every refund posted exactly one balanced pair.
    refund_entries = [e for e in ledger.entries() if e.refund_id]
    assert len(refund_entries) == 6


def test_refund_success_posts_balanced_reversing_entries(service, refunds, ledger):
    pid = _succeeded(service, "rf-entries", 800)
    r = refunds.create_refund(pid, "rf-entries-key", 300, "CNY")
    refunds.apply_channel_result(r["refund_id"], True, 300, "CNY")
    explain("rf-entries-key", "SUCCEEDED", "refund posts debit revenue / credit clearing, balanced")
    assert ledger.net_amount(pid, CLEARING_ACCOUNT) == -800 + 300
    assert ledger.balance(CLEARING_ACCOUNT) + ledger.balance(REVENUE_ACCOUNT) == 0


def test_refund_terminal_state_sticky(service, refunds):
    pid = _succeeded(service, "rf-sticky", 400)
    r = refunds.create_refund(pid, "rf-sticky-key", 100, "CNY")
    rid = r["refund_id"]
    refunds.apply_channel_result(rid, False, 100, "CNY")
    again = refunds.apply_channel_result(rid, False, 100, "CNY")  # repeat: no-op
    explain("rf-sticky-key", again.status.value, "repeated failure callback converges safely")
    assert again.status is RefundStatus.FAILED
    with pytest.raises(InvalidStateTransition):
        refunds.apply_channel_result(rid, True, 100, "CNY")
    explain("rf-sticky-key", "FAILED", "late contradicting success rejected; terminal sticky")
    assert refunds.get(rid).status is RefundStatus.FAILED
