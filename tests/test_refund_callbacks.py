"""Refund callbacks (duplicate/out-of-order/late/mismatch) and refund reconciliation."""

from app.models import ReconDiffType, RefundStatus
from app.payments import CLEARING_ACCOUNT
from app.reconcile import reconcile
from tests.conftest import explain


def _succeeded_payment(service, key, amount):
    pid = service.create_payment(key, amount, "CNY")["payment_id"]
    service.confirm_payment(pid)
    service.apply_channel_result(pid, True)
    return pid


def _refund(refunds, pid, key, amount):
    return refunds.create_refund(pid, key, amount, "CNY")["refund_id"]


# -- callback delivery semantics ------------------------------------------


def test_duplicate_refund_callback_applied_once(service, refunds, ledger, channel, refund_processor):
    pid = _succeeded_payment(service, "rcb-dup", 500)
    rid = _refund(refunds, pid, "rcb-dup-key", 200)
    event = channel.emit_refund(rid, pid, 200, "CNY", succeeded=True)
    channel.duplicate(event)
    channel.duplicate(event)
    results = refund_processor.handle_many(channel.events())
    actions = [r["action"] for r in results]
    explain("rcb-dup-key", refunds.get(rid).status.value, f"3 identical refund events => actions={actions}")
    assert actions == ["applied", "duplicate_ignored", "duplicate_ignored"]
    assert refunds.get(rid).status is RefundStatus.SUCCEEDED
    # Exactly one balanced refund posting.
    assert len([e for e in ledger.entries() if e.refund_id == rid]) == 2


def test_out_of_order_refund_callbacks_converge(service, refunds, channel, refund_processor):
    pid = _succeeded_payment(service, "rcb-ooo", 900)
    rids = [_refund(refunds, pid, f"rcb-ooo-{i}", 100) for i in range(3)]
    for rid in rids:
        channel.emit_refund(rid, pid, 100, "CNY", succeeded=True)
    shuffled = channel.shuffled_events(seed=7)
    assert [e.refund_id for e in shuffled] != rids  # genuinely out of order
    results = refund_processor.handle_many(shuffled)
    explain("rcb-ooo-*", "SUCCEEDED", f"shuffled {[e.event_id for e in shuffled]} all applied")
    assert all(r["action"] == "applied" for r in results)
    assert all(refunds.get(r).status is RefundStatus.SUCCEEDED for r in rids)


def test_late_contradicting_refund_callback_rejected(service, refunds, channel, refund_processor):
    pid = _succeeded_payment(service, "rcb-late", 400)
    rid = _refund(refunds, pid, "rcb-late-key", 100)
    fail = channel.emit_refund(rid, pid, 100, "CNY", succeeded=False)
    assert refund_processor.handle(fail)["action"] == "applied"
    late = channel.emit_refund(rid, pid, 100, "CNY", succeeded=True)
    result = refund_processor.handle(late)
    explain("rcb-late-key", refunds.get(rid).status.value, f"late success after FAILED => action={result['action']} reason={result.get('reason_code')}")
    assert result["action"] == "rejected"
    assert result["reason_code"] == "INVALID_STATE_TRANSITION"
    assert refunds.get(rid).status is RefundStatus.FAILED


def test_refund_callback_amount_mismatch_distinguishable(service, refunds, channel, refund_processor):
    pid = _succeeded_payment(service, "rcb-amt", 400)
    rid = _refund(refunds, pid, "rcb-amt-key", 100)
    bad = channel.emit_refund(rid, pid, 150, "CNY", succeeded=True)
    result = refund_processor.handle(bad)
    explain("rcb-amt-key", refunds.get(rid).status.value, f"callback amount 150 != accepted 100 => {result['action']} reason={result.get('reason_code')}")
    assert result["action"] == "rejected"
    assert result["reason_code"] == "CALLBACK_MISMATCH"
    assert refunds.get(rid).status is RefundStatus.CREATED  # not advanced
    # A later correct callback still applies: mismatch did not wedge the refund.
    good = channel.emit_refund(rid, pid, 100, "CNY", succeeded=True)
    assert refund_processor.handle(good)["action"] == "applied"
    assert refunds.get(rid).status is RefundStatus.SUCCEEDED


def test_refund_callback_currency_mismatch_distinguishable(service, refunds, channel, refund_processor):
    pid = _succeeded_payment(service, "rcb-ccy", 400)
    rid = _refund(refunds, pid, "rcb-ccy-key", 100)
    bad = channel.emit_refund(rid, pid, 100, "USD", succeeded=True)
    result = refund_processor.handle(bad)
    explain("rcb-ccy-key", refunds.get(rid).status.value, f"callback currency USD != CNY => {result['action']} reason={result.get('reason_code')}")
    assert result["action"] == "rejected"
    assert result["reason_code"] == "CALLBACK_MISMATCH"
    assert refunds.get(rid).status is RefundStatus.CREATED


def test_one_bad_refund_event_does_not_block_others(service, refunds, channel, refund_processor):
    pid = _succeeded_payment(service, "rcb-iso", 600)
    rid_good = _refund(refunds, pid, "rcb-iso-good", 100)
    rid_bad = _refund(refunds, pid, "rcb-iso-bad", 100)
    evt_bad = channel.emit_refund(rid_bad, pid, 999, "CNY", succeeded=True)
    evt_good = channel.emit_refund(rid_good, pid, 100, "CNY", succeeded=True)
    results = refund_processor.handle_many([evt_bad, evt_good])
    explain("rcb-iso-good", refunds.get(rid_good).status.value, f"bad event action={results[0]['action']} does not block good event")
    assert results[0]["action"] == "rejected"
    assert results[0]["reason_code"] == "CALLBACK_MISMATCH"
    assert results[1]["action"] == "applied"
    assert refunds.get(rid_good).status is RefundStatus.SUCCEEDED


