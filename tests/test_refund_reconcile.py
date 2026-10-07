"""Refund reconciliation: matched, one-sided, mismatch, duplicate, contradiction."""

from app.models import ReconDiffType
from app.payments import CLEARING_ACCOUNT
from app.reconcile import reconcile
from tests.conftest import explain


def _refund_flow(refunds, service, channel, processor, key, amount, *,
                 succeeded=True, channel_amount=None, currency="CNY",
                 emit_channel=True):
    pid = service.create_payment(key + "-pay", amount, currency)["payment_id"]
    service.confirm_payment(pid)
    payment_event = channel.emit(pid, amount, succeeded=True)
    processor.handle(payment_event)
    rid = refunds.create_refund(pid, key, amount, currency)["refund_id"]
    claimed = channel_amount if channel_amount is not None else amount
    event = channel.emit_refund(
        rid, pid, claimed, succeeded=succeeded, currency=currency
    )
    if emit_channel:
        if claimed != amount:
            # The statement claims a different amount, but the local callback
            # confirms the accepted amount, so the local refund succeeds.
            from app.models import ChannelEvent, ChannelEventType

            local_event = ChannelEvent(
                event_id=event.event_id + "-local",
                payment_id=pid,
                event_type=(
                    ChannelEventType.REFUND_SUCCEEDED
                    if succeeded
                    else ChannelEventType.REFUND_FAILED
                ),
                amount=amount,
                sequence=event.sequence,
                refund_id=rid,
                currency=currency,
            )
            processor.handle(local_event)
        else:
            processor.handle(event)
    return pid, rid


def test_refund_clean_matched(service, refunds, channel, processor, ledger):
    pid, rid = _refund_flow(refunds, service, channel, processor, "rfr-ok", 100)
    report = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT, refunds.all_refunds())
    explain("rfr-ok", "MATCHED", f"payment+refund matched={report.matched} diffs={report.diffs}")
    assert rid in report.matched and pid in report.matched
    assert report.ok


def test_refund_missing_in_channel(service, refunds, channel, processor, ledger):
    pid, rid = _refund_flow(
        refunds, service, channel, processor, "rfr-mc", 100, emit_channel=False
    )
    # Settle locally only (no channel refund record).
    refunds.apply_channel_result(rid, True)
    # Drop any stray refund records to simulate the channel losing them.
    channel._records = [r for r in channel.statement() if r.kind != "refund"]
    report = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT, refunds.all_refunds())
    diffs = [d for d in report.diffs if d.refund_id == rid]
    explain("rfr-mc", "DIFF", f"local refund success, no channel record => {[d.diff_type.value for d in diffs]}")
    assert [d.diff_type for d in diffs] == [ReconDiffType.MISSING_IN_CHANNEL]
    assert diffs[0].ledger_amount == 100


def test_refund_missing_in_ledger(service, refunds, channel, processor, ledger):
    # Channel-only refund record, unknown locally.
    channel.emit_refund("ref_ghost", "pay_ghost", 300, succeeded=True, currency="CNY")
    report = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT, refunds.all_refunds())
    diffs = [d for d in report.diffs if d.refund_id == "ref_ghost"]
    explain("ref_ghost", "DIFF", f"channel refund only => {[d.diff_type.value for d in diffs]}")
    assert [d.diff_type for d in diffs] == [ReconDiffType.MISSING_IN_LEDGER]
    assert diffs[0].channel_amount == 300


def test_refund_amount_mismatch(service, refunds, channel, processor, ledger):
    pid, rid = _refund_flow(
        refunds, service, channel, processor, "rfr-amt", 200, channel_amount=250
    )
    report = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT, refunds.all_refunds())
    diffs = [d for d in report.diffs if d.refund_id == rid]
    explain("rfr-amt", "DIFF", f"local 200 vs channel 250 => {[d.diff_type.value for d in diffs]}")
    assert [d.diff_type for d in diffs] == [ReconDiffType.AMOUNT_MISMATCH]
    assert diffs[0].ledger_amount == 200 and diffs[0].channel_amount == 250


