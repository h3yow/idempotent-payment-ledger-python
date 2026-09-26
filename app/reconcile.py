"""Reconciliation between local ledger/intents and the channel statement.

Each payment is compared independently, so a single difference never blocks
the remaining entries. Differences are classified as:
- LOCAL_ONLY:      local intent exists but the channel has no record
- CHANNEL_ONLY:    channel recorded an event unknown locally
- AMOUNT_MISMATCH: both sides recorded the payment with different amounts
- DUPLICATE:       duplicated records on either side
- STATUS_MISMATCH: both sides recorded it but the outcomes disagree

The result is deterministic (sorted) so conclusions are reproducible.
"""
from __future__ import annotations

from collections import Counter

from .models import (
    ChannelEvent,
    ChannelEventType,
    DiffKind,
    PaymentIntent,
    PaymentStatus,
    ReconDiff,
)


def reconcile(
    intents: list[PaymentIntent], channel_events: list[ChannelEvent]
) -> list[ReconDiff]:
    diffs: list[ReconDiff] = []

    local_by_pid: dict[str, list[PaymentIntent]] = {}
    for intent in intents:
        local_by_pid.setdefault(intent.payment_id, []).append(intent)

    # Channel side: one logical record per payment = its final event.
    channel_final: dict[str, ChannelEvent] = {}
    channel_counts: Counter[str] = Counter()
    for ev in channel_events:
        channel_counts[ev.payment_id] += 1
        channel_final[ev.payment_id] = ev  # last write wins = final state

    # Duplicates on either side.
    for pid, group in sorted(local_by_pid.items()):
        if len(group) > 1:
            diffs.append(
                ReconDiff(DiffKind.DUPLICATE, pid, f"{len(group)} local intents")
            )
    for pid, count in sorted(channel_counts.items()):
        if count > 1:
            diffs.append(
                ReconDiff(DiffKind.DUPLICATE, pid, f"{count} channel events")
            )

    for pid in sorted(set(local_by_pid) | set(channel_final)):
        local = local_by_pid.get(pid, [None])[0]
        remote = channel_final.get(pid)
        if local is None:
            diffs.append(
                ReconDiff(
                    DiffKind.CHANNEL_ONLY,
                    pid,
                    "channel event without local intent",
                    channel_amount=remote.amount,
                )
            )
            continue
        if remote is None:
            diffs.append(
                ReconDiff(
                    DiffKind.LOCAL_ONLY,
                    pid,
                    f"local intent {local.status.value} missing on channel",
                    local_amount=local.amount,
                )
            )
            continue
        if local.amount != remote.amount:
            diffs.append(
                ReconDiff(
                    DiffKind.AMOUNT_MISMATCH,
                    pid,
                    f"local {local.amount} != channel {remote.amount}",
                    local_amount=local.amount,
                    channel_amount=remote.amount,
                )
            )
        local_ok = local.status is PaymentStatus.SUCCEEDED
        remote_ok = remote.event_type is ChannelEventType.SUCCEEDED
        if local_ok != remote_ok:
            diffs.append(
                ReconDiff(
                    DiffKind.STATUS_MISMATCH,
                    pid,
                    f"local {local.status.value} vs channel {remote.event_type.value}",
                    local_amount=local.amount,
                    channel_amount=remote.amount,
                )
            )
    return diffs
