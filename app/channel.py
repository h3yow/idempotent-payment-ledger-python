"""Local simulated payment channel.

Produces callback events for payments. The channel keeps its own statement
(the "channel side" of reconciliation). Tests can inject duplicates,
reordering and delays by replaying / reshuffling the emitted events before
feeding them back to the service; the channel itself never talks to the
network.
"""
from __future__ import annotations

import threading
import uuid

from .models import ChannelEvent, ChannelEventType


class LocalChannel:
    def __init__(self) -> None:
        self._events: list[ChannelEvent] = []
        self._lock = threading.Lock()

    def emit(
        self,
        payment_id: str,
        succeeded: bool,
        amount: int,
        currency: str,
        reason: str | None = None,
    ) -> ChannelEvent:
        event = ChannelEvent(
            event_id=uuid.uuid4().hex,
            payment_id=payment_id,
            event_type=ChannelEventType.SUCCEEDED if succeeded else ChannelEventType.FAILED,
            amount=amount,
            currency=currency,
            reason=reason,
        )
        with self._lock:
            self._events.append(event)
        return event

    def statement(self) -> list[ChannelEvent]:
        """All events the channel recorded (its side of reconciliation)."""
        with self._lock:
            return list(self._events)
