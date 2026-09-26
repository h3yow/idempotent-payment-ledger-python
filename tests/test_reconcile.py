"""Reconciliation differences: single-side records, amount mismatch,
missing and duplicates; one diff must not block the others."""
import logging

from app.models import (
    ChannelEvent,
    ChannelEventType,
    PaymentIntent,
    PaymentStatus,
)
from app.reconcile import DiffKind, reconcile

log = logging.getLogger("test.reconcile")


def test_reconcile_classifies_all_difference_kinds():
    # p1 matches; p2 local-only (missing on channel); p3 channel-only;
    # p4 amount mismatch; p5 status mismatch; p6 duplicated channel events.
    intents = [
        PaymentIntent("p1", "k1", 100, "CNY", PaymentStatus.SUCCEEDED),
        PaymentIntent("p2", "k2", 200, "CNY", PaymentStatus.PROCESSING),
        PaymentIntent("p4", "k4", 400, "CNY", PaymentStatus.SUCCEEDED),
        PaymentIntent("p5", "k5", 500, "CNY", PaymentStatus.FAILED),
        PaymentIntent("p6", "k6", 600, "CNY", PaymentStatus.SUCCEEDED),
    ]
    events = [
        ChannelEvent("e1", "p1", ChannelEventType.SUCCEEDED, 100, "CNY"),
        ChannelEvent("e3", "p3", ChannelEventType.SUCCEEDED, 300, "CNY"),
        ChannelEvent("e4", "p4", ChannelEventType.SUCCEEDED, 401, "CNY"),
        ChannelEvent("e5", "p5", ChannelEventType.SUCCEEDED, 500, "CNY"),
        ChannelEvent("e6a", "p6", ChannelEventType.SUCCEEDED, 600, "CNY"),
        ChannelEvent("e6b", "p6", ChannelEventType.SUCCEEDED, 600, "CNY"),
    ]

    diffs = reconcile(intents, events)
    by_pid = {}
    for d in diffs:
        by_pid.setdefault(d.payment_id, []).append(d.kind)

    for d in diffs:
        log.info(
            "payment=%s kind=%s detail=%s basis=independent per-payment comparison",
            d.payment_id, d.kind.value, d.detail,
        )

    assert "p1" not in by_pid  # clean
    assert by_pid["p2"] == [DiffKind.LOCAL_ONLY]
    assert by_pid["p3"] == [DiffKind.CHANNEL_ONLY]
    assert by_pid["p4"] == [DiffKind.AMOUNT_MISMATCH]
    assert by_pid["p5"] == [DiffKind.STATUS_MISMATCH]
    assert by_pid["p6"] == [DiffKind.DUPLICATE]
    # All five problematic payments reported despite each difference:
    assert set(by_pid) == {"p2", "p3", "p4", "p5", "p6"}


def test_reconcile_result_is_reproducible():
    intents = [PaymentIntent("a", "k", 10, "CNY", PaymentStatus.SUCCEEDED)]
    events = [
        ChannelEvent("z", "b", ChannelEventType.FAILED, 20, "CNY"),
        ChannelEvent("y", "c", ChannelEventType.SUCCEEDED, 30, "CNY"),
    ]
    first = reconcile(intents, list(reversed(events)))
    second = reconcile(intents, events)
    assert [(d.kind, d.payment_id, d.detail) for d in first] == [
        (d.kind, d.payment_id, d.detail) for d in second
    ]
    log.info("basis=sorted deterministic output, diffs=%d", len(first))
