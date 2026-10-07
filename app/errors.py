"""Distinguishable failure reasons for the payment service (skeleton)."""

from __future__ import annotations


class PaymentError(Exception):
    """Base class for all payment errors."""

    code = "PAYMENT_ERROR"


class IdempotencyConflict(PaymentError):
    """Same idempotency key replayed with a different request body."""

    code = "IDEMPOTENCY_CONFLICT"


class InvalidStateTransition(PaymentError):
    """Illegal payment status transition."""

    code = "INVALID_STATE_TRANSITION"


class PaymentNotFound(PaymentError):
    code = "PAYMENT_NOT_FOUND"


class UnbalancedEntry(PaymentError):
    """Ledger posting that would break conservation of funds."""

    code = "UNBALANCED_ENTRY"


class ValidationError(PaymentError):
    """Malformed request content (e.g. non-positive amount)."""

    code = "VALIDATION_ERROR"


class RefundNotFound(PaymentError):
    code = "REFUND_NOT_FOUND"


class RefundExceedsPayment(PaymentError):
    """Cumulative refunds would exceed the original accepted amount."""

    code = "REFUND_EXCEEDS_PAYMENT"


class CallbackMismatch(PaymentError):
    """Callback declares an amount/currency that disagrees with acceptance."""

    code = "CALLBACK_MISMATCH"
