"""PaymentService: orchestrates idempotent acceptance, ledger posting,
status transitions and channel callback handling.

Invariants:
- A successful payment produces exactly one balanced ledger pair.
- Status only moves along the deterministic state machine; terminal states
  never change again, so duplicate/out-of-order/late callbacks are no-ops.
- Concurrent submissions of the same idempotency key cannot double-post:
  the key claim and the status transition are both serialized by locks.
"""
from __future__ import annotations

import threading
import uuid

from .channel import LocalChannel
from .errors import PaymentNotFoundError
from .idempotency import IdempotencyStore, fingerprint
from .ledger import Ledger
from .models import (
    ChannelEvent,
    ChannelEventType,
    PaymentIntent,
    PaymentStatus,
)
from .statemachine import check_transition


class PaymentService:
    def __init__(
        self, ledger: Ledger, idem: IdempotencyStore, channel: LocalChannel
    ) -> None:
        self.ledger = ledger
        self.idem = idem
        self.channel = channel
        self._intents: dict[str, PaymentIntent] = {}
        self._applied_event_ids: set[str] = set()
        self._lock = threading.Lock()

    # -- acceptance ------------------------------------------------------

    def create_payment(
        self, idempotency_key: str, amount: int, currency: str
    ) -> tuple[int, dict]:
        """Idempotently accept a payment request; replay returns same result."""
        fp = fingerprint({"amount": amount, "currency": currency})
        record, is_replay = self.idem.begin(idempotency_key, fp)
        if is_replay:
            body = dict(record.response_body)
            body["replayed"] = True
            return record.status_code, body
        try:
            intent = PaymentIntent(
                payment_id=f"pay_{uuid.uuid4().hex[:16]}",
                idempotency_key=idempotency_key,
                amount=amount,
                currency=currency,
            )
            with self._lock:
                self._intents[intent.payment_id] = intent
            body = self._render(intent)
            body["replayed"] = False
            self.idem.complete(idempotency_key, intent.payment_id, 201, body)
            return 201, body
        except Exception:
            self.idem.fail(idempotency_key)
            raise

    # -- state transitions -------------------------------------------------

    def mark_processing(self, payment_id: str) -> PaymentIntent:
        """Advance CREATED -> PROCESSING (the only way to enter PROCESSING)."""
        intent = self.get_payment(payment_id)
        with self._lock:
            check_transition(intent.status, PaymentStatus.PROCESSING)
            intent.status = PaymentStatus.PROCESSING
            intent.version += 1
        return intent

    def confirm(self, payment_id: str, succeed: bool = True) -> PaymentIntent:
        """Advance CREATED -> PROCESSING and run local channel processing."""
        intent = self.mark_processing(payment_id)
        event = self.channel.emit(
            payment_id,
            succeeded=succeed,
            amount=intent.amount,
            currency=intent.currency,
            reason=None if succeed else "channel_declined",
        )
        self.handle_callback(event)
        return intent

    def expire(self, payment_id: str) -> PaymentIntent:
        """Timeout path: safely converge to EXPIRED without partial records."""
        intent = self.get_payment(payment_id)
        with self._lock:
            check_transition(intent.status, PaymentStatus.EXPIRED)
            intent.status = PaymentStatus.EXPIRED
            intent.version += 1
        return intent

    # -- callbacks ---------------------------------------------------------

    def handle_callback(self, event: ChannelEvent) -> dict:
        """Idempotently apply a channel callback; terminal states are stable."""
        if event.event_id in self._applied_event_ids:
            return {
                "applied": False,
                "reason": "DUPLICATE_EVENT",
                "event_id": event.event_id,
            }
        intent = self._intents.get(event.payment_id)
        if intent is None:
            return {
                "applied": False,
                "reason": "UNKNOWN_PAYMENT",
                "event_id": event.event_id,
            }
        with self._lock:
            if intent.status in (
                PaymentStatus.SUCCEEDED,
                PaymentStatus.FAILED,
                PaymentStatus.EXPIRED,
            ):
                return {
                    "applied": False,
                    "reason": "ALREADY_TERMINAL",
                    "status": intent.status.value,
                    "event_id": event.event_id,
                }
            if event.amount != intent.amount or event.currency != intent.currency:
                return {
                    "applied": False,
                    "reason": "AMOUNT_MISMATCH",
                    "event_id": event.event_id,
                }
            target = (
                PaymentStatus.SUCCEEDED
                if event.event_type is ChannelEventType.SUCCEEDED
                else PaymentStatus.FAILED
            )
            check_transition(intent.status, target)
            if target is PaymentStatus.SUCCEEDED:
                # Ledger first: if posting raises, status stays PROCESSING.
                self.ledger.post_payment(intent.payment_id, intent.amount)
            else:
                intent.failure_reason = event.reason or "channel_declined"
            intent.status = target
            intent.version += 1
            self._applied_event_ids.add(event.event_id)
        return {
            "applied": True,
            "status": intent.status.value,
            "event_id": event.event_id,
        }

    # -- queries -----------------------------------------------------------

    def get_payment(self, payment_id: str) -> PaymentIntent:
        intent = self._intents.get(payment_id)
        if intent is None:
            raise PaymentNotFoundError(f"payment '{payment_id}' not found")
        return intent

    def list_payments(self) -> list[PaymentIntent]:
        return list(self._intents.values())

    @staticmethod
    def _render(intent: PaymentIntent) -> dict:
        return {
            "payment_id": intent.payment_id,
            "status": intent.status.value,
            "amount": intent.amount,
            "currency": intent.currency,
            "version": intent.version,
        }
