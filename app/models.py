"""Core type definitions for the payment ledger (skeleton)."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class PaymentStatus(enum.Enum):
    """Lifecycle states of a payment intent."""

    CREATED = "CREATED"
    CONFIRMED = "CONFIRMED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class RefundStatus(enum.Enum):
    """Lifecycle states of a refund (terminal: SUCCEEDED, FAILED)."""

    CREATED = "CREATED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class EntryDirection(enum.Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


@dataclass(frozen=True)
class LedgerEntry:
    """A single double-entry bookkeeping line."""

    entry_id: str
    payment_id: str
    account: str
    direction: EntryDirection
    amount: int  # minor units, always positive
    memo: str = ""
    refund_id: str = ""  # empty = payment flow; set for refund postings


@dataclass
class PaymentIntent:
    """A payment intent with a deterministic state machine."""

    payment_id: str
    idempotency_key: str
    amount: int
    currency: str
    status: PaymentStatus = PaymentStatus.CREATED
    version: int = 0


@dataclass
class Refund:
    """A refund against a succeeded payment, with its own state machine."""

    refund_id: str
    payment_id: str
    idempotency_key: str
    amount: int
    currency: str
    status: RefundStatus = RefundStatus.CREATED
    version: int = 0


class ChannelEventType(enum.Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ChannelEvent:
    """A callback event from the simulated channel.

    refund_id is set for refund events (empty/None = payment event);
    currency lets the receiver detect a callback that disagrees with the
    accepted refund.
    """

    event_id: str
    payment_id: str
    event_type: ChannelEventType
    amount: int
    sequence: int
    refund_id: str | None = None
    currency: str = ""


@dataclass(frozen=True)
class ChannelRecord:
    """A settled record in the simulated channel statement."""

    payment_id: str
    amount: int
    record_id: str
    refund_id: str | None = None  # set for refund settlements


class ReconDiffType(enum.Enum):
    MISSING_IN_CHANNEL = "MISSING_IN_CHANNEL"   # ledger has it, channel does not
    MISSING_IN_LEDGER = "MISSING_IN_LEDGER"     # channel has it, ledger does not
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"         # both sides exist, amounts differ
    DUPLICATE_IN_CHANNEL = "DUPLICATE_IN_CHANNEL"  # channel settled twice
    CONTRADICTORY = "CONTRADICTORY"             # both sides recorded, opposite conclusions


@dataclass(frozen=True)
class ReconDiff:
    diff_type: ReconDiffType
    payment_id: str
    ledger_amount: int | None = None
    channel_amount: int | None = None
    detail: str = ""
    refund_id: str | None = None  # set when the diff concerns a refund flow


@dataclass
class ReconReport:
    matched: list[str] = field(default_factory=list)
    diffs: list[ReconDiff] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.diffs
