"""Reconciliation between the local ledger and the channel statement.

For every payment id seen on either side we compare the ledger's net posted
amount (on the clearing account) with the channel's settled amount, and
classify each difference:

- MISSING_IN_CHANNEL:   ledger posted, channel never settled
- MISSING_IN_LEDGER:    channel settled, ledger has no posting
- AMOUNT_MISMATCH:      both sides exist with different amounts
- DUPLICATE_IN_CHANNEL: channel settled the same payment more than once

Each payment is evaluated independently, so one difference never blocks the
processing of the others. Output is sorted, so reports are reproducible.
"""

from __future__ import annotations

from collections import Counter

from app.ledger import Ledger
from app.models import ChannelRecord, ReconDiff, ReconDiffType, ReconReport


def reconcile(ledger: Ledger, statement: list[ChannelRecord], account: str) -> ReconReport:
    """Compare ledger postings with channel records; never raises on diffs."""
    ledger_amounts: dict[str, int] = {}
    for entry in ledger.entries():
        if entry.account != account:
            continue
        signed = entry.amount if entry.direction.value == "CREDIT" else -entry.amount
        ledger_amounts[entry.payment_id] = ledger_amounts.get(entry.payment_id, 0) + signed
    ledger_amounts = {pid: abs(v) for pid, v in ledger_amounts.items() if v != 0}

    channel_counts: Counter[str] = Counter(r.payment_id for r in statement)
    channel_amounts: dict[str, int] = {}
    for record in statement:
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
    report.matched.sort()
    return report
