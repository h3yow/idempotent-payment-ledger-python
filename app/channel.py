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

    def emit_refund(
        self,
        refund_id: str,
        payment_id: str,
        amount: int,
        succeeded: bool = True,
        currency: str | None = None,
    ) -> ChannelEvent:
        """Emit a refund callback event.

        Refund records always land on the statement (success or failure), so
        reconciliation can compare the channel's claimed refund outcome with
        the local one, including opposite conclusions.
        """
        with self._lock:
            self._sequence += 1
            event = ChannelEvent(
                event_id=f"evt_{self._sequence:06d}",
                payment_id=payment_id,
                event_type=(
                    ChannelEventType.REFUND_SUCCEEDED
                    if succeeded
                    else ChannelEventType.REFUND_FAILED
                ),
                amount=amount,
                sequence=self._sequence,
                refund_id=refund_id,
                currency=currency,
            )
            self._events.append(event)
            self._records.append(
                ChannelRecord(
                    payment_id=payment_id,
                    amount=amount,
                    record_id=f"rec_{self._sequence:06d}",
                    kind="refund",
                    refund_id=refund_id,
                    currency=currency,
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

    - Duplicate event ids are discarded before touching any service.
    - Refund events (REFUND_SUCCEEDED / REFUND_FAILED) are routed to the
      RefundService when one is configured.
    - Events for records already in a terminal state never change the
      result; contradicting ones are reported, not applied.
    - Refund amount/currency disagreements are reported with their own stable
      reason codes, distinguishable from state conflicts and unknown records.
    - Every event is processed independently: one bad event never blocks
      the rest.
    """

    def __init__(
        self,
        service: PaymentService,
        refunds: "RefundService | None" = None,  # noqa: F821
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

        if event.event_type in (
            ChannelEventType.REFUND_SUCCEEDED,
            ChannelEventType.REFUND_FAILED,
        ):
            return self._handle_refund(event)
        return self._handle_payment(event)

    def _handle_payment(self, event: ChannelEvent) -> dict:
        succeeded = event.event_type is ChannelEventType.SUCCEEDED
        try:
            intent = self._service.apply_channel_result(event.payment_id, succeeded)
            return {
                "event_id": event.event_id,
                "payment_id": event.payment_id,
                "refund_id": None,
                "action": "applied",
                "status": intent.status.value,
            }
        except PaymentError as exc:
            return {
                "event_id": event.event_id,
                "payment_id": event.payment_id,
                "refund_id": None,
                "action": "rejected",
                "reason": str(exc),
                "status": self._safe_status(event),
            }

    def _handle_refund(self, event: ChannelEvent) -> dict:
        base = {
            "event_id": event.event_id,
            "payment_id": event.payment_id,
            "refund_id": event.refund_id,
        }
        if self._refunds is None or not event.refund_id:
            return {
                **base,
                "action": "rejected",
                "reason_code": "REFUND_NOT_ROUTED",
                "reason": "no refund service configured or missing refund_id",
                "status": self._safe_status(event),
            }
        succeeded = event.event_type is ChannelEventType.REFUND_SUCCEEDED
        try:
            refund = self._refunds.apply_channel_result(
                event.refund_id,
                succeeded,
                amount=event.amount,
                currency=event.currency,
            )
            return {**base, "action": "applied", "status": refund.status.value}
        except PaymentError as exc:
            return {
                **base,
                "action": "rejected",
                "reason_code": exc.code,
                "reason": str(exc),
                "status": self._safe_status(event),
            }

    def handle_many(self, events: list[ChannelEvent]) -> list[dict]:
        return [self.handle(e) for e in events]

    def _safe_status(self, event: ChannelEvent) -> str:
        try:
            if event.refund_id is not None and self._refunds is not None:
                return self._refunds.get(event.refund_id).status.value
            return self._service.get(event.payment_id).status.value
        except Exception:
            return PaymentStatus.CREATED.value
