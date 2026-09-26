"""Core domain types: payment intents, ledger entries, idempotency records,
channel events and reconciliation differences. (Skeleton: types only.)"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class PaymentStatus(str, enum.Enum):
    """Deterministic lifecycle of a payment intent."""

    CREATED = "CREATED"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class EntryDirection(str, enum.Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    payment_id: str
    account: str
    direction: EntryDirection
    amount: int  # minor units, always positive
    seq: int


@dataclass
class PaymentIntent:
    payment_id: str
    idempotency_key: str
    amount: int
    currency: str
    status: PaymentStatus = PaymentStatus.CREATED
    version: int = 0
    failure_reason: str | None = None


@dataclass
class IdempotencyRecord:
    key: str
    request_fingerprint: str
    payment_id: str | None = None
    response_body: dict | None = None
    status_code: int | None = None
    in_progress: bool = False


class ChannelEventType(str, enum.Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ChannelEvent:
    event_id: str
    payment_id: str
    event_type: ChannelEventType
    amount: int
    currency: str
    reason: str | None = None


class DiffKind(str, enum.Enum):
    LOCAL_ONLY = "LOCAL_ONLY"          # present in ledger, missing in channel
    CHANNEL_ONLY = "CHANNEL_ONLY"      # present in channel, missing in ledger
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    DUPLICATE = "DUPLICATE"            # duplicated records on one side
    STATUS_MISMATCH = "STATUS_MISMATCH"


@dataclass(frozen=True)
class ReconDiff:
    kind: DiffKind
    payment_id: str
    detail: str
    local_amount: int | None = None
    channel_amount: int | None = None
