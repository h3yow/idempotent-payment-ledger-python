"""Local simulated channel and idempotent callback processing.

The channel emits signed-settlement events (payments and refunds) and keeps
a statement of settled records. It can produce duplicate, out-of-order and
late callbacks so the receiver side can be tested for idempotency and
terminal-state stickiness. Everything is in-memory: no network, no
credentials, no external services.
"""

from __future__ import annotations

import random
import threading

from app.errors import PaymentError
from app.models import ChannelEvent, ChannelEventType, ChannelRecord, PaymentStatus
from app.payments import PaymentService
from app.refunds import RefundService


class SimulatedChannel:
    """In-memory channel that records settlements and emits events."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[ChannelEvent] = []
        self._records: list[ChannelRecord] = []
        self._sequence = 0

    def emit(self, payment_id: str, amount: int, succeeded: bool = True) -> ChannelEvent:
        """Emit a payment settlement event; successes land on the statement."""
        with self._lock:
            self._sequence += 1
            event = ChannelEvent(
                event_id=f"evt_{self._sequence:06d}",
                payment_id=payment_id,
                event_type=ChannelEventType.SUCCEEDED if succeeded else ChannelEventType.FAILED,
                amount=amount,
                sequence=self._sequence,
            )
            self._events.append(event)
            if succeeded:
                self._records.append(
                    ChannelRecord(
                        payment_id=payment_id,
                        amount=amount,
                        record_id=f"rec_{self._sequence:06d}",
                    )
                )
            return event

    def emit_refund(
        self,
        refund_id: str,
        payment_id: str,
        amount: int,
        currency: str = "",
        succeeded: bool = True,
    ) -> ChannelEvent:
        """Emit a refund settlement event; successes land on the statement."""
        with self._lock:
            self._sequence += 1
            event = ChannelEvent(
                event_id=f"evt_{self._sequence:06d}",
                payment_id=payment_id,
                event_type=ChannelEventType.SUCCEEDED if succeeded else ChannelEventType.FAILED,
                amount=amount,
                sequence=self._sequence,
                refund_id=refund_id,
                currency=currency,
            )
            self._events.append(event)
            if succeeded:
                self._records.append(
                    ChannelRecord(
                        payment_id=payment_id,
                        amount=amount,
                        record_id=f"rec_{self._sequence:06d}",
                        refund_id=refund_id,
                    )
                )
            return event

    def duplicate(self, event: ChannelEvent) -> ChannelEvent:
        """Re-emit the same event (duplicate callback delivery)."""
        with self._lock:
            self._events.append(event)
            return event

    def shuffled_events(self, seed: int = 0) -> list[ChannelEvent]:
        """Return emitted events in a deterministic shuffled (out-of-order) order."""
        events = list(self._events)
        random.Random(seed).shuffle(events)
        return events

    def events(self) -> list[ChannelEvent]:
        with self._lock:
            return list(self._events)

    def statement(self) -> list[ChannelRecord]:
        with self._lock:
            return list(self._records)


class CallbackProcessor:
    """Applies channel events to the payment/refund services idempotently.

    - Duplicate event ids are discarded before touching the services.
    - Events for flows already in a terminal state never change the result;
      contradicting ones are reported, not applied.
    - A callback whose amount/currency disagrees with acceptance is
      rejected with a distinguishable reason code (CALLBACK_MISMATCH).
    - Every event is processed independently: one bad event never blocks
      the rest.
    """

    def __init__(
        self, service: PaymentService, refunds: RefundService | None = None
    ) -> None:
        self._service = service
        self._refunds = refunds
        self._lock = threading.Lock()
        self._seen_event_ids: set[str] = set()

    def handle(self, event: ChannelEvent) -> dict:
        with self._lock:
            if event.event_id in self._seen_event_ids:
                return {
                    "event_id": event.event_id,
                    "payment_id": event.payment_id,
                    "refund_id": event.refund_id,
                    "action": "duplicate_ignored",
                    "status": self._safe_status(event),
                }
            self._seen_event_ids.add(event.event_id)

        succeeded = event.event_type is ChannelEventType.SUCCEEDED
        try:
            status = self._apply(event, succeeded)
            return {
                "event_id": event.event_id,
                "payment_id": event.payment_id,
                "refund_id": event.refund_id,
                "action": "applied",
                "status": status,
            }
        except PaymentError as exc:
            return {
                "event_id": event.event_id,
                "payment_id": event.payment_id,
                "refund_id": event.refund_id,
                "action": "rejected",
                "reason": str(exc),
                "reason_code": exc.code,
                "status": self._safe_status(event),
            }

    def _apply(self, event: ChannelEvent, succeeded: bool) -> str:
        if event.refund_id:
            if self._refunds is None:
                raise PaymentError("refund callbacks are not supported here")
            refund = self._refunds.apply_channel_result(
                event.refund_id, succeeded, event.amount, event.currency
            )
            return refund.status.value
        intent = self._service.apply_channel_result(event.payment_id, succeeded)
        return intent.status.value

    def handle_many(self, events: list[ChannelEvent]) -> list[dict]:
        return [self.handle(e) for e in events]

    def _safe_status(self, event: ChannelEvent) -> str:
        try:
            if event.refund_id and self._refunds is not None:
                return self._refunds.get(event.refund_id).status.value
            return self._service.get(event.payment_id).status.value
        except Exception:
            return PaymentStatus.CREATED.value