# -- reconciliation over refund flows --------------------------------------


def _channel_settled_payment(service, channel, refund_processor, key, amount):
    """Settle a payment through the channel so both sides have a record."""
    pid = service.create_payment(key, amount, "CNY")["payment_id"]
    service.confirm_payment(pid)
    refund_processor.handle(channel.emit(pid, amount, succeeded=True))
    return pid


def _settled_refund(service, refunds, channel, refund_processor, key, pay_amount, ref_amount):
    pid = _channel_settled_payment(service, channel, refund_processor, f"{key}-pay", pay_amount)
    rid = _refund(refunds, pid, f"{key}-ref", ref_amount)
    refund_processor.handle(channel.emit_refund(rid, pid, ref_amount, "CNY"))
    return pid, rid


def _outcomes(service, refunds):
    return {**service.terminal_outcomes(), **refunds.terminal_outcomes()}


def test_refund_flows_matched_in_reconcile(service, refunds, channel, refund_processor, ledger):
    pid, rid = _settled_refund(service, refunds, channel, refund_processor, "rr-ok", 500, 200)
    report = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT, _outcomes(service, refunds))
    explain("rr-ok-ref", "MATCHED", f"matched={report.matched} diffs={report.diffs}")
    assert report.ok
    assert report.matched == sorted([pid, rid])


def test_refund_diff_types_classified(service, refunds, channel, refund_processor, ledger):
    from app.models import ChannelRecord

    # Matched refund.
    _, rid_ok = _settled_refund(service, refunds, channel, refund_processor, "rr-m", 500, 100)
    # MISSING_IN_CHANNEL: refund posted locally, channel record lost.
    pid_mc, rid_mc = _settled_refund(service, refunds, channel, refund_processor, "rr-mc", 500, 100)
    channel._records = [r for r in channel.statement() if r.refund_id != rid_mc]
    # MISSING_IN_LEDGER: channel settled a refund we never posted.
    channel._records.append(
        ChannelRecord(payment_id="pay_ghost", amount=50, record_id="rec_ghost", refund_id="ref_ghost")
    )
    # AMOUNT_MISMATCH: channel statement disagrees on the settled amount.
    _, rid_mm = _settled_refund(service, refunds, channel, refund_processor, "rr-mm", 500, 100)
    channel._records = [
        ChannelRecord(r.payment_id, r.amount + 1, r.record_id, r.refund_id) if r.refund_id == rid_mm else r
        for r in channel.statement()
    ]
    # CONTRADICTORY: local refund FAILED, channel statement shows it settled.
    pid_ct = _channel_settled_payment(service, channel, refund_processor, "rr-ct-pay", 500)
    rid_ct = _refund(refunds, pid_ct, "rr-ct-ref", 100)
    refund_processor.handle(channel.emit_refund(rid_ct, pid_ct, 100, "CNY", succeeded=False))
    channel._records.append(
        ChannelRecord(payment_id=pid_ct, amount=100, record_id="rec_ct", refund_id=rid_ct)
    )

    report = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT, _outcomes(service, refunds))
    by_type: dict = {}
    for d in report.diffs:
        by_type.setdefault(d.diff_type, []).append(d.refund_id or d.payment_id)
    explain("rr-*", "DIFFS", f"classified={ {k.value: v for k, v in by_type.items()} }")
    assert rid_ok in report.matched
    assert by_type[ReconDiffType.MISSING_IN_CHANNEL] == [rid_mc]
    assert by_type[ReconDiffType.MISSING_IN_LEDGER] == ["ref_ghost"]
    assert by_type[ReconDiffType.AMOUNT_MISMATCH] == [rid_mm]
    assert by_type[ReconDiffType.CONTRADICTORY] == [rid_ct]
    contradictory = [d for d in report.diffs if d.diff_type is ReconDiffType.CONTRADICTORY][0]
    assert contradictory.payment_id == pid_ct and contradictory.refund_id == rid_ct


def test_refund_diff_does_not_block_others_and_reproducible(
    service, refunds, channel, refund_processor, ledger
):
    _, rid_good = _settled_refund(service, refunds, channel, refund_processor, "rr-good", 500, 100)
    pid_bad = _channel_settled_payment(service, channel, refund_processor, "rr-bad-pay", 500)
    rid_bad = _refund(refunds, pid_bad, "rr-bad-ref", 100)  # never settled by channel
    refunds.apply_channel_result(rid_bad, True, 100, "CNY")  # local posting only

    first = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT, _outcomes(service, refunds))
    second = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT, _outcomes(service, refunds))
    sig = lambda r: [(d.diff_type.value, d.payment_id, d.refund_id, d.ledger_amount, d.channel_amount) for d in r.diffs]
    explain("rr-good-ref", "MATCHED", f"rid_bad diff isolated; reproducible={sig(first) == sig(second)}")
    assert rid_good in first.matched
    assert [d.refund_id for d in first.diffs] == [rid_bad]
    assert first.diffs[0].diff_type is ReconDiffType.MISSING_IN_CHANNEL
    assert sig(first) == sig(second)
    assert first.matched == second.matched
