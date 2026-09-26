"""Double-entry ledger.

Every fund movement is persisted as a balanced set of entries: for each
payment exactly one DEBIT and one CREDIT of equal amount. Entries are
append-only and posted atomically under a lock, so no success or failure
path can leave a half-written (unbalanced) record. `net_balance()` must
therefore always be zero.
"""
from __future__ import annotations

import threading
import uuid

from .errors import LedgerInvariantError
from .models import EntryDirection, LedgerEntry

# Local clearing accounts used by the simulated channel.
ACCOUNT_CHANNEL_CLEARING = "channel:clearing"
ACCOUNT_MERCHANT_RECEIVABLE = "merchant:receivable"


class Ledger:
    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []
        self._lock = threading.Lock()

    def post_payment(self, payment_id: str, amount: int) -> list[LedgerEntry]:
        """Post a balanced DEBIT/CREDIT pair for a successful payment."""
        if amount <= 0:
            raise LedgerInvariantError(f"amount must be positive, got {amount}")
        with self._lock:
            if self.entries_for(payment_id):
                raise LedgerInvariantError(
                    f"payment {payment_id} already has ledger entries"
                )
            base = len(self._entries)
            pair = [
                LedgerEntry(
                    entry_id=uuid.uuid4().hex,
                    payment_id=payment_id,
                    account=ACCOUNT_CHANNEL_CLEARING,
                    direction=EntryDirection.DEBIT,
                    amount=amount,
                    seq=base,
                ),
                LedgerEntry(
                    entry_id=uuid.uuid4().hex,
                    payment_id=payment_id,
                    account=ACCOUNT_MERCHANT_RECEIVABLE,
                    direction=EntryDirection.CREDIT,
                    amount=amount,
                    seq=base + 1,
                ),
            ]
            self._entries.extend(pair)
            return list(pair)

    def entries_for(self, payment_id: str) -> list[LedgerEntry]:
        return [e for e in self._entries if e.payment_id == payment_id]

    def all_entries(self) -> list[LedgerEntry]:
        return list(self._entries)

    def net_balance(self) -> int:
        """Sum of signed amounts across all entries; must always be zero."""
        total = 0
        for e in self._entries:
            total += e.amount if e.direction is EntryDirection.DEBIT else -e.amount
        return total
