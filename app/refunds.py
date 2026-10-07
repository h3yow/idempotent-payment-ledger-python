"""Refund service: idempotent acceptance + deterministic state machine.

State machine (terminal states: SUCCEEDED, FAILED):

    CREATED --channel success--> SUCCEEDED   (posts reversing entries)
    CREATED --channel failure--> FAILED      (no entries)

Rules:
- Refunds are accepted only against SUCCEEDED payments, in the payment's
  currency; cumulative non-failed refunds never exceed the payment amount
  (over-refunds raise RefundExceedsPayment, a distinguishable rejection).
- Idempotent by key: same key + same content replays the stored response;
  same key + different content raises IdempotencyConflict.
- Illegal transitions raise InvalidStateTransition; repeating an applied
  outcome converges safely (no-op); terminal states are sticky.
- Funds move exactly once, only on CREATED -> SUCCEEDED, as a balanced
  double-entry posting (debit merchant_revenue / credit channel_clearing),
  so the ledger always conserves value and never holds half-posted refunds.
"""

from __future__ import annotations

import hashlib
import threading

from app.errors import (
    CallbackMismatch,
    InvalidStateTransition,
    RefundExceedsPayment,
    RefundNotFound,
    ValidationError,
)
from app.idempotency import IdempotencyStore
from app.ledger import Ledger
from app.models import (
    EntryDirection,
    LedgerEntry,
    PaymentStatus,
    Refund,
    RefundStatus,
)
from app.payments import CLEARING_ACCOUNT, REVENUE_ACCOUNT, PaymentService

REFUND_TERMINAL_STATES = frozenset({RefundStatus.SUCCEEDED, RefundStatus.FAILED})


class RefundService:
    def __init__(
        self,
        payments: PaymentService,
        ledger: Ledger,
        idempotency: IdempotencyStore | None = None,
    ) -> None:
        self._payments = payments
        self._ledger = ledger
        self._idem = idempotency or IdempotencyStore()
        self._lock = threading.Lock()
        self._refunds: dict[str, Refund] = {}

    # -- acceptance ------------------------------------------------------

    def create_refund(
        self, payment_id: str, idempotency_key: str, amount: int, currency: str
    ) -> dict:
        """Idempotently accept a refund; replay returns the same result."""
        if not idempotency_key:
            raise ValidationError("idempotency_key must be non-empty")
        if not isinstance(amount, int) or amount <= 0:
            raise ValidationError(f"amount must be a positive integer, got {amount!r}")
        if not currency:
            raise ValidationError("currency must be non-empty")

        fingerprint = IdempotencyStore.fingerprint_of(
            {"payment_id": payment_id, "amount": amount, "currency": currency}
        )
        while True:
            record, owner = self._idem.begin(idempotency_key, fingerprint)
            if owner:
                try:
                    response = self._execute_create(
                        payment_id, idempotency_key, amount, currency
                    )
                except Exception:
                    self._idem.abandon(idempotency_key)
                    raise
                self._idem.complete(idempotency_key, response)
                return response
            record.done.wait(timeout=30)
            if record.completed:
                return {**record.response, "replayed": True}
            # Owner abandoned the key after a failure; retry the claim.

    def _execute_create(
        self, payment_id: str, idempotency_key: str, amount: int, currency: str
    ) -> dict:
        with self._lock:
            payment = self._payments.get(payment_id)
            if payment.status is not PaymentStatus.SUCCEEDED:
                raise InvalidStateTransition(
                    f"cannot refund payment {payment_id} in state "
                    f"{payment.status.value} (expected SUCCEEDED)"
                )
            if currency != payment.currency:
                raise ValidationError(
                    f"refund currency {currency!r} does not match payment "
                    f"currency {payment.currency!r}"
                )
            cumulative = self._refunded_amount_locked(payment_id)
            if cumulative + amount > payment.amount:
                raise RefundExceedsPayment(
                    f"refund {amount} would bring cumulative refunds to "
                    f"{cumulative + amount}, exceeding payment amount "
                    f"{payment.amount} for {payment_id}"
                )
            refund_id = "ref_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]
            refund = Refund(
                refund_id=refund_id,
                payment_id=payment_id,
                idempotency_key=idempotency_key,
                amount=amount,
                currency=currency,
            )
            self._refunds[refund_id] = refund
        return self._to_response(refund, replayed=False)

    # -- state transitions ------------------------------------------------

    def apply_channel_result(
        self, refund_id: str, succeeded: bool, amount: int, currency: str
    ) -> Refund:
        """Apply a refund callback outcome idempotently.

        A callback whose amount/currency disagrees with acceptance is
        rejected as CallbackMismatch; terminal states are sticky.
        """
        with self._lock:
            refund = self.get(refund_id)
            if amount != refund.amount or (currency and currency != refund.currency):
                raise CallbackMismatch(
                    f"callback for refund {refund_id} declares "
                    f"amount={amount} currency={currency!r}, accepted was "
                    f"amount={refund.amount} currency={refund.currency!r}"
                )
            target = RefundStatus.SUCCEEDED if succeeded else RefundStatus.FAILED
            if refund.status is target:
                return refund  # duplicate callback: no-op
            if refund.status in REFUND_TERMINAL_STATES:
                raise InvalidStateTransition(
                    f"callback outcome {target.value} contradicts terminal "
                    f"state {refund.status.value} for refund {refund_id}"
                )
            # CREATED is the only remaining state.
            if succeeded:
                self._post_refund_entries(refund)
            refund.status = target
            refund.version += 1
            return refund

    # -- internals ---------------------------------------------------------

    def _post_refund_entries(self, refund: Refund) -> None:
        self._ledger.post(
            [
                LedgerEntry(
                    entry_id=f"{refund.refund_id}:refund:debit",
                    payment_id=refund.payment_id,
                    account=REVENUE_ACCOUNT,
                    direction=EntryDirection.DEBIT,
                    amount=refund.amount,
                    memo="refund reverses merchant revenue",
                    refund_id=refund.refund_id,
                ),
                LedgerEntry(
                    entry_id=f"{refund.refund_id}:refund:credit",
                    payment_id=refund.payment_id,
                    account=CLEARING_ACCOUNT,
                    direction=EntryDirection.CREDIT,
                    amount=refund.amount,
                    memo="refund returns funds to channel",
                    refund_id=refund.refund_id,
                ),
            ]
        )

    # -- queries -----------------------------------------------------------

    def get(self, refund_id: str) -> Refund:
        try:
            return self._refunds[refund_id]
        except KeyError:
            raise RefundNotFound(f"refund {refund_id!r} not found") from None

    def refunded_amount(self, payment_id: str) -> int:
        """Cumulative amount of non-failed refunds for a payment."""
        with self._lock:
            return self._refunded_amount_locked(payment_id)

    def _refunded_amount_locked(self, payment_id: str) -> int:
        return sum(
            r.amount
            for r in self._refunds.values()
            if r.payment_id == payment_id and r.status is not RefundStatus.FAILED
        )

    def terminal_outcomes(self) -> dict[str, str]:
        """Map of refund_id -> terminal status value, for reconciliation."""
        with self._lock:
            return {
                r.refund_id: r.status.value
                for r in self._refunds.values()
                if r.status in REFUND_TERMINAL_STATES
            }

    @staticmethod
    def _to_response(refund: Refund, replayed: bool) -> dict:
        return {
            "refund_id": refund.refund_id,
            "payment_id": refund.payment_id,
            "idempotency_key": refund.idempotency_key,
            "amount": refund.amount,
            "currency": refund.currency,
            "status": refund.status.value,
            "replayed": replayed,
        }