def test_refund_currency_mismatch_in_reconcile(service, refunds, channel, processor, ledger):
    pid, rid = _refund_flow(
        refunds, service, channel, processor, "rfr-cur", 100
    )
    # Tamper the statement so the channel refund claims a different currency.
    channel._records = [
        _with_currency(r, "USD") if r.refund_id == rid else r
        for r in channel.statement()
    ]
    report = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT, refunds.all_refunds())
    diffs = [d for d in report.diffs if d.refund_id == rid]
    explain("rfr-cur", "DIFF", f"local CNY vs channel USD => {[d.diff_type.value for d in diffs]}")
    assert [d.diff_type for d in diffs] == [ReconDiffType.AMOUNT_MISMATCH]


def test_refund_duplicate_in_channel(service, refunds, channel, processor, ledger):
    pid, rid = _refund_flow(refunds, service, channel, processor, "rfr-dup", 100)
    channel.emit_refund(rid, pid, 100, succeeded=True, currency="CNY")  # duplicate
    report = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT, refunds.all_refunds())
    dup = [d for d in report.diffs if d.refund_id == rid and d.diff_type is ReconDiffType.DUPLICATE_IN_CHANNEL]
    explain("rfr-dup", "DIFF", f"channel refunded twice => duplicate count={len(dup)}")
    assert len(dup) == 1
    # The refund itself still matches on amounts (duplicate is a separate diff).
    assert rid in report.matched


def test_contradictory_result_local_failed_channel_succeeded(service, refunds, channel, processor, ledger):
    pid, rid = _refund_flow(
        refunds, service, channel, processor, "rfr-contra", 400, succeeded=False
    )
    assert refunds.get(rid).status.value == "FAILED"
    report = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT, refunds.all_refunds())
    diffs = [d for d in report.diffs if d.refund_id == rid]
    explain(
        "rfr-contra", "DIFF",
        f"local FAILED but channel SUCCEEDED => {[d.diff_type.value for d in diffs]}: {diffs[0].detail if diffs else ''}",
    )
    assert [d.diff_type for d in diffs] == [ReconDiffType.CONTRADICTORY_RESULT]
    assert diffs[0].ledger_amount is None and diffs[0].channel_amount == 400


def test_single_refund_diff_does_not_block_others(service, refunds, channel, processor, ledger):
    good_pid, good_rid = _refund_flow(
        refunds, service, channel, processor, "rfr-good", 100
    )
    bad_pid, bad_rid = _refund_flow(
        refunds, service, channel, processor, "rfr-bad", 200, succeeded=False
    )
    report = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT, refunds.all_refunds())
    explain(
        "rfr-good", "MIXED",
        f"good matched={good_rid in report.matched}; contradiction isolated={[d.diff_type.value for d in report.diffs if d.refund_id == bad_rid]}",
    )
    assert good_rid in report.matched
    assert any(
        d.refund_id == bad_rid and d.diff_type is ReconDiffType.CONTRADICTORY_RESULT
        for d in report.diffs
    )


def test_refund_reconcile_reproducible(service, refunds, channel, processor, ledger):
    _refund_flow(refunds, service, channel, processor, "rfr-rep-1", 100, channel_amount=101)
    _refund_flow(refunds, service, channel, processor, "rfr-rep-2", 200, succeeded=False)
    sig = lambda r: [
        (d.diff_type.value, d.payment_id, d.refund_id, d.ledger_amount, d.channel_amount)
        for d in r.diffs
    ]
    first = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT, refunds.all_refunds())
    second = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT, refunds.all_refunds())
    explain("rfr-rep-*", "REPRODUCIBLE", f"identical={sig(first) == sig(second)}")
    assert sig(first) == sig(second)
    assert first.matched == second.matched


def _with_currency(record, currency):
    import dataclasses

    return dataclasses.replace(record, currency=currency)
