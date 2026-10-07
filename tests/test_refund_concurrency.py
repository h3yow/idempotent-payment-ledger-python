"""Concurrent refund submissions: one execution per key, no over-refund."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import RefundAmountExceeded
from app.models import RefundStatus
from app.payments import CLEARING_ACCOUNT
from tests.conftest import explain


def _succeeded(service, key, amount=1000):
    pid = service.create_payment(key, amount, "CNY")["payment_id"]
    service.confirm_payment(pid)
    service.apply_channel_result(pid, True)
    return pid


def test_concurrent_same_refund_key_single_acceptance(refunds, service):
    pid = _succeeded(service, "rc-rf-same-pay", 500)
    workers = 16
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(
            pool.map(
                lambda _: refunds.create_refund(pid, "rc-rf-same", 300, "CNY"),
                range(workers),
            )
        )
    ids = {r["refund_id"] for r in results}
    owners = sum(1 for r in results if not r["replayed"])
    explain(
        "rc-rf-same", results[0]["status"],
        f"{workers} concurrent same-key refunds => {len(ids)} refund, owners={owners}",
    )
    assert ids == {results[0]["refund_id"]}
    assert owners == 1
    assert len(refunds.refunds_for(pid)) == 1


def test_concurrent_refunds_never_over_refund(refunds, service, ledger):
    pid = _succeeded(service, "rc-rf-over-pay", 1000)
    # 20 workers each try to reserve 100: exactly 10 can win.
    workers = 20
    barrier = []

    def submit(i):
        return refunds.create_refund(pid, f"rc-rf-over-{i}", 100, "CNY")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(submit, i) for i in range(workers)]
        outcomes = []
        for f in futures:
            try:
                outcomes.append(("ok", f.result()))
            except RefundAmountExceeded as exc:
                outcomes.append(("rejected", exc))

    accepted = [r for kind, r in outcomes if kind == "ok"]
    rejected = [exc for kind, exc in outcomes if kind == "rejected"]
    reserved = sum(r.amount for r in refunds.refunds_for(pid))
    explain(
        "rc-rf-over-*", "MIXED",
        f"accepted={len(accepted)} rejected={len(rejected)} total_reserved={reserved}/1000",
    )
    assert len(accepted) == 10
    assert len(rejected) == 10
    assert {exc.code for exc in rejected} == {"REFUND_AMOUNT_EXCEEDED"}
    assert reserved == 1000

    # Settle every accepted refund concurrently as well: no duplicate posting.
    refund_ids = [r["refund_id"] for r in accepted]
    with ThreadPoolExecutor(max_workers=10) as pool:
        settled = list(pool.map(lambda rid: refunds.apply_channel_result(rid, True), refund_ids))
    assert all(r.status is RefundStatus.SUCCEEDED for r in settled)
    entries = [e for e in ledger.entries() if e.payment_id == pid]
    assert len(entries) == 2 + 20  # payment pair + ten balanced refund pairs
    explain(
        "rc-rf-over-*", "SUCCEEDED",
        f"concurrent settlement entries={len(entries)}, net clearing={ledger.net_amount(pid, CLEARING_ACCOUNT)}",
    )
    # payment debit 1000, refund credits 1000 -> net zero; conservation holds.
    assert ledger.net_amount(pid, CLEARING_ACCOUNT) == 0


def test_no_half_finished_records_on_refund_failure(refunds, service, ledger):
    pid = _succeeded(service, "rc-rf-fail-pay", 200)
    refund = refunds.create_refund(pid, "rc-rf-fail", 200, "CNY")
    before = len(ledger.entries())
    result = refunds.apply_channel_result(refund["refund_id"], False)
    after = len(ledger.entries())
    explain(
        "rc-rf-fail", result.status.value,
        f"failed refund posts no entries ({before} -> {after}); capacity released",
    )
    assert result.status is RefundStatus.FAILED
    assert before == after
