"""Refund service: idempotent refund acceptance, double-entry posting and a
deterministic state machine.

Acceptance rules
----------------
- Only an already SUCCEEDED payment can be refunded (partial or full).
- Each refund has its own idempotency key; same key + same content (payment,
  amount, currency) replays the original result exactly once; same key with
  different content raises IdempotencyConflict.
- Cumulative *outstanding* refunds (CREATED reservations plus SUCCEEDED
  postings) must never exceed the original accepted amount; otherwise
  RefundAmountExceeded is raised. FAILED/EXPIRED refunds release capacity.
- Currency must match the accepted payment currency.

State machine (terminal: SUCCEEDED, FAILED, EXPIRED)

    CREATED --channel refund success--> SUCCEEDED   (posts balanced entries)
    CREATED --channel refund failure--> FAILED      (no entries)
    CREATED --timeout-----------------> EXPIRED     (no entries)

- Repeated same-outcome callbacks converge as a no-op.
- A contradicting outcome on a terminal refund is rejected, never applied.
- Callback amount/currency that disagree with acceptance are rejected with
  distinguishable error codes and post nothing.
- Funds move exactly once per refund, as a balanced reversal posting
  (debit merchant_revenue / credit channel_clearing); the ledger total
  always conserves value.
"""

from __future__ import annotations

import hashlib
import logging
import threading

