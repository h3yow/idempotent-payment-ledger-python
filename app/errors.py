"""Distinguishable failure reasons for the payment service. (Skeleton.)"""
from __future__ import annotations


class PaymentError(Exception):
    """Base class; `code` lets callers distinguish failure reasons."""

    code = "PAYMENT_ERROR"
    http_status = 400

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message


class IdempotencyConflictError(PaymentError):
    """Same idempotency key reused with a different request payload."""

    code = "IDEMPOTENCY_CONFLICT"
    http_status = 409


class IdempotencyInProgressError(PaymentError):
    """Same key is currently being processed by another request."""

    code = "IDEMPOTENCY_IN_PROGRESS"
    http_status = 409


class InvalidStateTransitionError(PaymentError):
    """Illegal payment status transition (incl. duplicate confirm)."""

    code = "INVALID_STATE_TRANSITION"
    http_status = 409


class PaymentNotFoundError(PaymentError):
    code = "PAYMENT_NOT_FOUND"
    http_status = 404


class PaymentExpiredError(PaymentError):
    code = "PAYMENT_EXPIRED"
    http_status = 410


class LedgerInvariantError(PaymentError):
    """Double-entry invariant violated; must never leave partial records."""

    code = "LEDGER_INVARIANT_VIOLATION"
    http_status = 500
