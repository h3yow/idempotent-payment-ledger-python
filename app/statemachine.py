"""Deterministic payment status transitions.

A payment intent may only move forward along ALLOWED_TRANSITIONS. Terminal
states (SUCCEEDED / FAILED / EXPIRED) have no outgoing edges, so duplicate
confirms, late retries and out-of-order callbacks are rejected instead of
corrupting the ledger.
"""
from __future__ import annotations

from .errors import InvalidStateTransitionError
from .models import PaymentStatus

ALLOWED_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.CREATED: frozenset({PaymentStatus.PROCESSING, PaymentStatus.EXPIRED}),
    PaymentStatus.PROCESSING: frozenset(
        {PaymentStatus.SUCCEEDED, PaymentStatus.FAILED, PaymentStatus.EXPIRED}
    ),
    PaymentStatus.SUCCEEDED: frozenset(),
    PaymentStatus.FAILED: frozenset(),
    PaymentStatus.EXPIRED: frozenset(),
}

TERMINAL_STATES: frozenset[PaymentStatus] = frozenset(
    {PaymentStatus.SUCCEEDED, PaymentStatus.FAILED, PaymentStatus.EXPIRED}
)


def check_transition(current: PaymentStatus, target: PaymentStatus) -> None:
    """Raise InvalidStateTransitionError unless current -> target is allowed."""
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidStateTransitionError(
            f"illegal transition {current.value} -> {target.value}"
        )
