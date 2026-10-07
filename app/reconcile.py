"""Reconciliation between the local ledger and the channel statement.

Payments: for every payment id seen on either side we compare the ledger's
net posted amount (on the clearing account) with the channel's settled amount.

Refunds: refund records (``kind="refund"``) carry a ``refund_id`` and are
compared with the local RefundService outcomes. A refund that succeeded on
one side but failed/expired on the other is classified as the dedicated
CONTRADICTORY_RESULT difference (both sides have a record, opposite
conclusions) rather than a one-sided missing/amount diff.

Difference classes:

- MISSING_IN_CHANNEL:     ledger posted, channel never settled
- MISSING_IN_LEDGER:      channel settled, ledger has no posting
- AMOUNT_MISMATCH:        both sides exist with different amounts/currency
- DUPLICATE_IN_CHANNEL:   channel settled the same record more than once
- CONTRADICTORY_RESULT:   both sides have records but opposite outcomes

Each record is evaluated independently, so one difference never blocks the
processing of the others. Output is sorted, so reports are reproducible.
"""

from __future__ import annotations

from collections import Counter

from app.ledger import Ledger
from app.models import (
    ChannelRecord,
    ReconDiff,
    ReconDiffType,
    ReconReport,
    Refund,
    RefundStatus,
)


def reconcile(
    ledger: Ledger,
    statement: list[ChannelRecord],
    account: str,
    refunds: list[Refund] | None = None,
) -> ReconReport:
    """Compare ledger postings with channel records; never raises on diffs."""
    report = _reconcile_payments(ledger, statement, account)
    _reconcile_refunds(statement, refunds or [], report)
    report.matched.sort()
    return report


def _reconcile_payments(
    ledger: Ledger, statement: list[ChannelRecord], account: str
) -> ReconReport:
    """Compare ledger postings with channel records; never raises on diffs."""
    ledger_amounts: dict[str, int] = {}
    for entry in ledger.entries():
        if entry.account != account or entry.refund_id is not None:
            continue
        signed = entry.amount if entry.direction.value == "CREDIT" else -entry.amount
        ledger_amounts[entry.payment_id] = ledger_amounts.get(entry.payment_id, 0) + signed
    ledger_amounts = {pid: abs(v) for pid, v in ledger_amounts.items() if v != 0}

    payment_statement = [r for r in statement if r.kind != "refund"]
    channel_counts: Counter[str] = Counter(r.payment_id for r in payment_statement)
    channel_amounts: dict[str, int] = {}
    for record in payment_statement:
        channel_amounts.setdefault(record.payment_id, record.amount)

    report = ReconReport()
    for payment_id in sorted(set(ledger_amounts) | set(channel_amounts)):
        in_ledger = payment_id in ledger_amounts
        in_channel = payment_id in channel_amounts
        if in_ledger and in_channel:
            if ledger_amounts[payment_id] == channel_amounts[payment_id]:
                report.matched.append(payment_id)
            else:
                report.diffs.append(
                    ReconDiff(
                        diff_type=ReconDiffType.AMOUNT_MISMATCH,
                        payment_id=payment_id,
                        ledger_amount=ledger_amounts[payment_id],
                        channel_amount=channel_amounts[payment_id],
                        detail="ledger and channel settled different amounts",
                    )
                )
        elif in_ledger:
            report.diffs.append(
                ReconDiff(
                    diff_type=ReconDiffType.MISSING_IN_CHANNEL,
                    payment_id=payment_id,
                    ledger_amount=ledger_amounts[payment_id],
                    detail="ledger posted but channel never settled",
                )
            )
        else:
            report.diffs.append(
                ReconDiff(
                    diff_type=ReconDiffType.MISSING_IN_LEDGER,
                    payment_id=payment_id,
                    channel_amount=channel_amounts[payment_id],
                    detail="channel settled but ledger has no posting",
                )
            )
        if channel_counts.get(payment_id, 0) > 1:
            report.diffs.append(
                ReconDiff(
                    diff_type=ReconDiffType.DUPLICATE_IN_CHANNEL,
                    payment_id=payment_id,
                    channel_amount=channel_amounts.get(payment_id),
                    detail=f"channel settled {channel_counts[payment_id]} times",
                )
            )
    return report


