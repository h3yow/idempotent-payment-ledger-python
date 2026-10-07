"""Payment intent service: idempotent acceptance + deterministic state machine.

State machine (terminal states: SUCCEEDED, FAILED, EXPIRED):

    CREATED --confirm--> CONFIRMED --channel success--> SUCCEEDED   (posts entries)
    (CONFIRMED --channel failure--> FAILED, no entries)
    CREATED --expire---> EXPIRED                                    (no entries)

Rules:
- Illegal transitions raise InvalidStateTransition.
- Repeating an already-applied transition with the same outcome converges
  safely (returns current state, no side effects).
- A callback that contradicts a terminal state is rejected and never
  changes the terminal result.
- Funds move exactly once, only on the CONFIRMED -> SUCCEEDED transition,
  as a balanced double-entry posting.
"""

from __future__ import annotations

import hashlib
import threading

from app.errors import InvalidStateTransition, PaymentNotFound, ValidationError
from app.idempotency import IdempotencyStore
from app.ledger import Ledger
from app.models import EntryDirection, LedgerEntry, PaymentIntent, PaymentStatus

TERMINAL_STATES = frozenset(
    {PaymentStatus.SUCCEEDED, PaymentStatus.FAILED, PaymentStatus.EXPIRED}
)

CLEARING_ACCOUNT = "channel_clearing"
REVENUE_ACCOUNT = "merchant_revenue"


class PaymentService:
    def __init__(self, ledger: Ledger, idempotency: IdempotencyStore | None = None) -> None:
        self._ledger = ledger
        self._idem = idempotency or IdempotencyStore()
        self._lock = threading.Lock()
        self._intents: dict[str, PaymentIntent] = {}

    # -- acceptance ------------------------------------------------------

    def create_payment(self, idempotency_key: str, amount: int, currency: str) -> dict:
        """Idempotently accept a payment; replay returns the same result."""
        if not idempotency_key:
            raise ValidationError("idempotency_key must be non-empty")
        if not isinstance(amount, int) or amount <= 0:
            raise ValidationError(f"amount must be a positive integer, got {amount!r}")
        if not currency:
            raise ValidationError("currency must be non-empty")

        fingerprint = IdempotencyStore.fingerprint_of(
            {"amount": amount, "currency": currency}
        )
        while True:
            record, owner = self._idem.begin(idempotency_key, fingerprint)
            if owner:
                try:
                    response = self._execute_create(idempotency_key, amount, currency)
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

    def _execute_create(self, idempotency_key: str, amount: int, currency: str) -> dict:
        payment_id = "pay_" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]
        with self._lock:
            intent = PaymentIntent(
                payment_id=payment_id,
                idempotency_key=idempotency_key,
                amount=amount,
                currency=currency,
            )
            self._intents[payment_id] = intent
        return self._to_response(intent, replayed=False)

    # -- state transitions ------------------------------------------------

    def confirm_payment(self, payment_id: str) -> PaymentIntent:
        with self._lock:
            intent = self.get(payment_id)
            if intent.status is PaymentStatus.CONFIRMED:
                return intent  # repeated confirm converges safely
            self._require(intent, PaymentStatus.CREATED, "confirm")
            self._advance(intent, PaymentStatus.CONFIRMED)
            return intent

    def expire_payment(self, payment_id: str) -> PaymentIntent:
        with self._lock:
            intent = self.get(payment_id)
            if intent.status is PaymentStatus.EXPIRED:
                return intent  # repeated timeout converges safely
            self._require(intent, PaymentStatus.CREATED, "expire")
            self._advance(intent, PaymentStatus.EXPIRED)
            return intent

    def apply_channel_result(self, payment_id: str, succeeded: bool) -> PaymentIntent:
        """Apply a channel callback outcome idempotently.

        Terminal states are sticky: a matching repeated outcome is a no-op,
        a contradicting one is rejected, and neither changes the result.
        """
        with self._lock:
            intent = self.get(payment_id)
            target = PaymentStatus.SUCCEEDED if succeeded else PaymentStatus.FAILED
            if intent.status is target:
                return intent  # duplicate callback: no-op
            if intent.status in TERMINAL_STATES:
                raise InvalidStateTransition(
                    f"callback outcome {target.value} contradicts terminal "
                    f"state {intent.status.value} for {payment_id}"
                )
            self._require(intent, PaymentStatus.CONFIRMED, f"channel result {target.value}")
            if succeeded:
                self._post_success_entries(intent)
            self._advance(intent, target)
            return intent

    # -- internals ---------------------------------------------------------

    def _post_success_entries(self, intent: PaymentIntent) -> None:
        self._ledger.post(
            [
                LedgerEntry(
                    entry_id=f"{intent.payment_id}:settle:debit",
                    payment_id=intent.payment_id,
                    account=CLEARING_ACCOUNT,
                    direction=EntryDirection.DEBIT,
                    amount=intent.amount,
                    memo="channel settlement receivable",
                ),
                LedgerEntry(
                    entry_id=f"{intent.payment_id}:settle:credit",
                    payment_id=intent.payment_id,
                    account=REVENUE_ACCOUNT,
                    direction=EntryDirection.CREDIT,
                    amount=intent.amount,
                    memo="merchant revenue",
                ),
            ]
        )

    @staticmethod
    def _require(intent: PaymentIntent, expected: PaymentStatus, action: str) -> None:
        if intent.status is not expected:
            raise InvalidStateTransition(
                f"cannot {action} payment {intent.payment_id} in state "
                f"{intent.status.value} (expected {expected.value})"
            )

    @staticmethod
    def _advance(intent: PaymentIntent, target: PaymentStatus) -> None:
        intent.status = target
        intent.version += 1

    def get(self, payment_id: str) -> PaymentIntent:
        try:
            return self._intents[payment_id]
        except KeyError:
            raise PaymentNotFound(f"payment {payment_id!r} not found") from None

    def terminal_outcomes(self) -> dict[str, str]:
        """Map of payment_id -> terminal status value, for reconciliation."""
        with self._lock:
            return {
                p.payment_id: p.status.value
                for p in self._intents.values()
                if p.status in TERMINAL_STATES
            }

    @staticmethod
    def _to_response(intent: PaymentIntent, replayed: bool) -> dict:
        return {
            "payment_id": intent.payment_id,
            "idempotency_key": intent.idempotency_key,
            "amount": intent.amount,
            "currency": intent.currency,
            "status": intent.status.value,
            "replayed": replayed,
        }
