"""Refund acceptance: idempotency, content conflicts and over-refund rejection."""

from app.errors import (
    IdempotencyConflict,
    RefundAmountExceeded,
    RefundNotAllowed,
)
from app.models import RefundStatus
from app.payments import CLEARING_ACCOUNT
from tests.conftest import explain


def _succeeded_payment(service, key: str, amount: int = 1000) -> str:
    pid = service.create_payment(key, amount, "CNY")["payment_id"]
    service.confirm_payment(pid)
    service.apply_channel_result(pid, True)
    return pid


def test_refund_replay_same_content_counts_once(refunds, service):
    pid = _succeeded_payment(service, "rf-replay-pay", 500)
    first = refunds.create_refund(pid, "rf-replay", 200, "CNY")
    replay = refunds.create_refund(pid, "rf-replay", 200, "CNY")
    explain(
        "rf-replay", replay["status"],
        "same key + same content => one refund, replayed=True, consistent result",
    )
    assert replay["refund_id"] == first["refund_id"]
    assert first["replayed"] is False and replay["replayed"] is True
    assert len(refunds.refunds_for(pid)) == 1


def test_refund_same_key_different_content_rejected(refunds, service):
    pid = _succeeded_payment(service, "rf-conflict-pay")
    refunds.create_refund(pid, "rf-conflict", 100, "CNY")
    try:
        refunds.create_refund(pid, "rf-conflict", 200, "CNY")
        raised = None
    except IdempotencyConflict as exc:
        raised = exc
    explain(
        "rf-conflict", "REJECTED",
        f"same key + different amount => IDEMPOTENCY_CONFLICT ({raised.code if raised else 'NO ERROR'})",
    )
    assert raised is not None and raised.code == "IDEMPOTENCY_CONFLICT"
    assert len(refunds.refunds_for(pid)) == 1


def test_over_refund_is_rejected_distinctly(refunds, service):
    pid = _succeeded_payment(service, "rf-over-pay", 1000)
    refunds.create_refund(pid, "rf-over-1", 600, "CNY")
    try:
        refunds.create_refund(pid, "rf-over-2", 401, "CNY")
        raised = None
    except RefundAmountExceeded as exc:
        raised = exc
    explain(
        "rf-over-2", "REJECTED",
        "600+401 > 1000 => REFUND_AMOUNT_EXCEEDED, distinct from idempotency conflict",
    )
    assert raised is not None and raised.code == "REFUND_AMOUNT_EXCEEDED"
    assert raised.code != IdempotencyConflict.code
    # Exactly the boundary succeeds: 600 + 400 = 1000.
    full = refunds.create_refund(pid, "rf-over-3", 400, "CNY")
    explain("rf-over-3", full["status"], "cumulative 600+400=1000 full refund accepted")
    assert full["status"] == RefundStatus.CREATED.value


def test_partial_refunds_accumulate_and_full_amount_conserved(refunds, service, ledger):
    pid = _succeeded_payment(service, "rf-parts-pay", 300)
    r1 = refunds.create_refund(pid, "rf-parts-1", 100, "CNY")
    r2 = refunds.create_refund(pid, "rf-parts-2", 200, "CNY")
    refunds.apply_channel_result(r1["refund_id"], True)
    refunds.apply_channel_result(r2["refund_id"], True)
    posted = [e for e in ledger.entries() if e.payment_id == pid]
    clearing = ledger.net_amount(pid, CLEARING_ACCOUNT)
    explain(
        "rf-parts-*", "SUCCEEDED",
        f"two partial refunds posted 4 entries; net clearing={clearing} "
        "(payment -300 + refunds +300)",
    )
    assert len(posted) == 2 + 4  # 2 payment entries + 2 balanced refund postings
    assert clearing == 0
    assert refunds.get(r1["refund_id"]).status is RefundStatus.SUCCEEDED


def test_refund_requires_succeeded_payment(refunds, service):
    pid = service.create_payment("rf-notallowed-pay", 100, "CNY")["payment_id"]
    service.confirm_payment(pid)  # CONFIRMED but not SUCCEEDED
    try:
        refunds.create_refund(pid, "rf-notallowed", 50, "CNY")
        raised = None
    except RefundNotAllowed as exc:
        raised = exc
    explain(
        "rf-notallowed", "REJECTED",
        "refund on non-SUCCEEDED payment => REFUND_NOT_ALLOWED",
    )
    assert raised is not None and raised.code == "REFUND_NOT_ALLOWED"


def test_failed_refund_releases_capacity(refunds, service):
    pid = _succeeded_payment(service, "rf-release-pay", 100)
    first = refunds.create_refund(pid, "rf-release-1", 100, "CNY")
    # Channel reports the refund failed; capacity is released.
    assert refunds.apply_channel_result(first["refund_id"], False).status is RefundStatus.FAILED
    again = refunds.create_refund(pid, "rf-release-2", 100, "CNY")
    explain(
        "rf-release-2", again["status"],
        "FAILED refund released capacity; full refund can be retried under a new key",
    )
    assert again["status"] == RefundStatus.CREATED.value
