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
    """Lifecycle states of a refund (terminal: SUCCEEDED, FAILED, EXPIRED)."""

    CREATED = "CREATED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


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
    refund_id: str | None = None  # set when the entry belongs to a refund posting


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
    """A refund against an accepted payment, with its own state machine."""

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
    REFUND_SUCCEEDED = "REFUND_SUCCEEDED"
    REFUND_FAILED = "REFUND_FAILED"


@dataclass(frozen=True)
class ChannelEvent:
    """A callback event from the simulated channel.

    For refund events, ``refund_id`` is set and ``currency`` carries the
    currency the channel claims (compared against the accepted refund).
    """

    event_id: str
    payment_id: str
    event_type: ChannelEventType
    amount: int
    sequence: int
    refund_id: str | None = None
    currency: str | None = None


@dataclass(frozen=True)
class ChannelRecord:
    """A settled record in the simulated channel statement.

    Payment settlements have ``kind="payment"``; refund records have
    ``kind="refund"`` and carry their ``refund_id`` and claimed currency.
    """

    payment_id: str
    amount: int
    record_id: str
    kind: str = "payment"
    refund_id: str | None = None
    currency: str | None = None


class ReconDiffType(enum.Enum):
    MISSING_IN_CHANNEL = "MISSING_IN_CHANNEL"   # ledger has it, channel does not
    MISSING_IN_LEDGER = "MISSING_IN_LEDGER"     # channel has it, ledger does not
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"         # both sides exist, amounts differ
    DUPLICATE_IN_CHANNEL = "DUPLICATE_IN_CHANNEL"  # channel settled twice
    CONTRADICTORY_RESULT = "CONTRADICTORY_RESULT"  # both sides exist, opposite outcomes


@dataclass(frozen=True)
class ReconDiff:
    diff_type: ReconDiffType
    payment_id: str
    ledger_amount: int | None = None
    channel_amount: int | None = None
    detail: str = ""
    refund_id: str | None = None


@dataclass
class ReconReport:
    matched: list[str] = field(default_factory=list)
    diffs: list[ReconDiff] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.diffs