def _reconcile_refunds(
    statement: list[ChannelRecord], local_refunds: list[Refund], report: ReconReport
) -> None:
    """Compare local refund outcomes with channel refund records."""
    refund_records = [r for r in statement if r.kind == "refund"]
    channel_counts: Counter[str] = Counter(
        r.refund_id for r in refund_records if r.refund_id
    )
    channel_first: dict[str, ChannelRecord] = {}
    for record in refund_records:
        if record.refund_id is not None:
            channel_first.setdefault(record.refund_id, record)

    local: dict[str, Refund] = {r.refund_id: r for r in local_refunds}
    # Locally a refund is only a settled fact once it reaches a terminal state;
    # CREATED refunds are still in flight.
    terminal_ids = {rid for rid, r in local.items() if r.status in (
        RefundStatus.SUCCEEDED, RefundStatus.FAILED, RefundStatus.EXPIRED
    )}

    for refund_id in sorted(set(terminal_ids) | set(channel_first)):
        local_refund = local.get(refund_id)
        channel_record = channel_first.get(refund_id)
        payment_id = (
            local_refund.payment_id if local_refund is not None
            else channel_record.payment_id
        )

        if channel_counts.get(refund_id, 0) > 1:
            report.diffs.append(
                ReconDiff(
                    diff_type=ReconDiffType.DUPLICATE_IN_CHANNEL,
                    payment_id=payment_id,
                    refund_id=refund_id,
                    channel_amount=channel_record.amount if channel_record else None,
                    detail=(
                        f"channel refunded {channel_counts[refund_id]} times"
                    ),
                )
            )

        local_succeeded = (
            local_refund is not None and local_refund.status is RefundStatus.SUCCEEDED
        )
        local_failed = (
            local_refund is not None
            and local_refund.status
            in (RefundStatus.FAILED, RefundStatus.EXPIRED)
        )

        if local_succeeded and channel_record is None:
            report.diffs.append(
                ReconDiff(
                    diff_type=ReconDiffType.MISSING_IN_CHANNEL,
                    payment_id=payment_id,
                    refund_id=refund_id,
                    ledger_amount=local_refund.amount,
                    detail="refund posted locally but channel has no refund record",
                )
            )
        elif local_failed and channel_record is not None:
            report.diffs.append(
                ReconDiff(
                    diff_type=ReconDiffType.CONTRADICTORY_RESULT,
                    payment_id=payment_id,
                    refund_id=refund_id,
                    ledger_amount=None,
                    channel_amount=channel_record.amount,
                    detail=(
                        f"local refund {local_refund.status.value} but channel "
                        "refund SUCCEEDED: opposite conclusions"
                    ),
                )
            )
        elif channel_record is not None and not local_succeeded:
            # Channel refunded but locally nothing settled: either unknown
            # locally or still/terminally non-successful without a local
            # failure conclusion matching a success.
            report.diffs.append(
                ReconDiff(
                    diff_type=ReconDiffType.MISSING_IN_LEDGER,
                    payment_id=payment_id,
                    refund_id=refund_id,
                    channel_amount=channel_record.amount,
                    detail="channel refunded but ledger has no refund posting",
                )
            )
        elif local_succeeded and channel_record is not None:
            currency_ok = (
                channel_record.currency is None
                or channel_record.currency == local_refund.currency
            )
            if channel_record.amount != local_refund.amount or not currency_ok:
                report.diffs.append(
                    ReconDiff(
                        diff_type=ReconDiffType.AMOUNT_MISMATCH,
                        payment_id=payment_id,
                        refund_id=refund_id,
                        ledger_amount=local_refund.amount,
                        channel_amount=channel_record.amount,
                        detail=(
                            "refund amounts/currencies differ: "
                            f"local={local_refund.amount} {local_refund.currency} "
                            f"channel={channel_record.amount} {channel_record.currency}"
                        ),
                    )
                )
            else:
                report.matched.append(refund_id)
