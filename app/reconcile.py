"""Reconciliation between the local ledger and the channel statement.

Payment flows are compared per payment id; refund flows per refund id. For
every flow seen on either side we compare the ledger's net posted amount
(on the clearing account) with the channel's settled amount, and classify
each difference:

- MISSING_IN_CHANNEL:   ledger posted, channel never settled
- MISSING_IN_LEDGER:    channel settled, ledger has no posting
- AMOUNT_MISMATCH:      both sides exist with different amounts
- DUPLICATE_IN_CHANNEL: channel settled the same flow more than once
- CONTRADICTORY:        both sides recorded the flow but reached opposite
                        conclusions (channel settled, local terminal state
                        is a failure) -- requires local terminal outcomes

Each flow is evaluated independently, so one difference never blocks the
processing of the others. Output is sorted, so reports are reproducible.
"""

from __future__ import annotations

from collections import Counter

from app.ledger import Ledger
from app.models import ChannelRecord, ReconDiff, ReconDiffType, ReconReport

_FAILURE_OUTCOMES = frozenset({"FAILED", "EXPIRED"})


def reconcile(
    ledger: Ledger,
    statement: list[ChannelRecord],
    account: str,
    local_outcomes: dict[str, str] | None = None,
) -> ReconReport:
    """Compare ledger postings with channel records; never raises on diffs.

    local_outcomes maps a flow id (payment_id or refund_id) to its local
    terminal status; when given, a channel settlement that contradicts a
    local failure conclusion is reported as CONTRADICTORY.
    """
    outcomes = local_outcomes or {}

    pay_ledger: dict[str, int] = {}
    ref_ledger: dict[str, int] = {}
    for entry in ledger.entries():
        if entry.account != account:
            continue
        signed = entry.amount if entry.direction.value == "CREDIT" else -entry.amount
        if entry.refund_id:
            ref_ledger[entry.refund_id] = ref_ledger.get(entry.refund_id, 0) + signed
        else:
            pay_ledger[entry.payment_id] = pay_ledger.get(entry.payment_id, 0) + signed
    pay_ledger = {k: abs(v) for k, v in pay_ledger.items() if v != 0}
    ref_ledger = {k: abs(v) for k, v in ref_ledger.items() if v != 0}

    pay_channel: dict[str, int] = {}
    ref_channel: dict[str, int] = {}
    pay_refund_of: dict[str, str] = {}
    pay_counts: Counter[str] = Counter()
    ref_counts: Counter[str] = Counter()
    for record in statement:
        if record.refund_id:
            ref_counts[record.refund_id] += 1
            ref_channel.setdefault(record.refund_id, record.amount)
            pay_refund_of.setdefault(record.refund_id, record.payment_id)
        else:
            pay_counts[record.payment_id] += 1
            pay_channel.setdefault(record.payment_id, record.amount)

    report = ReconReport()
    _compare_flows(
        report, pay_ledger, pay_channel, pay_counts,
        key_of=lambda pid: (pid, None), outcomes=outcomes,
    )
    _compare_flows(
        report, ref_ledger, ref_channel, ref_counts,
        key_of=lambda rid: (pay_refund_of.get(rid, ""), rid), outcomes=outcomes,
    )
    report.matched.sort()
    report.diffs.sort(
        key=lambda d: (d.payment_id, d.refund_id or "", d.diff_type.value)
    )
    return report


def _compare_flows(
    report: ReconReport,
    ledger_amounts: dict[str, int],
    channel_amounts: dict[str, int],
    channel_counts: Counter[str],
    key_of,
    outcomes: dict[str, str],
) -> None:
    for flow_id in sorted(set(ledger_amounts) | set(channel_amounts)):
        payment_id, refund_id = key_of(flow_id)
        in_ledger = flow_id in ledger_amounts
        in_channel = flow_id in channel_amounts
        if in_channel and outcomes.get(flow_id) in _FAILURE_OUTCOMES:
            report.diffs.append(
                ReconDiff(
                    diff_type=ReconDiffType.CONTRADICTORY,
                    payment_id=payment_id,
                    channel_amount=channel_amounts[flow_id],
                    detail=(
                        f"channel settled but local terminal outcome is "
                        f"{outcomes[flow_id]}"
                    ),
                    refund_id=refund_id,
                )
            )
        elif in_ledger and in_channel:
            if ledger_amounts[flow_id] == channel_amounts[flow_id]:
                report.matched.append(flow_id)
            else:
                report.diffs.append(
                    ReconDiff(
                        diff_type=ReconDiffType.AMOUNT_MISMATCH,
                        payment_id=payment_id,
                        ledger_amount=ledger_amounts[flow_id],
                        channel_amount=channel_amounts[flow_id],
                        detail="ledger and channel settled different amounts",
                        refund_id=refund_id,
                    )
                )
        elif in_ledger:
            report.diffs.append(
                ReconDiff(
                    diff_type=ReconDiffType.MISSING_IN_CHANNEL,
                    payment_id=payment_id,
                    ledger_amount=ledger_amounts[flow_id],
                    detail="ledger posted but channel never settled",
                    refund_id=refund_id,
                )
            )
        elif in_channel:
            report.diffs.append(
                ReconDiff(
                    diff_type=ReconDiffType.MISSING_IN_LEDGER,
                    payment_id=payment_id,
                    channel_amount=channel_amounts[flow_id],
                    detail="channel settled but ledger has no posting",
                    refund_id=refund_id,
                )
            )
        if channel_counts.get(flow_id, 0) > 1:
            report.diffs.append(
                ReconDiff(
                    diff_type=ReconDiffType.DUPLICATE_IN_CHANNEL,
                    payment_id=payment_id,
                    channel_amount=channel_amounts.get(flow_id),
                    detail=f"channel settled {channel_counts[flow_id]} times",
                    refund_id=refund_id,
                )
            )
