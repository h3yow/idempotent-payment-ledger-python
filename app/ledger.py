"""Double-entry ledger.

Every posting is a set of entries whose debits and credits sum to the same
amount, so the ledger as a whole always conserves value. Postings are
atomic: either the whole set is appended or none of it is.
"""

from __future__ import annotations

import threading

from app.errors import UnbalancedEntry
from app.models import EntryDirection, LedgerEntry


class Ledger:
    """Append-only double-entry ledger; every posting must balance."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[LedgerEntry] = []

    def post(self, entries: list[LedgerEntry]) -> None:
        """Atomically append a balanced set of entries.

        Raises UnbalancedEntry if debits != credits or any amount <= 0;
        nothing is appended in that case.
        """
        if not entries:
            raise UnbalancedEntry("posting requires at least one entry")
        debit = 0
        credit = 0
        for e in entries:
            if e.amount <= 0:
                raise UnbalancedEntry(f"entry {e.entry_id} has non-positive amount {e.amount}")
            if e.direction is EntryDirection.DEBIT:
                debit += e.amount
            else:
                credit += e.amount
        if debit != credit:
            raise UnbalancedEntry(f"unbalanced posting: debit={debit} credit={credit}")
        with self._lock:
            self._entries.extend(entries)

    def entries(self) -> list[LedgerEntry]:
        with self._lock:
            return list(self._entries)

    def balance(self, account: str) -> int:
        """Signed balance: credits minus debits."""
        total = 0
        for e in self.entries():
            if e.account != account:
                continue
            total += e.amount if e.direction is EntryDirection.CREDIT else -e.amount
        return total

    def net_amount(self, payment_id: str, account: str) -> int:
        """Net posted amount for one payment on one account (credit - debit)."""
        total = 0
        for e in self.entries():
            if e.payment_id != payment_id or e.account != account:
                continue
            total += e.amount if e.direction is EntryDirection.CREDIT else -e.amount
        return total
