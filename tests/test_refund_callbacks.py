"""Refund callbacks: duplicates, out-of-order, late, amount/currency mismatch."""

from app.models import ChannelEventType, RefundStatus
from app.payments import CLEARING_ACCOUNT
from tests.conftest import explain


def _accepted_refund(refunds, service, channel, key, amount=100, cb_amount=None,
                     currency="CNY", pay_key=None):
    pid = service.create_payment(pay_key or key + "-pay", amount, "CNY")["payment_id"]
    service.confirm_payment(pid)
    service.apply_channel_result(pid, True)
    accepted = refunds.create_refund(pid, key, cb_amount or amount, currency)
    return pid, accepted["refund_id"]


def test_refund_duplicate_callback_applied_once(refunds, service, channel, processor, ledger):
    pid, rid = _accepted_refund(refunds, service, channel, "rfc-dup", 100)
    event = channel.emit_refund(rid, pid, 100, succeeded=True, currency="CNY")
    channel.duplicate(event)
    channel.duplicate(event)
    results = processor.handle_many(channel.events())
    actions = [r["action"] for r in results]
    explain("rfc-dup", refunds.get(rid).status.value, f"3 identical refund events => {actions}")
    assert actions == ["applied", "duplicate_ignored", "duplicate_ignored"]
    assert ledger.net_amount(pid, CLEARING_ACCOUNT) == 0  # -100 payment +100 refund


def test_refund_callbacks_out_of_order(refunds, service, channel, processor):
    created = []
    for i in range(4):
        pid, rid = _accepted_refund(
            refunds, service, channel, f"rfc-ooo-{i}", 100 + i
        )
        channel.emit_refund(rid, pid, 100 + i, succeeded=True, currency="CNY")
        created.append(rid)
    shuffled = channel.shuffled_events(seed=7)
    assert [e.refund_id for e in shuffled] != created
    results = processor.handle_many(shuffled)
    explain(
        "rfc-ooo-*", "SUCCEEDED",
        f"shuffled refund callbacks {[e.event_id for e in shuffled]} all applied",
    )
    assert all(r["action"] == "applied" for r in results)
    assert all(refunds.get(rid).status is RefundStatus.SUCCEEDED for rid in created)


def test_late_refund_callback_cannot_rewrite_terminal(refunds, service, channel, processor):
    pid, rid = _accepted_refund(refunds, service, channel, "rfc-late", 200)
    fail = channel.emit_refund(rid, pid, 200, succeeded=False, currency="CNY")
    assert processor.handle(fail)["action"] == "applied"
    late = channel.emit_refund(rid, pid, 200, succeeded=True, currency="CNY")
    result = processor.handle(late)
    explain(
        "rfc-late", refunds.get(rid).status.value,
        f"late REFUND_SUCCEEDED after FAILED => action={result['action']} code={result.get('reason_code')}",
    )
    assert result["action"] == "rejected"
    assert result["reason_code"] == "INVALID_STATE_TRANSITION"
    assert refunds.get(rid).status is RefundStatus.FAILED


def test_refund_callback_amount_mismatch_distinguishable(refunds, service, channel, processor, ledger):
    pid, rid = _accepted_refund(refunds, service, channel, "rfc-amt", 100)
    bad = channel.emit_refund(rid, pid, 90, succeeded=True, currency="CNY")
    result = processor.handle(bad)
    explain(
        "rfc-amt", refunds.get(rid).status.value,
        f"callback amount 90 != accepted 100 => {result['reason_code']}",
    )
    assert result["action"] == "rejected"
    assert result["reason_code"] == "CALLBACK_AMOUNT_MISMATCH"
    assert refunds.get(rid).status is RefundStatus.CREATED  # nothing advanced
    # A correct later callback still converges.
    good = channel.emit_refund(rid, pid, 100, succeeded=True, currency="CNY")
    ok = processor.handle(good)
    explain("rfc-amt", ok["status"], "subsequent correct callback applies the refund")
    assert ok["action"] == "applied" and ok["status"] == "SUCCEEDED"


def test_refund_callback_currency_mismatch_distinguishable(refunds, service, channel, processor, ledger):
    pid, rid = _accepted_refund(refunds, service, channel, "rfc-cur", 100)
    bad = channel.emit_refund(rid, pid, 100, succeeded=True, currency="USD")
    result = processor.handle(bad)
    explain(
        "rfc-cur", refunds.get(rid).status.value,
        f"callback currency USD != accepted CNY => {result['reason_code']}",
    )
    assert result["action"] == "rejected"
    assert result["reason_code"] == "CALLBACK_CURRENCY_MISMATCH"
    assert refunds.get(rid).status is RefundStatus.CREATED


def test_one_bad_refund_event_does_not_block_others(refunds, service, channel, processor):
    pid, good_rid = _accepted_refund(refunds, service, channel, "rfc-good", 150)
    _, other_rid = _accepted_refund(
        refunds, service, channel, "rfc-other", 50, pay_key="rfc-other-pay"
    )
    bad = channel.emit_refund("ref_unknown", pid, 150, succeeded=True, currency="CNY")
    good = channel.emit_refund(good_rid, pid, 150, succeeded=True, currency="CNY")
    results = processor.handle_many([bad, good])
    explain(
        "rfc-good", refunds.get(good_rid).status.value,
        f"unknown refund rejected ({results[0]['reason_code']}), good refund unaffected",
    )
    assert results[0]["action"] == "rejected"
    assert results[1]["action"] == "applied"
    assert refunds.get(good_rid).status is RefundStatus.SUCCEEDED


def test_refund_failure_then_success_out_of_order_contradiction(refunds, service, channel, processor):
    """Failure first, success late: terminal FAILED is sticky; distinguishes codes."""
    pid, rid = _accepted_refund(refunds, service, channel, "rfc-contra", 80)
    failure = channel.emit_refund(rid, pid, 80, succeeded=False, currency="CNY")
    success = channel.emit_refund(rid, pid, 80, succeeded=True, currency="CNY")
    results = processor.handle_many([failure, success])
    explain(
        "rfc-contra", "FAILED",
        f"failure applied, contradicting success rejected: {[r['action'] for r in results]}",
    )
    assert results[0]["action"] == "applied"
    assert results[1]["action"] == "rejected"
    assert results[1]["reason_code"] == "INVALID_STATE_TRANSITION"