from app.errors import (
    CallbackAmountMismatch,
    CallbackCurrencyMismatch,
    InvalidStateTransition,
    RefundAmountExceeded,
    RefundNotAllowed,
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

log = logging.getLogger("refunds")

TERMINAL_STATES = frozenset(
    {RefundStatus.SUCCEEDED, RefundStatus.FAILED, RefundStatus.EXPIRED}
)

# Refunds still consuming refund capacity: reserved at acceptance or settled.
_RESERVED_STATES = frozenset({RefundStatus.CREATED, RefundStatus.SUCCEEDED})


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

    # -- acceptance -------------------------------------------------------

    def create_refund(
        self, payment_id: str, idempotency_key: str, amount: int, currency: str
    ) -> dict:
        """Idempotently accept a refund against a succeeded payment."""
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
            # Another caller owns/owned this key with identical content.
            record.done.wait(timeout=30)
            if record.completed:
                return {**record.response, "replayed": True}
            # Owner abandoned the key after a failure; retry the claim.

    def _execute_create(
        self, payment_id: str, idempotency_key: str, amount: int, currency: str
    ) -> dict:
        refund_id = "ref_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]
        # The service-wide lock makes the capacity check + insert atomic, so
        # concurrent refunds can never together exceed the accepted amount.
        with self._lock:
            intent = self._payments.get(payment_id)
            if intent.status is not PaymentStatus.SUCCEEDED:
                raise RefundNotAllowed(
                    f"cannot refund payment {payment_id} in state "
                    f"{intent.status.value} (only SUCCEEDED payments are refundable)"
                )
            if currency != intent.currency:
                raise ValidationError(
                    f"refund currency {currency!r} does not match payment "
                    f"currency {intent.currency!r}"
                )
            reserved = sum(
                r.amount
                for r in self._refunds.values()
                if r.payment_id == payment_id and r.status in _RESERVED_STATES
            )
            if reserved + amount > intent.amount:
                log.info(
                    "refund rejected idempotency_key=%s payment=%s status=REJECTED "
                    "rationale=over_refund reserved=%d requested=%d accepted=%d",
                    idempotency_key, payment_id, reserved, amount, intent.amount,
                )
                raise RefundAmountExceeded(
                    f"refund of {amount} would exceed refundable amount for "
                    f"{payment_id}: already reserved {reserved} of {intent.amount}"
                )
            refund = Refund(
                refund_id=refund_id,
                payment_id=payment_id,
                idempotency_key=idempotency_key,
                amount=amount,
                currency=currency,
            )
            self._refunds[refund_id] = refund
        log.info(
            "refund accepted idempotency_key=%s refund_id=%s payment=%s "
            "status=CREATED amount=%d rationale=first_submission",
            idempotency_key, refund_id, payment_id, amount,
        )
        return self._to_response(refund, replayed=False)

    # -- state transitions ------------------------------------------------

    def apply_channel_result(
        self,
        refund_id: str,
        succeeded: bool,
        amount: int | None = None,
        currency: str | None = None,
    ) -> Refund:
        """Apply a channel refund callback outcome idempotently.

        Amount/currency disagreements are rejected before any state change or
        posting. Terminal states are sticky: a matching repeated outcome is a
        no-op, a contradicting one is rejected.
        """
        with self._lock:
            refund = self.get(refund_id)
            if amount is not None and amount != refund.amount:
                log.info(
                    "refund callback rejected refund_id=%s status=%s "
                    "rationale=amount_mismatch callback=%d accepted=%d",
                    refund_id, refund.status.value, amount, refund.amount,
                )
                raise CallbackAmountMismatch(
                    f"callback amount {amount} does not match refund {refund_id} "
                    f"amount {refund.amount}"
                )
            if currency is not None and currency != refund.currency:
                log.info(
                    "refund callback rejected refund_id=%s status=%s "
                    "rationale=currency_mismatch callback=%s accepted=%s",
                    refund_id, refund.status.value, currency, refund.currency,
                )
                raise CallbackCurrencyMismatch(
                    f"callback currency {currency!r} does not match refund "
                    f"{refund_id} currency {refund.currency!r}"
                )
            target = RefundStatus.SUCCEEDED if succeeded else RefundStatus.FAILED
            if refund.status is target:
                log.info(
                    "refund callback converged refund_id=%s status=%s "
                    "rationale=duplicate_outcome_noop",
                    refund_id, target.value,
                )
                return refund  # duplicate callback: no-op
            if refund.status in TERMINAL_STATES:
                log.info(
                    "refund callback rejected refund_id=%s status=%s "
                    "rationale=contradicts_terminal target=%s",
                    refund_id, refund.status.value, target.value,
                )
                raise InvalidStateTransition(
                    f"refund outcome {target.value} contradicts terminal state "
                    f"{refund.status.value} for {refund_id}"
                )
            self._require(refund, RefundStatus.CREATED, f"refund result {target.value}")
            if succeeded:
                self._post_refund_entries(refund)
            self._advance(refund, target)
            log.info(
                "refund callback applied refund_id=%s payment=%s status=%s "
                "rationale=channel_outcome amount=%d",
                refund_id, refund.payment_id, target.value, refund.amount,
            )
            return refund

    def expire_refund(self, refund_id: str) -> Refund:
        with self._lock:
            refund = self.get(refund_id)
            if refund.status is RefundStatus.EXPIRED:
                return refund  # repeated timeout converges safely
            self._require(refund, RefundStatus.CREATED, "expire refund")
            self._advance(refund, RefundStatus.EXPIRED)
            log.info(
                "refund expired refund_id=%s payment=%s status=EXPIRED "
                "rationale=timeout_no_entries",
                refund_id, refund.payment_id,
            )
            return refund

    # -- internals --------------------------------------------------------

    def _post_refund_entries(self, refund: Refund) -> None:
        self._ledger.post(
            [
                LedgerEntry(
                    entry_id=f"{refund.refund_id}:refund:debit",
                    payment_id=refund.payment_id,
                    account=REVENUE_ACCOUNT,
                    direction=EntryDirection.DEBIT,
                    amount=refund.amount,
                    memo=f"revenue reversal for refund {refund.refund_id}",
                    refund_id=refund.refund_id,
                ),
                LedgerEntry(
                    entry_id=f"{refund.refund_id}:refund:credit",
                    payment_id=refund.payment_id,
                    account=CLEARING_ACCOUNT,
                    direction=EntryDirection.CREDIT,
                    amount=refund.amount,
                    memo=f"clearing payout for refund {refund.refund_id}",
                    refund_id=refund.refund_id,
                ),
            ]
        )

    @staticmethod
    def _require(refund: Refund, expected: RefundStatus, action: str) -> None:
        if refund.status is not expected:
            raise InvalidStateTransition(
                f"cannot {action} refund {refund.refund_id} in state "
                f"{refund.status.value} (expected {expected.value})"
            )

    @staticmethod
    def _advance(refund: Refund, target: RefundStatus) -> None:
        refund.status = target
        refund.version += 1

    def get(self, refund_id: str) -> Refund:
        try:
            return self._refunds[refund_id]
        except KeyError:
            raise RefundNotFound(f"refund {refund_id!r} not found") from None

    def refunds_for(self, payment_id: str) -> list[Refund]:
        with self._lock:
            return sorted(
                (r for r in self._refunds.values() if r.payment_id == payment_id),
                key=lambda r: r.refund_id,
            )

    def all_refunds(self) -> list[Refund]:
        with self._lock:
            return sorted(self._refunds.values(), key=lambda r: r.refund_id)

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
