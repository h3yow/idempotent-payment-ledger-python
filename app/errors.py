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


class RefundNotAllowed(PaymentError):
    """Refund requested against a payment that never succeeded."""

    code = "REFUND_NOT_ALLOWED"


class RefundAmountExceeded(PaymentError):
    """Cumulative refunds would exceed the original accepted amount."""

    code = "REFUND_AMOUNT_EXCEEDED"


class RefundNotFound(PaymentError):
    code = "REFUND_NOT_FOUND"


class CallbackAmountMismatch(PaymentError):
    """Callback claims an amount different from the accepted record."""

    code = "CALLBACK_AMOUNT_MISMATCH"


class CallbackCurrencyMismatch(PaymentError):
    """Callback claims a currency different from the accepted record."""

    code = "CALLBACK_CURRENCY_MISMATCH"
