"""Reconciliation: diff classification, isolation and reproducibility."""

from app.models import ReconDiffType
from app.payments import CLEARING_ACCOUNT
from app.reconcile import reconcile
from tests.conftest import explain


def _settled(service, channel, processor, key, amount, channel_amount=None):
    pid = service.create_payment(key, amount, "CNY")["payment_id"]
    service.confirm_payment(pid)
    event = channel.emit(pid, channel_amount if channel_amount is not None else amount)
    processor.handle(event)
    return pid


def test_clean_books_all_matched(service, channel, processor, ledger):
    pids = [_settled(service, channel, processor, f"rc-ok-{i}", 100 + i) for i in range(3)]
    report = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT)
    explain("rc-ok-*", "MATCHED", f"matched={report.matched} diffs={report.diffs}")
    assert report.ok
    assert report.matched == sorted(pids)


def test_all_diff_types_classified(service, channel, processor, ledger):
    matched = _settled(service, channel, processor, "rc-matched", 100)
    mismatch = _settled(service, channel, processor, "rc-mismatch", 200, channel_amount=250)
    missing_channel = _settled(service, channel, processor, "rc-mc", 300)
    # Remove the channel record to simulate a settlement the channel lost.
    channel._records = [r for r in channel.statement() if r.payment_id != missing_channel]
    # Channel-only record (never posted locally) + duplicate settlement.
    channel.emit("pay_ghost", 400)
    channel.emit("pay_ghost", 400)

    report = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT)
    by_type = {}
    for d in report.diffs:
        by_type.setdefault(d.diff_type, []).append(d.payment_id)
    explain("rc-*", "DIFFS", f"classified={ {k.value: v for k, v in by_type.items()} }")
    assert report.matched == [matched]
    assert by_type[ReconDiffType.AMOUNT_MISMATCH] == [mismatch]
    assert by_type[ReconDiffType.MISSING_IN_CHANNEL] == [missing_channel]
    assert by_type[ReconDiffType.MISSING_IN_LEDGER] == ["pay_ghost"]
    assert by_type[ReconDiffType.DUPLICATE_IN_CHANNEL] == ["pay_ghost"]


def test_single_diff_does_not_block_others(service, channel, processor, ledger):
    good = _settled(service, channel, processor, "rc-good", 100)
    bad = _settled(service, channel, processor, "rc-bad", 200, channel_amount=201)
    report = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT)
    explain("rc-good", "MATCHED", "amount mismatch on rc-bad did not block rc-good")
    assert good in report.matched
    assert [d.payment_id for d in report.diffs] == [bad]


def test_reconciliation_is_reproducible(service, channel, processor, ledger):
    _settled(service, channel, processor, "rc-rep-1", 100, channel_amount=101)
    _settled(service, channel, processor, "rc-rep-2", 200)
    channel.emit("pay_ghost", 50)
    first = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT)
    second = reconcile(ledger, channel.statement(), CLEARING_ACCOUNT)
    sig = lambda r: [(d.diff_type.value, d.payment_id, d.ledger_amount, d.channel_amount) for d in r.diffs]
    explain("rc-rep-*", "REPRODUCIBLE", f"two runs identical={sig(first) == sig(second)}")
    assert sig(first) == sig(second)
    assert first.matched == second.matched
