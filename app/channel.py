"""Local simulated channel and idempotent callback processing.

The channel emits signed-settlement events and keeps a statement of settled
records. It can produce duplicate, out-of-order and late callbacks so the
receiver side can be tested for idempotency and terminal-state stickiness.
Everything is in-memory: no network, no credentials, no external services.
"""

from __future__ import annotations

import random
import threading

from app.errors import PaymentError
from app.models import ChannelEvent, ChannelEventType, ChannelRecord, PaymentStatus
from app.payments import PaymentService


class SimulatedChannel:
    """In-memory channel that records settlements and emits events."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[ChannelEvent] = []
        self._records: list[ChannelRecord] = []
        self._sequence = 0

    def emit(self, payment_id: str, amount: int, succeeded: bool = True) -> ChannelEvent:
        """Emit a settlement event; successful ones land on the statement."""
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
    """Applies channel events to the payment service idempotently.

    - Duplicate event ids are discarded before touching the service.
    - Events for payments already in a terminal state never change the
      result; contradicting ones are reported, not applied.
    - Every event is processed independently: one bad event never blocks
      the rest.
    """

    def __init__(self, service: PaymentService) -> None:
        self._service = service
        self._lock = threading.Lock()
        self._seen_event_ids: set[str] = set()

    def handle(self, event: ChannelEvent) -> dict:
        with self._lock:
            if event.event_id in self._seen_event_ids:
                return {
                    "event_id": event.event_id,
                    "payment_id": event.payment_id,
                    "action": "duplicate_ignored",
                    "status": self._safe_status(event.payment_id),
                }
            self._seen_event_ids.add(event.event_id)

        succeeded = event.event_type is ChannelEventType.SUCCEEDED
        try:
            intent = self._service.apply_channel_result(event.payment_id, succeeded)
            return {
                "event_id": event.event_id,
                "payment_id": event.payment_id,
                "action": "applied",
                "status": intent.status.value,
            }
        except PaymentError as exc:
            return {
                "event_id": event.event_id,
                "payment_id": event.payment_id,
                "action": "rejected",
                "reason": str(exc),
                "status": self._safe_status(event.payment_id),
            }

    def handle_many(self, events: list[ChannelEvent]) -> list[dict]:
        return [self.handle(e) for e in events]

    def _safe_status(self, payment_id: str) -> str:
        try:
            return self._service.get(payment_id).status.value
        except Exception:
            return PaymentStatus.CREATED.value
